"""RxPY Subject-backed ReactiveBus.

Replaces v3's imperative EventBus with reactive streams.
Every message is an Observable emission.
"""

from __future__ import annotations

import logging
from typing import Any

import rx
from rx import operators as ops
from rx.disposable import CompositeDisposable
from rx.subject import Subject

log = logging.getLogger(__name__)


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

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish(self, message: object) -> None:
        """Publish a message to all subscribers."""
        if self._log is not None:
            self._log.append(message)
        try:
            self._subject.on_next(message)
        except Exception as exc:
            log.error("Bus publish error: %s", exc)
        if self._metrics is not None:
            self._metrics.counter("bus.messages.published").inc()

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
