"""Per-broker TOTP rate-limit guard with cross-process durability.

Brokers such as Dhan and Upstox enforce a minimum interval between successive
TOTP code generations (Dhan: 2 minutes; Upstox: 10 minutes).  This guard
prevents the SDK from generating a new code too frequently, which would waste
the code and potentially lock the account.

The guard persists state to a JSON file and uses advisory cross-process file
locks so that multiple processes (e.g. a strategy runner and a monitoring
daemon) cannot race each other.  An in-process ``threading.RLock`` provides
single-flight semantics within a single process.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import ClassVar

from tradex_domain import RateLimitError

from tradex_brokers.common.paths import default_totp_cooldown_path
from tradex_brokers.common.token_lifecycle import (
    _atomic_write_text,
    _exclusive_file_lock,
    _process_lock,
)

# ---------------------------------------------------------------------------
# Broker-specific cooldown defaults
# ---------------------------------------------------------------------------

DHAN_COOLDOWN_SECONDS = 120.0
UPSTOX_COOLDOWN_SECONDS = 600.0
DEFAULT_COOLDOWN_SECONDS = DHAN_COOLDOWN_SECONDS
BROKER_COOLDOWN_SECONDS: dict[str, float] = {
    "dhan": DHAN_COOLDOWN_SECONDS,
    "upstox": UPSTOX_COOLDOWN_SECONDS,
}


# ---------------------------------------------------------------------------
# TotpRateLimitError
# ---------------------------------------------------------------------------


class TotpRateLimitError(RateLimitError):
    """Raised when TOTP generation is blocked by local or broker cooldown."""

    def __init__(self, message: str, *, remaining_seconds: float = 0.0) -> None:
        super().__init__(message)
        self.remaining_seconds = remaining_seconds


# ---------------------------------------------------------------------------
# TotpCooldownGuard
# ---------------------------------------------------------------------------


class TotpCooldownGuard:
    """Shared cooldown tracker for TOTP token generation attempts.

    Supports two APIs:

    **v4 in-process API** — ``acquire()``, ``record_attempt()``,
    ``wait_and_acquire()``, ``remaining_cooldown``.  These use
    ``time.monotonic()`` and an in-process lock; they are *not* durable
    across restarts.  Use this when you only need simple rate-limiting
    within a single process lifetime.

    **v3 durable API** — ``for_broker()``, ``check_allowed()``,
    ``acquire_attempt()``, ``record_success()``, ``record_rate_limited()``,
    ``remaining_cooldown_seconds()``.  These persist state to a JSON file
    and use cross-process file locks.  Use this when multiple processes
    share a broker login (e.g. strategy runner + monitoring daemon).
    """

    _class_lock: ClassVar[threading.Lock] = threading.Lock()
    _instances: ClassVar[dict[str, TotpCooldownGuard]] = {}

    def __init__(
        self,
        cooldown_seconds: float | None = None,
        *,
        broker: str | None = None,
        state_path: Path | None = None,
    ) -> None:
        # Resolve the in-process cooldown (v4 API).  With a broker but no
        # explicit override, use the broker table (120s dhan / 600s upstox)
        # rather than a generic 30s — otherwise for_broker() guards under-wait
        # relative to the durable cooldown they're meant to mirror.
        if cooldown_seconds is not None and cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative")
        broker_key = broker.lower() if broker else ""
        self._cooldown = (
            cooldown_seconds
            if cooldown_seconds is not None
            else BROKER_COOLDOWN_SECONDS.get(broker_key, DEFAULT_COOLDOWN_SECONDS)
            if broker_key
            else 30.0
        )
        self._last_attempt: float = 0.0
        self._lock = threading.Lock()

        # Durable state (v3 compatibility)
        self._broker = broker_key
        self._cooldown_seconds = (
            cooldown_seconds
            if cooldown_seconds is not None
            else BROKER_COOLDOWN_SECONDS.get(self._broker, DEFAULT_COOLDOWN_SECONDS)
        )
        self._state_path = (
            state_path.resolve()
            if state_path is not None
            else (default_totp_cooldown_path(self._broker) if self._broker else None)
        )
        self._lock_path = (
            self._state_path.with_name(self._state_path.name + ".lock")
            if self._state_path is not None
            else None
        )
        self._last_attempt_at: float | None = None
        self._last_success_at: float | None = None
        if self._state_path is not None:
            self._load_state()

    # -- v3 singleton constructor -------------------------------------------

    @classmethod
    def for_broker(cls, broker: str) -> TotpCooldownGuard:
        """Return the process-shared guard for a broker (broker-default cooldown).

        Deliberately no cooldown override: the singleton is keyed by broker, so
        an override would silently couple unrelated callers.  Tests construct a
        fresh ``TotpCooldownGuard`` directly when they need a custom window.
        """
        key = broker.lower()
        with cls._class_lock:
            if key not in cls._instances:
                cls._instances[key] = cls(broker=key)
            return cls._instances[key]

    # -- v4 in-process API --------------------------------------------------

    def acquire(self) -> bool:
        """Return ``True`` if a TOTP code can be generated right now.

        This is a non-blocking check — it does *not* record the attempt.
        Call ``record_attempt()`` after successfully generating a code.
        """
        if self._cooldown == 0.0:
            return True
        with self._lock:
            elapsed = time.monotonic() - self._last_attempt
            return elapsed >= self._cooldown

    def record_attempt(self) -> None:
        """Record that a TOTP generation was just performed."""
        with self._lock:
            self._last_attempt = time.monotonic()

    def wait_and_acquire(self, timeout: float | None = None) -> bool:
        """Block until the cooldown has elapsed, then return ``True``.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait.  ``None`` waits forever.

        Returns
        -------
        bool
            ``True`` if the cooldown elapsed within the timeout, ``False``
            otherwise.
        """
        if self._cooldown == 0.0:
            return True
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.acquire():
                return True
            with self._lock:
                remaining = self._cooldown - (time.monotonic() - self._last_attempt)
            if remaining <= 0:
                return True
            if deadline is not None:
                wall_remaining = deadline - time.monotonic()
                if wall_remaining <= 0:
                    return False
                remaining = min(remaining, wall_remaining)
            time.sleep(remaining)

    @property
    def remaining_cooldown(self) -> float:
        """Seconds remaining before the next TOTP can be generated (0 if ready)."""
        if self._cooldown == 0.0:
            return 0.0
        with self._lock:
            elapsed = time.monotonic() - self._last_attempt
            return max(0.0, self._cooldown - elapsed)

    # -- v3 durable API -----------------------------------------------------

    def _load_state(self) -> None:
        # Guard: the v3 durable API and v4 in-process API are mutually
        # exclusive on a single instance.  The durable methods (acquire_attempt,
        # check_allowed, record_success, record_rate_limited,
        # remaining_cooldown_seconds) all require cross-process state — calling
        # them on a guard constructed without ``state_path``/``broker`` would
        # silently consult the in-process monotonic clock instead of the
        # durable wall-clock state, mixing clock domains and corrupting
        # cooldowns.  Each durable method below guards itself.
        self._last_attempt_at = None
        self._last_success_at = None
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text())
            self._last_attempt_at = _coerce_wall_clock(data.get("last_attempt_at"))
            self._last_success_at = _coerce_wall_clock(data.get("last_success_at"))
            # legacy v2 field — treat as attempt timestamp
            if self._last_attempt_at is None:
                self._last_attempt_at = _coerce_wall_clock(data.get("rate_limited_at"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return

    def _require_durable(self) -> Path:
        """Raise if this guard was not constructed for the v3 durable API."""
        if self._lock_path is None:
            raise TypeError(
                "durable TOTP API requires state_path/broker; use acquire() / "
                "record_attempt() for in-process monotonic-clock rate limiting."
            )
        return self._lock_path

    def _persist_state(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "broker": self._broker,
                "last_attempt_at": self._last_attempt_at,
                "last_success_at": self._last_success_at,
            },
            indent=2,
        )
        _atomic_write_text(self._state_path, payload)

    def _remaining_unlocked(self) -> float:
        if self._last_attempt_at is None:
            return 0.0
        elapsed = time.time() - self._last_attempt_at
        return max(0.0, self._cooldown_seconds - elapsed)

    def remaining_cooldown_seconds(self) -> float:
        """Return the durable remaining cooldown (cross-process safe)."""
        lock_path = self._require_durable()
        with _process_lock(lock_path), _exclusive_file_lock(lock_path):
            self._load_state()
            return self._remaining_unlocked()

    def _raise_if_blocked_unlocked(self) -> None:
        remaining = self._remaining_unlocked()
        if remaining > 0:
            raise TotpRateLimitError(
                f"{self._broker} TOTP cooldown active; retry in {remaining:.0f}s",
                remaining_seconds=remaining,
            )

    def check_allowed(self) -> None:
        """Raise ``TotpRateLimitError`` if the cooldown is still active."""
        lock_path = self._require_durable()
        with _process_lock(lock_path), _exclusive_file_lock(lock_path):
            self._load_state()
            self._raise_if_blocked_unlocked()

    def acquire_attempt(self) -> float:
        """Atomically verify cooldown and reserve the next broker attempt."""
        lock_path = self._require_durable()
        with _process_lock(lock_path), _exclusive_file_lock(lock_path):
            self._load_state()
            self._raise_if_blocked_unlocked()
            reserved_at = time.time()
            self._last_attempt_at = reserved_at
            self._persist_state()
            return reserved_at

    def release_attempt(self, reserved_at: float) -> None:
        """Release one failed reservation without clearing newer state."""
        lock_path = self._require_durable()
        with _process_lock(lock_path), _exclusive_file_lock(lock_path):
            self._load_state()
            if self._last_attempt_at != reserved_at:
                return
            self._last_attempt_at = self._last_success_at
            self._persist_state()

    def record_success(self) -> None:
        """Record a successful broker login (resets the cooldown)."""
        lock_path = self._require_durable()
        with _process_lock(lock_path), _exclusive_file_lock(lock_path):
            self._load_state()
            now = time.time()
            self._last_attempt_at = now
            self._last_success_at = now
            self._persist_state()

    def record_rate_limited(self) -> None:
        """Record a broker rate limit without overwriting a newer success."""
        lock_path = self._require_durable()
        with _process_lock(lock_path), _exclusive_file_lock(lock_path):
            self._load_state()
            if (
                self._last_success_at is not None
                and self._last_attempt_at is not None
                and self._last_success_at >= self._last_attempt_at
            ):
                return
            self._last_attempt_at = time.time()
            self._persist_state()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_wall_clock(value: object) -> float | None:
    try:
        ts = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # Ignore old monotonic timestamps (meaningless after restart)
    if ts < 1_000_000_000:
        return None
    return ts


__all__ = [
    "BROKER_COOLDOWN_SECONDS",
    "DHAN_COOLDOWN_SECONDS",
    "TotpCooldownGuard",
    "TotpRateLimitError",
    "UPSTOX_COOLDOWN_SECONDS",
]
