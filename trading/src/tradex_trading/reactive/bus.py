"""RxPY Subject-backed ReactiveBus.

Replaces v3's imperative EventBus with reactive streams.
Every message is an Observable emission.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from datetime import time
from typing import Any

import rx
from rx import operators as ops
from rx.disposable import CompositeDisposable
from rx.subject import Subject

log = logging.getLogger(__name__)
SubscriberType = str


def _ts(m: object) -> time | None:  # type: ignore[type-arg]
    """Extract a comparable timestamp from a message, if present."""
    return getattr(m, "timestamp", None)


#: ponytail: nested-delivery cap — a buggy subscriber that republishes forever
#: must fail visibly (like the old RecursionError), not hang the bus silently.
_MAX_NESTED_DELIVERIES = 10_000
# ponytail: per-subscriber buffering removed — Subject is synchronous and
# live concurrency uses ThreadSafeReactiveBus; add BoundedReactiveBus when measured.
# Kept for backward compat with test import (deprecated stub):
_backpressure_triggered: dict[int, bool] = {}  # noqa: F401


class ReactiveBus:
    """RxPY Subject-backed message bus."""

    def __init__(self, message_log: list[Any] | None = None, metrics: Any | None = None) -> None:
        self._subject: Subject = Subject()
        self._log: list[Any] | None = message_log
        self._disposables: CompositeDisposable = CompositeDisposable()
        self._metrics = metrics
        self._pending: deque[Any] = deque()
        self._draining = False

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish(self, message: object) -> None:
        """Publish a message to all subscribers.

        Delivery is synchronous and latency-neutral: a nested ``publish()``
        made from inside a subscriber is enqueued and drained by the outermost
        ``publish()`` before it returns, so stream order is always causal
        (matching the message log) and effects are visible before ``publish``
        returns.

        Single-threaded only: the drain state (``_pending``/``_draining``) is
        not locked. Publish from one thread, or wrap the bus in
        ``ThreadSafeReactiveBus`` for concurrent publishers.
        """
        if self._log is not None:
            self._log.append(message)
        self._pending.append(message)
        if self._draining:
            return
        self._draining = True
        try:
            delivered = 0
            while self._pending:
                delivered += 1
                if delivered > _MAX_NESTED_DELIVERIES:
                    log.error(
                        "Bus drain exceeded %d nested deliveries; dropping backlog",
                        _MAX_NESTED_DELIVERIES,
                    )
                    self._pending.clear()
                    break
                msg = self._pending.popleft()
                try:
                    self._subject.on_next(msg)
                except Exception as exc:
                    log.error("Bus publish error: %s", exc)
                if self._metrics is not None:
                    self._metrics.counter("bus.messages.published").inc()
        finally:
            self._draining = False

    # ------------------------------------------------------------------
    # Subscribing
    # ------------------------------------------------------------------

    def of_type(self, msg_type: type) -> rx.Observable:
        """Typed stream — only messages of the given type."""
        return self._subject.pipe(
            ops.filter(lambda m: isinstance(m, msg_type)),
            ops.share(),
        )

    def stream(self) -> rx.Observable:
        """Raw Observable of ALL messages."""
        return self._subject.pipe(ops.share())

    def subscribe(
        self,
        on_next: Any = None,
        on_error: Any = None,
        on_completed: Any = None,
        max_queue_size: int | None = None,
        on_backpressure: Callable[[SubscriberType], None] | None = None,
        subscriber_type: SubscriberType | None = None,
    ) -> Any:
        """Subscribe and track the disposable for cleanup on dispose().

        ``max_queue_size`` / ``on_backpressure`` are accepted for backward
        compatibility but are currently no-ops — the bus is synchronous and
        unbounded (see module ponytail note).
        """
        if on_next is not None:
            on_next = self._isolate(on_next)
        d = self._subject.subscribe(
            on_next=on_next, on_error=on_error, on_completed=on_completed,
        )
        self._disposables.add(d)
        return d

    def _isolate(self, on_next: Any) -> Any:
        """Wrap ``on_next`` so errors don't propagate to other subscribers."""

        def safe(value: object) -> None:
            try:
                on_next(value)
            except Exception as exc:  # noqa: BLE001 – subscriber isolation
                log.error("Subscriber error: %s", exc)
                if self._metrics is not None:
                    self._metrics.counter("bus.messages.subscriber_errors").inc()

        return safe

    # ------------------------------------------------------------------
    # Replay / historical
    # ------------------------------------------------------------------

    def replay(
        self,
        start: Any | None = None,
        end: Any | None = None,
    ) -> rx.Observable:
        """Replay logged messages as an Observable sequence."""
        if not self._log:
            return rx.empty()
        return (
            rx.from_iterable(self._log)
            .pipe(
                ops.filter(
                    lambda m: (
                        (start is None or _ts(m) >= start)
                        and (end is None or _ts(m) <= end)
                    ),
                ),
            )
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def dispose(self) -> None:
        """Clean teardown — dispose all subscriptions."""
        self._disposables.dispose()
        try:
            self._subject.on_completed()
        except Exception:  # pragma: no cover – defensive
            pass
