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
      instead of an unbounded list.
    """

    _DEFAULT_MAX_LOG = 10_000

    def __init__(
        self,
        bus: ReactiveBus | None = None,
        max_log: int = _DEFAULT_MAX_LOG,
    ) -> None:
        # RLock: a subscriber publishing through the wrapper during delivery
        # re-enters the same thread's lock (the core bus enqueues it into the
        # active drain) instead of deadlocking.
        self._lock = threading.RLock()
        self._bus = bus if bus is not None else ReactiveBus()
        # Replace the bus's unbounded log with a bounded deque.
        self._log: deque[Any] = deque(maxlen=max_log)
        self._bus._log = self._log  # type: ignore[assignment]

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
        self, on_next: Any = None, on_error: Any = None, on_completed: Any = None,
    ) -> Any:
        return self._bus.subscribe(on_next=on_next, on_error=on_error, on_completed=on_completed)

    def replay(
        self,
        start: Any | None = None,
        end: Any | None = None,
    ) -> rx.Observable:
        return self._bus.replay(start=start, end=end)

    def dispose(self) -> None:
        self._bus.dispose()


__all__ = ["ThreadSafeReactiveBus"]
