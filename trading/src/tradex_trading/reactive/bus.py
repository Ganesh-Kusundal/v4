"""RxPY Subject-backed ReactiveBus.

Replaces v3's imperative EventBus with reactive streams.
Every message is an Observable emission.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

import rx
from rx import operators as ops
from rx.disposable import CompositeDisposable
from rx.subject import Subject

log = logging.getLogger(__name__)

#: ponytail: nested-delivery cap — a buggy subscriber that republishes forever
#: must fail visibly (like the old RecursionError), not hang the bus silently.
_MAX_NESTED_DELIVERIES = 10_000


class ReactiveBus:
    """RxPY Subject-backed message bus.

    Replaces v3's imperative EventBus with reactive streams.
    Every message is an Observable emission.
    """

    def __init__(self, message_log: list[Any] | None = None, metrics: Any = None) -> None:
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
        returns. No buffering across calls — live market events are never
        delayed by ordering.

        Single-threaded only: the drain state (``_pending``/``_draining``) is
        not locked. Publish from one thread, or wrap the bus in
        ``ThreadSafeReactiveBus`` for concurrent publishers.
        """
        if self._log is not None:
            self._log.append(message)
        self._pending.append(message)
        if self._draining:
            return  # reentrant publish — the active drain delivers it
        self._draining = True
        try:
            delivered = 0
            while self._pending:
                delivered += 1
                if delivered > _MAX_NESTED_DELIVERIES:
                    log.error(
                        "Bus drain exceeded %d nested deliveries; "
                        "dropping backlog",
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
        """Typed stream — only messages of the given type.

        Usage::

            bus.of_type(Quote).subscribe(handle_quote)
            bus.of_type(Order).pipe(filter(...), map(...)).subscribe(...)
        """
        return self._subject.pipe(
            ops.filter(lambda m, _t=msg_type: isinstance(m, _t)),
            ops.share(),
        )

    def stream(self) -> rx.Observable:
        """Raw Observable of ALL messages."""
        return self._subject.pipe(ops.share())

    def subscribe(
        self, on_next: Any = None, on_error: Any = None, on_completed: Any = None,
    ) -> Any:
        """Subscribe and track the disposable for cleanup on dispose().

        The ``on_next`` handler is wrapped so a raising subscriber is isolated:
        its exception is logged (and counted via metrics) without preventing
        other subscribers from receiving the message.
        """
        if on_next is not None:
            on_next = self._isolate(on_next)
        d = self._subject.subscribe(
            on_next=on_next, on_error=on_error, on_completed=on_completed,
        )
        self._disposables.add(d)
        return d

    def _isolate(self, on_next: Any) -> Any:
        """Wrap an ``on_next`` handler so errors don't propagate to other subscribers."""

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
        """Replay logged messages as an Observable sequence.

        *start* and *end* are reserved for future time-range filtering.
        """
        if not self._log:
            return rx.empty()
        return rx.from_iterable(self._log)

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
