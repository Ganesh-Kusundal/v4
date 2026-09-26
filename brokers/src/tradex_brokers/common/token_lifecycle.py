"""Token lifecycle management with persistence and refresh scheduling.

Provides a ``TokenLifecyclePort`` protocol for broker-specific token operations
and a ``DurableTokenManager`` that handles persistence, expiry checks, and
automatic refresh.  The manager also supports a generation-aware mint mode
(ported from v3) where an injected ``MintStrategy`` produces tokens with
optional expiry and refresh-token rotation.

Additional helpers — ``TokenMintResult``, ``TokenBroadcast``,
``TokenRefreshScheduler`` — support fan-out notification and periodic refresh.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from tradex_domain import AuthenticationError, RateLimitError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cross-process helpers (atomic write, file locks)
# ---------------------------------------------------------------------------

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses msvcrt
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX uses fcntl
    msvcrt = None  # type: ignore[assignment]


_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[Path, threading.RLock] = {}


def _process_lock(path: Path) -> threading.RLock:
    """Return the in-process single-flight lock for one shared state file."""
    key = path.resolve()
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


#: Mode for files that hold broker credentials (access/refresh tokens, TOTP
#: cooldown state).  Owner read/write only: these files are secret material and
#: must never be readable by other local users.
SECRET_FILE_MODE = 0o600

#: Mode for directories we create that hold such files.  The file mode is the
#: primary control (the directory only needs to be traversable by its owner,
#: since everything secret inside it is already 0600).
SECRET_DIR_MODE = 0o700


def _create_secret_file(temp: Path) -> int:
    """Create ``temp`` empty with ``SECRET_FILE_MODE`` and return its fd.

    The mode is passed to :func:`os.open` at *creation* rather than applied
    afterwards with ``chmod``, so the file is never briefly readable by the
    group or others.  ``temp.open("w")`` would instead take the process umask
    (typically 0022 -> 0644, world-readable) and leak the window between
    ``open`` and any later ``chmod``.

    ``O_EXCL`` fails if the name already exists, which protects an existing
    target from being clobbered by a colliding temp name.  The caller always
    removes its temp name on the way out, so a collision means a stale file
    from a crashed writer with the same pid+thread id: unlink it and retry
    once, which is still safe because nothing has been written yet.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        return os.open(temp, flags, SECRET_FILE_MODE)
    except FileExistsError:
        # Same-pid/tid leftover from a crashed writer, or a concurrent writer
        # that has not finished yet.  The file is still empty in the first
        # case; a concurrent writer's file is never ours to reuse.
        with suppress(OSError):
            temp.unlink()
        return os.open(temp, flags, SECRET_FILE_MODE)


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace a same-directory file atomically, without exposing partial JSON.

    The file is created ``SECRET_FILE_MODE`` (0600) at the moment of creation,
    before a single byte of ``content`` is written, and ``os.replace`` carries
    that mode onto the destination — so a credential file is never observed
    world-readable, not even for an instant.  A pre-existing destination with
    looser permissions is replaced wholesale: the new file's mode wins, which
    is the point of the fix.

    Atomicity is unchanged: write -> flush -> ``os.fsync`` -> ``os.replace``
    -> ``fsync`` of the directory.  A crash mid-write therefore leaves either
    the old content or the new content under the real name, never a partial
    or wrongly-permissioned file.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=SECRET_DIR_MODE)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        fd = _create_secret_file(temp)
        try:
            handle = os.fdopen(fd, "w")
        except BaseException:  # never leak the descriptor on a failed wrap
            os.close(fd)
            raise
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, SECRET_FILE_MODE)  # defeat a permissive pre-existing mode
        os.replace(temp, path)
        if fcntl is not None:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            temp.unlink()


@contextmanager
def _exclusive_file_lock(path: Path):  # type: ignore[no-untyped-def]
    """Advisory cross-process lock; yields whether acquisition had to wait."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        waited = False
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                waited = True
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows only
            handle.seek(0)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                waited = True
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - unsupported platform
            raise RuntimeError("no supported cross-process file-lock implementation")
        try:
            yield waited
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows only
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


# ---------------------------------------------------------------------------
# Port protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class TokenLifecyclePort(Protocol):
    """Port for broker-specific token operations.

    Each broker adapter implements this protocol to handle its own token
    acquisition, refresh, and expiry semantics.
    """

    def get_access_token(self) -> str:
        """Return the current access token (may be expired)."""
        ...

    def refresh(self) -> str:
        """Refresh the access token and return the new value."""
        ...

    def is_expired(self) -> bool:
        """Return ``True`` if the current access token has expired."""
        ...

    # -- generation-aware methods (optional, v3 compatibility) ---------------

    def ensure_token(
        self,
        *,
        force_refresh: bool = False,
        rejected_token: str | None = None,
    ) -> str:
        """Return a valid token, minting or refreshing as needed."""
        ...

    def current(self) -> str:
        """Return the current token without triggering a refresh."""
        ...


# ---------------------------------------------------------------------------
# Supporting types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenMintResult:
    """Result of a real provider mint (TOTP generate or OAuth refresh).

    ``expires_at`` uses the same clock as the ``DurableTokenManager``
    (wall-clock seconds by default) and may be ``None`` when the provider
    gives no expiry — the token is then trusted until a 401.
    ``refresh_token`` is persisted for providers that rotate it (Upstox).
    """

    token: str
    expires_at: float | None = None
    refresh_token: str | None = None


MintStrategy = Callable[[], str | TokenMintResult]


@dataclass(frozen=True, slots=True)
class _TokenState:
    """Internal immutable snapshot of durable token state."""

    generation: int
    token: str
    issued_at: str
    expires_at: float | None = None
    refresh_token: str | None = None


# ---------------------------------------------------------------------------
# TokenBroadcast — fan-out of token refreshes
# ---------------------------------------------------------------------------


class TokenBroadcast:
    """Fan-out of token refreshes with per-receiver error isolation."""

    def __init__(self) -> None:
        self._receivers: list[Callable[[str], None]] = []

    def register(self, receiver: Callable[[str], None]) -> Callable[[str], None]:
        """Register a receiver and return it for decorator-style usage."""
        self._receivers.append(receiver)
        return receiver

    def broadcast(self, new_token: str) -> int:
        """Notify all receivers.  Exceptions are swallowed for isolation."""
        for receiver in list(self._receivers):
            try:
                receiver(new_token)
            except Exception:  # noqa: BLE001 — isolation is the contract
                continue
        return len(self._receivers)

    def receiver_count(self) -> int:
        """Return the number of registered receivers."""
        return len(self._receivers)


# ---------------------------------------------------------------------------
# TokenRefreshScheduler — periodic background refresh
# ---------------------------------------------------------------------------


class TokenRefreshScheduler:
    """Background refresh loop; never mints outside the manager's generation gate."""

    def __init__(self, manager: TokenLifecyclePort, interval_seconds: float) -> None:
        self._manager = manager
        self._interval = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._refresh_count = 0
        self._error_count = 0

    def start(self) -> None:
        """Start the background refresh thread (idempotent)."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout_seconds: float = 5.0) -> None:
        """Signal the background thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_seconds)
            self._thread = None

    def is_running(self) -> bool:
        """Return ``True`` if the background thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def refresh_now(self) -> bool:
        """Trigger an immediate refresh.  Returns ``True`` on success."""
        try:
            self._manager.ensure_token()
        except Exception:  # noqa: BLE001 — error_count is the contract
            self._error_count += 1
            return False
        self._refresh_count += 1
        return True

    def refresh_count(self) -> int:
        """Return the number of successful refreshes."""
        return self._refresh_count

    def error_count(self) -> int:
        """Return the number of failed refreshes."""
        return self._error_count

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            self.refresh_now()


# ---------------------------------------------------------------------------
# PortTokenManager — v4 port mode
# ---------------------------------------------------------------------------


class PortTokenManager:
    """Port-mode token manager (v4).

    Delegates ``get_token()`` / ``force_refresh()`` to a
    ``TokenLifecyclePort`` implementation and adds caching, generation
    tracking, and persistence on top.

    Parameters
    ----------
    port:
        Broker-specific token operations implementation.
    state_path:
        File path for persisting token state.  If ``None``, persistence is
        disabled and tokens live only in memory.
    """

    def __init__(
        self,
        port: TokenLifecyclePort | None = None,
        state_path: Path | str | None = None,
    ) -> None:
        self._port = port
        self._path = Path(state_path) if state_path is not None else None
        self._lock = threading.RLock()
        self._cached_token: str | None = None
        if self._port is not None and self._path is not None:
            self.load_state()

    # -- v4 port-mode API (generation-aware) -------------------------------

    def get_token(self) -> str:
        """Return a valid access token, refreshing if necessary.

        Tracks generations in the same durable way as mint mode so that a
        401 on a stale token burns exactly one mint slot across processes.

        Raises
        ------
        AuthenticationError
            If the token cannot be obtained or refreshed.
        """
        with self._lock:
            assert self._port is not None
            if self._cached_token and not self._port.is_expired():
                return self._cached_token
            return self._refresh_locked()

    def force_refresh(self) -> str:
        """Force a token refresh regardless of expiry."""
        with self._lock:
            return self._refresh_locked()

    def get_token_with_rejection(self, rejected_token: str | None = None) -> str:
        """Return a valid token; pass ``rejected_token`` when the broker 401'd.

        In port mode the token is refreshed via ``self._port.refresh()``.
        The ``rejected_token`` parameter is forwarded from
        ``ProviderHttpClient``'s 401-once handler so the manager can reject
        a stale 401 (where the port already refreshed concurrently).
        """
        with self._lock:
            assert self._port is not None
            if (
                rejected_token is not None
                and self._cached_token is not None
                and rejected_token != self._cached_token
            ):
                # Port already refreshed concurrently — return the newer cached token.
                return self._cached_token
            if self._cached_token and not self._port.is_expired():
                return self._cached_token
            return self._refresh_locked()

    def save_state(self) -> None:
        """Persist the current token to disk."""
        if self._path is None:
            return
        with self._lock:
            token = self._cached_token
        if token is None:
            return
        # Same hardened writer as the mint-mode path: a live access token is
        # credential material, so the file must land at SECRET_FILE_MODE.
        _atomic_write_text(self._path, json.dumps({"access_token": token}, indent=2))
        log.debug("Token state saved to %s", self._path)

    def load_state(self) -> None:
        """Load a previously persisted token from disk."""
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            token = data.get("access_token")
            if token:
                with self._lock:
                    self._cached_token = token
                log.debug("Token state loaded from %s", self._path)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to load token state from %s: %s", self._path, exc)

    def _refresh_locked(self) -> str:
        """Refresh the token.  Must be called while holding ``_lock``."""
        if self._port is None:
            raise RuntimeError("TokenLifecyclePort not configured")
        try:
            new_token = self._port.refresh()
        except Exception as exc:
            raise AuthenticationError(
                f"Token refresh failed: {exc}"
            ) from exc
        self._cached_token = new_token
        # Best-effort persistence so a concurrent process sees the rotated
        # token and treats a stale 401 via get_token_with_rejection().
        try:
            self.save_state()
        except Exception:
            log.warning("Failed to persist token after refresh", exc_info=True)
        return new_token


# ---------------------------------------------------------------------------
# MintTokenManager — v3 mint mode (generation-aware)
# ---------------------------------------------------------------------------


class MintTokenManager:
    """Mint-mode token manager (v3 compatibility).

    Generation-aware: tracks durable token generations, reuses valid tokens
    until expiry, mints on demand via an injected ``MintStrategy``, and
    refreshes on 401 exactly once per generation.  ``ensure_token()`` and
    ``current()`` are the primary entry points.

    Also satisfies ``TokenLifecyclePort`` itself (``get_access_token`` /
    ``refresh`` / ``is_expired`` delegate to the generation-aware paths),
    so a mint-mode manager can stand in wherever a port is expected.

    Parameters
    ----------
    state_path:
        File path for persisting token state.  If ``None``, persistence is
        disabled and tokens live only in memory.
    mint:
        A callable that returns a fresh token (``str``) or a
        ``TokenMintResult``.
    refresh_buffer_seconds:
        Seconds before ``expires_at`` at which the token is considered
        expired for proactive refresh purposes.
    clock:
        Injectable wall-clock function (defaults to ``time.time``).
    cooldown:
        Optional ``TotpCooldownGuard`` for rate-limiting TOTP mints.
    """

    def __init__(
        self,
        state_path: Path | str | None = None,
        *,
        mint: MintStrategy | None = None,
        totp: Any | None = None,
        refresh_buffer_seconds: float = 0.0,
        clock: Callable[[], float] = time.time,
        cooldown: Any | None = None,
    ) -> None:
        self._path = Path(state_path) if state_path is not None else None
        self._lock = threading.RLock()
        self._mint = mint
        self._totp = totp
        self._buffer = refresh_buffer_seconds
        self._clock = clock
        self._cooldown = cooldown
        self._memory: _TokenState | None = None
        self._highest_generation = 0
        self._state_lock_path = (
            self._path.with_name(self._path.name + ".lock") if self._path is not None else None
        )
        self._generation_path = (
            self._path.with_name(self._path.name + ".generation")
            if self._path is not None
            else None
        )

    # -- v3 mint-mode API (generation-aware) --------------------------------

    def ensure_token(
        self,
        *,
        force_refresh: bool = False,
        rejected_token: str | None = None,
    ) -> str:
        """Return a valid token, minting or refreshing as needed.

        In mint mode this is the primary entry point.  It handles
        generation tracking, expiry-aware reuse, and 401-once semantics.
        """
        with self._lock:
            if self._state_lock_path is None:
                return self._ensure_token_locked(force_refresh, rejected_token)
            with _process_lock(self._state_lock_path), _exclusive_file_lock(
                self._state_lock_path
            ):
                state = self._load(force_disk=True)
                return self._ensure_token_from_state(state, force_refresh, rejected_token)

    def _ensure_token_locked(
        self, force_refresh: bool, rejected_token: str | None
    ) -> str:
        state = self._load()
        return self._ensure_token_from_state(state, force_refresh, rejected_token)

    def _ensure_token_from_state(
        self,
        state: _TokenState | None,
        force_refresh: bool,
        rejected_token: str | None,
    ) -> str:
        if rejected_token is not None and state is not None:
            if rejected_token != state.token:
                # Stale 401 for an older generation — reuse current durable token.
                return state.token
            # Current token was rejected — mint a new generation (401-once).
            return self._mint_and_persist(state, use_totp=True)
        # Reuse a valid (non-expired) persisted token when in durable mode or
        # when a mint is available.
        # In legacy in-memory mode (no path, no mint), always generate a new generation.
        if (
            not force_refresh
            and state is not None
            and state.token
            and not self._expired(state)
            and (self._path is not None or self._mint is not None)
        ):
            return state.token
        # No valid state and no mint — in durable mode, raise; in-memory mode, use legacy gen-N.
        if self._mint is None and self._path is not None:
            raise AuthenticationError("no mint strategy and no valid persisted token")
        return self._mint_and_persist(state, use_totp=False)

    def current(self) -> str:
        """Return the current durable token without triggering a mint."""
        state = self._load(force_disk=self._path is not None)
        return state.token if state is not None else ""

    def persisted_refresh_token(self) -> str:
        """Return the refresh token persisted with the current durable state."""
        state = self._load(force_disk=self._path is not None)
        return state.refresh_token or "" if state is not None else ""

    # -- TokenLifecyclePort conformance --------------------------------------

    def get_access_token(self) -> str:
        """Return a valid token (generation-aware ``ensure_token`` alias)."""
        return self.ensure_token()

    def refresh(self) -> str:
        """Force-mint a fresh token (``ensure_token(force_refresh=True)``)."""
        return self.ensure_token(force_refresh=True)

    def is_expired(self) -> bool:
        """Return ``True`` if the persisted token is missing or expired."""
        state = self._load(force_disk=self._path is not None)
        return state is None or self._expired(state)

    # -- internals -------------------------------------------------------------

    def _expired(self, state: _TokenState) -> bool:
        if state.expires_at is None:
            return False
        return self._clock() + self._buffer >= state.expires_at

    def _mint_and_persist(self, state: _TokenState | None, *, use_totp: bool) -> str:
        generation = state.generation if state is not None else self._highest_generation
        if self._mint is not None:
            cooldown = self._cooldown
            if cooldown is not None and hasattr(cooldown, "acquire_attempt"):
                # Durable v2/v3 API: cross-process attempt reservation with
                # success / rate-limit / release accounting, so the shared
                # cooldown file (e.g. DHAN_COOLDOWN_PATH) is honored.
                reserved = cooldown.acquire_attempt()
                try:
                    result = self._mint()
                except RateLimitError:
                    cooldown.record_rate_limited()
                    raise
                except Exception:  # noqa: BLE001 — network failure is not a TOTP attempt
                    cooldown.release_attempt(reserved)
                    raise
                cooldown.record_success()
            elif cooldown is not None:
                # In-process v4 API (test doubles without the durable methods).
                if not cooldown.wait_and_acquire():
                    raise RateLimitError("TOTP cooldown active")
                try:
                    result = self._mint()
                except RateLimitError:
                    cooldown.record_attempt()
                    raise
                except Exception:  # noqa: BLE001 — network failure is not a TOTP attempt
                    cooldown.record_attempt()
                    raise
                cooldown.record_attempt()
            else:
                result = self._mint()
            if isinstance(result, TokenMintResult):
                token, expires_at, refresh_token = (
                    result.token,
                    result.expires_at,
                    result.refresh_token,
                )
            else:
                token, expires_at, refresh_token = str(result), None, None
        else:
            # Legacy deterministic path used by existing synthetic test doubles.
            if use_totp and self._totp is not None:
                self._totp.generate()
            token, expires_at, refresh_token = f"gen-{generation + 1}", None, None
        # Trust-until-rejected clamp: a token minted with a lifetime already
        # within the refresh buffer (or past) must still be reused between reads
        # instead of re-minting on every request.
        if expires_at is not None and expires_at <= self._clock() + self._buffer:
            expires_at = None
        next_gen = generation + 1
        new_state = _TokenState(
            generation=next_gen,
            token=token,
            issued_at=datetime.now(UTC).isoformat(),
            expires_at=expires_at,
            refresh_token=refresh_token,
        )
        self._save(new_state)
        return token

    def _save(self, state: _TokenState) -> None:
        self._highest_generation = max(self._highest_generation, state.generation)
        self._memory = state
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._generation_path is not None:
            _atomic_write_text(self._generation_path, str(self._highest_generation))
        _atomic_write_text(
            self._path,
            json.dumps(
                {
                    "generation": state.generation,
                    "token": state.token,
                    "issued_at": state.issued_at,
                    "expires_at": state.expires_at,
                    "refresh_token": state.refresh_token,
                    # v2/DhanTokenStore compat: readers of the shared state file
                    # hard-require ``access_token`` (and prefer ``expires_at_ms``);
                    # without these keys sibling projects see the file as invalid
                    # and re-mint against the broker's 2-minute rate limit.
                    "access_token": state.token,
                    "expires_at_ms": (
                        int(state.expires_at * 1000) if state.expires_at else None
                    ),
                }
            ),
        )

    def _read_generation_marker(self) -> int:
        if self._generation_path is None or not self._generation_path.exists():
            return 0
        try:
            return max(0, int(self._generation_path.read_text().strip()))
        except (OSError, ValueError):
            return 0

    def _load(self, *, force_disk: bool = False) -> _TokenState | None:
        if self._path is None:
            return self._memory
        if not force_disk and self._memory is not None and not self._expired(self._memory):
            return self._memory
        marker_generation = self._read_generation_marker()
        self._highest_generation = max(self._highest_generation, marker_generation)
        if not self._path.exists():
            self._memory = None
            return None
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            self._memory = None
            return None
        # v2 (legacy) durable files write ``access_token`` + ``expires_at_ms``;
        # read both key families so a valid v2 LIVE token survives the migration.
        raw_expires = data.get("expires_at")
        if raw_expires is None and data.get("expires_at_ms") is not None:
            raw_expires = float(data["expires_at_ms"]) / 1000.0
        raw_refresh = data.get("refresh_token")
        state = _TokenState(
            generation=int(data.get("generation", 0)),
            token=str(data.get("token") or data.get("access_token") or ""),
            issued_at=str(data.get("issued_at", "")),
            expires_at=float(raw_expires) if raw_expires is not None else None,
            refresh_token=str(raw_refresh) if raw_refresh else None,
        )
        self._highest_generation = max(self._highest_generation, state.generation)
        self._memory = state
        return state


# ---------------------------------------------------------------------------
# DurableTokenManager — backward-compatible dual-mode facade
# ---------------------------------------------------------------------------


class DurableTokenManager(PortTokenManager, MintTokenManager):
    """Dual-mode token manager kept for backward compatibility.

    Combines both modes in one object:

    **Port mode (v4)** — pass a ``TokenLifecyclePort`` implementation.
    ``get_token()`` / ``force_refresh()`` delegate to the port with caching
    + persistence (see ``PortTokenManager``).

    **Mint mode (v3 compatibility)** — pass a ``mint`` callable instead of
    a port.  ``ensure_token()`` / ``current()`` provide generation-aware
    minting (see ``MintTokenManager``).

    .. deprecated::
        New code should use ``PortTokenManager`` or ``MintTokenManager``
        directly. This class will be removed in a future release.
    """

    def __init__(
        self,
        port: TokenLifecyclePort | None = None,
        state_path: Path | str | None = None,
        *,
        mint: MintStrategy | None = None,
        totp: Any | None = None,
        refresh_buffer_seconds: float = 0.0,
        clock: Callable[[], float] = time.time,
        cooldown: Any | None = None,
    ) -> None:
        import warnings

        warnings.warn(
            "DurableTokenManager is deprecated; use PortTokenManager or "
            "MintTokenManager directly.",
            DeprecationWarning,
            stacklevel=2,
        )
        PortTokenManager.__init__(self, port, state_path)
        MintTokenManager.__init__(
            self,
            state_path,
            mint=mint,
            totp=totp,
            refresh_buffer_seconds=refresh_buffer_seconds,
            clock=clock,
            cooldown=cooldown,
        )


__all__ = [
    "MintStrategy",
    "MintTokenManager",
    "PortTokenManager",
    "TokenBroadcast",
    "TokenLifecyclePort",
    "TokenMintResult",
    "TokenRefreshScheduler",
    "DurableTokenManager",  # deprecated — kept for backward compatibility
]
