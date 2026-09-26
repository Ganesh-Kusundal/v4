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

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconnectConfig:
    """Configuration for WebSocket reconnection behaviour.

    Mirrors the v3 ``ReconnectConfig`` so that callers can construct a
    ``WsReconnectManager`` from a frozen config object.
    """

    max_retries: int = 10
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True


class WSReconnectManager:
    """Manages WebSocket reconnection with exponential backoff and jitter.

    Parameters
    ----------
    max_retries:
        Maximum number of consecutive reconnection attempts before giving up.
        ``next_delay()`` returns ``None`` when this limit is exceeded.
    base_delay:
        Initial delay in seconds before the first reconnection attempt.
    max_delay:
        Maximum delay cap in seconds (after exponential growth).
    exponential_base:
        Multiplier applied to the delay for each successive retry.
    jitter:
        If ``True``, apply random jitter (±25 %) to each delay to avoid
        thundering-herd problems.
    """

    def __init__(
        self,
        max_retries: int = 10,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
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

    # -- public API ---------------------------------------------------------

    def next_delay(self) -> float | None:
        """Compute the delay before the next reconnection attempt.

        Returns
        -------
        float or None
            Delay in seconds, or ``None`` if *max_retries* has been exceeded.
        """
        with self._lock:
            if self._attempt >= self._max_retries:
                return None
            delay = min(
                self._base_delay * (self._exponential_base ** self._attempt),
                self._max_delay,
            )
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
    def exhausted(self) -> bool:
        """``True`` if the maximum number of retries has been reached."""
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
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        grace_seconds: float = 30.0,
    ) -> None:
        """Initialise the reconnect machinery (call at end of ``__init__``).

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
            if delay is None:
                log.warning("stream reconnect attempts exhausted; giving up")
                return
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
