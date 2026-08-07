"""Bounded ReactiveBus with dead-letter queue and metrics."""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

import rx

log = logging.getLogger(__name__)


class BoundedReactiveBus:
    """Wraps ReactiveBus with bounds, DLQ, and error isolation.

    * Message log and DLQ are bounded (deques with maxlen)
    * Subscriber exceptions are caught and routed to DLQ
    * Optional metrics (counters + memory-bound gauges)
    """

    def __init__(
        self,
        bus: Any = None,
        max_log: int = 10_000,
        max_dlq: int = 1_000,
        metrics: Any = None,
    ) -> None:
        from tradex_trading.reactive.bus import ReactiveBus
        self._bus = bus if bus is not None else ReactiveBus()
        # RLock: a subscriber publishing through the wrapper during delivery
        # re-enters the same thread's lock (the core bus enqueues it into the
        # active drain) instead of deadlocking.
        self._lock = threading.RLock()
        self.max_log = max_log
        self.max_dlq = max_dlq
        self._dlq: deque[Any] = deque(maxlen=max_dlq)
        self._metrics = metrics
        # Replace the bus's log with a bounded deque
        self._bus._log = deque(maxlen=max_log)
        # Share the registry with the wrapped bus so "published" is counted once.
        if hasattr(self._bus, "_metrics"):
            self._bus._metrics = metrics

    def publish(self, message: object) -> None:
        with self._lock:
            try:
                self._bus.publish(message)
            except Exception as exc:
                self._dlq.append(message)
                log.error("Message routed to DLQ: %s", exc)
                if self._metrics is not None:
                    self._metrics.counter("bus.messages.dead_lettered").inc()
            finally:
                self._record_bounds()

    def of_type(self, msg_type: type) -> rx.Observable:
        return self._bus.of_type(msg_type)

    def stream(self) -> rx.Observable:
        return self._bus.stream()

    def subscribe(self, on_next: Any = None, on_error: Any = None, on_completed: Any = None) -> Any:
        return self._bus.subscribe(on_next=on_next, on_error=on_error, on_completed=on_completed)

    def replay(self, start: Any = None, end: Any = None) -> rx.Observable:
        return self._bus.replay(start=start, end=end)

    def dispose(self) -> None:
        self._bus.dispose()

    def _record_bounds(self) -> None:
        """Refresh memory-bound gauges on the metrics registry (no-op if none)."""
        if self._metrics is None:
            return
        self._metrics.gauge("bus.messages.log_size").set(self.log_size)
        self._metrics.gauge("bus.messages.dlq_size").set(self.dlq_size)

    @property
    def dead_letter_queue(self) -> list[Any]:
        return list(self._dlq)

    @property
    def dlq_size(self) -> int:
        return len(self._dlq)

    @property
    def log_size(self) -> int:
        """Current number of messages retained in the bounded log."""
        log = getattr(self._bus, "_log", None)
        return len(log) if log is not None else 0


__all__ = ["BoundedReactiveBus"]
