"""WebSocket reconnection manager with exponential backoff.

Used by broker WebSocket streaming adapters to manage reconnection attempts
when the connection drops unexpectedly.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tradex_brokers.common.retry_config import WS_RETRY_CONFIG

log = logging.getLogger(__name__)

# Cap the escalation exponent: with indefinite retries ``over`` grows without
# bound and ``2.0 ** over`` would eventually overflow a float long after the
# delay has already been clamped to ``max_delay`` (2**1024 > float range).
_MAX_ESCALATION_STEPS = 64


@dataclass(frozen=True)
class ReconnectConfig:
    """Configuration for WebSocket reconnection behaviour.

    Mirrors the v3 ``ReconnectConfig`` so that callers can construct a
    ``WsReconnectManager`` from a frozen config object.
    
    Deprecated: Use WS_RETRY_CONFIG from retry_config.py instead.
    """

    max_retries: int = WS_RETRY_CONFIG.max_retries
    base_delay: float = WS_RETRY_CONFIG.base_delay
    max_delay: float = WS_RETRY_CONFIG.max_delay
    exponential_base: float = WS_RETRY_CONFIG.exponential_base
    jitter: bool = True


class WSReconnectManager:
    """WebSocket reconnection backoff — retries **indefinitely** (AC-12/3b).

    A dropped order or market-data socket is never abandoned: giving up
    silently would leave a resting order believed live and the book blind
    for the rest of the session. There is no retry ceiling — only a *delay*
    ceiling, reached by backing off.

    Schedule (see :meth:`_raw_delay`):

    * attempts 1..``max_retries`` (default 10) use ``base_delay`` (default 5 s);
    * from attempt ``max_retries + 1`` the delay grows by ``exponential_base``
      per attempt from ``base_delay`` up to ``max_delay`` (default 300 s) and
      then holds at ``max_delay`` forever.

    So ``max_retries`` is the *escalation point*, not a give-up limit.

    Parameters
    ----------
    max_retries:
        Attempt at which the delay escalates toward ``max_delay`` (default 10).
        Not a give-up limit: ``next_delay()`` never stops returning a delay.
    base_delay:
        Delay in seconds used for the initial attempts (default 5 s).
    max_delay:
        Ceiling in seconds the delay grows to after escalation (default 300 s).
    exponential_base:
        Multiplier applied per attempt once past the escalation point.
    jitter:
        If ``True``, apply random jitter (±25 %) to each delay to avoid
        thundering-herd problems.
    """

    def __init__(
        self,
        max_retries: int = 10,
        base_delay: float = 5.0,
        max_delay: float = 300.0,
        exponential_base: float = 2.0,
        jitter: bool = True,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")
        if base_delay <= 0:
            raise ValueError("base_delay must be positive")
        if max_delay < base_delay:
            raise ValueError("max_delay must be >= base_delay")

        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._exponential_base = exponential_base
        self._jitter = jitter

        self._attempt: int = 0
        self._lock = threading.Lock()

    def _raw_delay(self, attempt: int) -> float:
        """Delay in seconds for a zero-based *attempt* index, before jitter.

        Because retries are indefinite, ``over`` can be arbitrarily large on a
        long outage. Clamp the exponent so ``base ** over`` cannot overflow
        before the ceiling clamp is applied (the cap is already reached long
        before the clamp bites).
        """
        if attempt < self._max_retries:
            # Flat base cadence until the escalation point.
            return min(self._base_delay, self._max_delay)
        over = min(attempt - self._max_retries, _MAX_ESCALATION_STEPS)
        return min(
            self._base_delay * (self._exponential_base ** over),
            self._max_delay,
        )

    # -- public API ---------------------------------------------------------

    def next_delay(self) -> float:
        """Compute the delay before the next reconnection attempt.

        Always returns a delay: reconnects retry indefinitely (AC-12/3b), so
        there is no attempt count at which the stream is abandoned. The delay
        escalates toward :attr:`max_delay` after the escalation point.
        """
        with self._lock:
            delay = self._raw_delay(self._attempt)
            if self._jitter:
                # ±25 % jitter
                delay = delay * (0.75 + random.random() * 0.5)  # noqa: S311
            self._attempt += 1
            return delay

    def reset(self) -> None:
        """Reset the retry counter (e.g. after a successful reconnection)."""
        with self._lock:
            self._attempt = 0

    @property
    def attempt_count(self) -> int:
        """Number of reconnection attempts made since the last reset."""
        with self._lock:
            return self._attempt

    @property
    def escalates(self) -> bool:
        """``True`` once the attempt counter is at/over the escalation point."""
        with self._lock:
            return self._attempt >= self._max_retries


# v3-compatible alias (lower-case 's' in 'Ws').
WsReconnectManager = WSReconnectManager


class AutoReconnectMixin:
    """Auto-reconnect for single-connection streaming backends.

    A backend mixing this in must, before calling :meth:`_init_reconnect`
    (call it at the end of ``__init__``):

    * set ``self._ws`` (current socket or ``None``), ``self._closing``,
      ``self._state_lock`` (``threading.RLock``), ``self._connect_lock``
      (``threading.Lock``), and ``self._ws_factory``;
    * implement ``_open_socket()`` — build the URL with a *fresh* token and
      return a newly opened socket;
    * implement ``_resubscribe(ws)`` — replay the subscription frames that
      were live on the previous socket;
    * call :meth:`_schedule_reconnect` from ``_receive_loop`` when the
      socket dies unexpectedly (after clearing ``self._ws``).

    Reconnect uses :class:`WSReconnectManager` exponential backoff with
    jitter. ``close()`` (which sets ``_closing``) cancels pending attempts.
    """

    # -- mixin contract (declared for type-checkers; set by concrete backends)

    _ws: Any
    _closing: bool
    _state_lock: threading.RLock
    _connect_lock: threading.Lock
    _ws_factory: Any
    _reconnect_enabled: bool
    _reconnect: WSReconnectManager
    _reconnect_thread: threading.Thread | None
    _reconnect_grace: float
    _last_success_at: float
    _on_reconnect: Callable[[], None] | None

    def set_reconnect_hook(self, cb: Callable[[], None] | None) -> None:
        """Register a callback invoked after every successful socket reconnect.

        The callback fires after ``_resubscribe`` completes and before the
        receive loop resumes, so the subscription set is already replayed when
        the caller is notified.  Exceptions in the callback are caught and
        logged — they must not abort the receive loop.

        Pass ``None`` to clear the hook.  Thread-safe: the assignment is
        atomic on CPython and guarded by exception isolation in
        ``_reconnect_worker``.
        """
        self._on_reconnect = cb

    def _open_socket(self) -> Any:
        """Open a fresh socket; implemented by the concrete backend."""
        raise NotImplementedError

    def _resubscribe(self, ws: Any) -> None:
        """Replay live subscriptions on the new socket; implemented by backends."""
        raise NotImplementedError

    def _receive_loop(self) -> None:
        """Socket receive loop; implemented by the concrete backend."""
        raise NotImplementedError

    def _init_reconnect(
        self,
        *,
        reconnect: bool = True,
        max_retries: int = 10,
        base_delay: float = 5.0,
        max_delay: float = 300.0,
        grace_seconds: float = 30.0,
    ) -> None:
        """Initialise the reconnect machinery (call at end of ``__init__``).

        Reconnects retry indefinitely: the delay stays at ``base_delay`` for
        the first ``max_retries`` attempts, then escalates toward ``max_delay``
        and holds there, so a long outage is survived without a dead socket
        (AC-12/3b).

        ``grace_seconds`` is the socket lifetime a successful reconnect must
        survive before the backoff counter resets. Without it, a flapping
        connection (opens then immediately drops — broker throttling) would
        reset the backoff every cycle and reconnect at the base delay forever.
        """
        self._reconnect_enabled = reconnect
        self._reconnect = WSReconnectManager(
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
            jitter=True,
        )
        self._reconnect_thread: threading.Thread | None = None
        self._reconnect_grace = grace_seconds
        self._last_success_at = time.monotonic()
        self._on_reconnect: Callable[[], None] | None = None

    def _schedule_reconnect(self) -> None:
        """Start (or queue) the next reconnection attempt after a socket drop."""
        with self._state_lock:
            if self._closing or not self._reconnect_enabled:
                return
            if self._reconnect_thread is not None:
                return
            delay = self._reconnect.next_delay()
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_worker, args=(delay,), daemon=True
            )
            self._reconnect_thread.start()

    def _reconnect_worker(self, delay: float) -> None:
        """Sleep, reopen the socket, and replay subscriptions."""
        time.sleep(delay)
        with self._connect_lock:
            with self._state_lock:
                if self._closing or self._ws is not None:
                    self._reconnect_thread = None
                    return
            try:
                ws = self._open_socket()
            except Exception:  # noqa: BLE001 — retry next round
                log.warning("stream reconnect socket open failed", exc_info=True)
                self._reconnect_thread = None
                self._schedule_reconnect()
                return
            with self._state_lock:
                if self._closing:
                    if hasattr(ws, "close"):
                        ws.close()
                    self._reconnect_thread = None
                    return
                self._ws = ws
            # Only reset the backoff when the previous socket survived the
            # stability grace period — a flapping connection keeps backing off.
            if time.monotonic() - self._last_success_at >= self._reconnect_grace:
                self._reconnect.reset()
            self._last_success_at = time.monotonic()
            # Release the reconnect slot BEFORE the new receive loop starts, so
            # a further drop can schedule the next attempt. Replay the live
            # subscription set first — the new loop must not fail-and-clear the
            # socket before the frames are sent.
            with self._state_lock:
                self._reconnect_thread = None
            try:
                self._resubscribe(ws)
            except Exception:  # noqa: BLE001 — best-effort replay
                log.warning("stream reconnect resubscribe failed", exc_info=True)
            cb = self._on_reconnect
            if cb is not None:
                try:
                    cb()
                except Exception:  # noqa: BLE001 — hook must not abort receive loop
                    log.warning("stream on_reconnect hook failed", exc_info=True)
            if hasattr(ws, "recv"):
                threading.Thread(target=self._receive_loop, daemon=True).start()


__all__ = [
    "AutoReconnectMixin",
    "ReconnectConfig",
    "WSReconnectManager",
    "WsReconnectManager",
]
