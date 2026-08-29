"""Thread-safe wrapper around ReactiveBus."""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

import rx

from tradex_trading.reactive.bus import ReactiveBus


class ThreadSafeReactiveBus:
    """Thread-safe facade over :class:`ReactiveBus`.

    * ``publish()`` is serialised with a ``Lock`` so that ``on_next()``
      calls to the RxPY Subject are never interleaved.
    * The internal message log uses a bounded ``deque(maxlen=10_000)``
      instead of an unbounded list. R1: pass ``event_log=`` to swap in a
      durable :class:`SQLEventLog` (instance or path); the deque is still
      the default for backward compatibility.
    """

    _DEFAULT_MAX_LOG = 10_000

    def __init__(
        self,
        bus: ReactiveBus | None = None,
        max_log: int = _DEFAULT_MAX_LOG,
        event_log: Any = None,
    ) -> None:
        # RLock: a subscriber publishing through the wrapper during delivery
        # re-enters the same thread's lock (the core bus enqueues it into the
        # active drain) instead of deadlocking.
        self._lock = threading.RLock()
        if event_log is not None:
            # Lazy import to avoid a hard dep from legacy call sites.
            from tradex_trading.reactive.event_log import SQLEventLog
            log: Any = (
                event_log if isinstance(event_log, SQLEventLog)
                else SQLEventLog(event_log)
            )
        else:
            log = deque(maxlen=max_log)
        self._bus = bus if bus is not None else ReactiveBus()
        self._log: Any = log
        # Wire the bus's log via the declared seam.
        self._bus.set_message_log(self._log)

    # ------------------------------------------------------------------
    # Publishing (serialised)
    # ------------------------------------------------------------------

    def publish(self, message: object) -> None:
        """Publish a message under a lock for thread safety."""
        with self._lock:
            self._bus.publish(message)

    # ------------------------------------------------------------------
    # Delegated read-only methods
    # ------------------------------------------------------------------

    def of_type(self, msg_type: type) -> rx.Observable:
        return self._bus.of_type(msg_type)

    def stream(self) -> rx.Observable:
        return self._bus.stream()

    def subscribe(
        self,
        on_next: Any = None,
        on_error: Any = None,
        on_completed: Any = None,
        max_queue_size: int | None = None,
        on_backpressure: Any = None,
        subscriber_type: str | None = None,
    ) -> Any:
        return self._bus.subscribe(
            on_next=on_next,
            on_error=on_error,
            on_completed=on_completed,
            max_queue_size=max_queue_size,
            on_backpressure=on_backpressure,
            subscriber_type=subscriber_type,
        )

    def replay(
        self,
        start: Any | None = None,
        end: Any | None = None,
    ) -> rx.Observable:
        return self._bus.replay(start=start, end=end)

    def dispose(self) -> None:
        self._bus.dispose()


__all__ = ["ThreadSafeReactiveBus"]
