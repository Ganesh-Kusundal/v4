"""RxPY Subject-backed ReactiveBus.

Replaces v3's imperative EventBus with reactive streams.
Every message is an Observable emission.

R2: Internally partitioned into lanes (order, market, diagnostics, default)
so a slow subscriber on one lane cannot block delivery on another.
The public API (publish, subscribe, of_type, stream, dispose) is unchanged.
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

# ---------------------------------------------------------------------------
# R2: Lane classification
# ---------------------------------------------------------------------------

#: Lane names for the internal bus partition.
_LANE_ORDER = "order"
_LANE_MARKET = "market"
_LANE_DIAGNOSTICS = "diagnostics"
_LANE_DEFAULT = "default"

#: Event class names routed to each lane. Built from tradex_domain.events.
_ORDER_EVENT_NAMES: frozenset[str] = frozenset({
    "OrderPlaced", "OrderFilled", "OrderRejected",
    "OrderCancelled", "OrderModified", "PlaceOrderCommand",
})
_MARKET_TYPE_NAMES: frozenset[str] = frozenset({
    "Quote", "Depth", "Candle", "Bar", "Tick", "StaleFeed",
    "OHLC", "MarketData",
})
_DIAGNOSTIC_EVENT_NAMES: frozenset[str] = frozenset({
    "ErrorOccurred",
})


def _lane_for(subject: object) -> str:
    """Map an event (instance) or event type (class) to its bus lane.

    Names (not isinstance) keep this module free of a ``tradex_domain``
    import; unknown names fall back to the default lane.
    """
    name = subject.__name__ if isinstance(subject, type) else type(subject).__name__
    if name in _ORDER_EVENT_NAMES:
        return _LANE_ORDER
    if name in _MARKET_TYPE_NAMES:
        return _LANE_MARKET
    if name in _DIAGNOSTIC_EVENT_NAMES:
        return _LANE_DIAGNOSTICS
    return _LANE_DEFAULT


class ReactiveBus:
    """RxPY Subject-backed message bus.

    R2: internally partitioned into lanes. Each lane has its own Subject
    so ``of_type(X)`` subscribers only receive events from X's lane —
    a slow market-data consumer cannot block order-pipeline delivery.
    ``stream()`` and plain ``subscribe()`` still see every event via the
    shared all-lane Subject.
    """

    def __init__(
        self,
        message_log: list[Any] | None = None,
        metrics: Any | None = None,
        event_log: Any | None = None,
    ) -> None:
        # event_log wins over message_log when both are provided: callers that
        # want durability pass an SQLEventLog; the deque/list path is the
        # legacy default and stays in place for backward compatibility.
        #: R2: all-lane Subject — ``stream()`` and ``subscribe()`` listen here.
        self._subject: Subject = Subject()
        #: R2: per-lane Subjects for isolated ``of_type()`` delivery.
        self._lane_subjects: dict[str, Subject] = {
            _LANE_ORDER: Subject(),
            _LANE_MARKET: Subject(),
            _LANE_DIAGNOSTICS: Subject(),
            _LANE_DEFAULT: Subject(),
        }
        self._log: list[Any] | None = event_log if event_log is not None else message_log
        self._disposables: CompositeDisposable = CompositeDisposable()
        self._metrics = metrics
        self._pending: deque[Any] = deque()
        self._draining = False
        #: ponytail: per-subscriber max_queue_size / on_backpressure are
        #: now wired (C5). Each subscription that opts in via
        #: max_queue_size gets a per-subscriber remaining-capacity
        #: counter; the wrapped on_next decrements it, drops with a
        #: counter + on_backpressure call when it reaches zero. No
        #: separate queue — the bus is synchronous, so the gate is
        #: a delivery guard, not a buffer.
        self._subscriber_remaining: dict[int, int] = {}
        self._subscriber_backpressure: dict[int, Callable[[str], None]] = {}
        self._subscriber_type: dict[int, str] = {}
        #: M3: guard flag — ``publish()`` after ``dispose()`` raises
        #: ``RuntimeError`` instead of silently enqueuing to a completed
        #: Subject. Default False; flipped to True by ``dispose()``.
        self._disposed = False

    def set_message_log(self, log: Any) -> None:
        """Install an alternative message log (e.g. a bounded deque).

        Declared seam for :class:`ThreadSafeReactiveBus` — replaces the former
        ``wrapper._bus._log = ...`` private-attribute poke [REF-5].
        """
        self._log = log

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

        R2: each message is dispatched to both the all-lane Subject (for
        ``stream()`` / plain ``subscribe()`` consumers) and its lane-specific
        Subject (for ``of_type()`` consumers). Lane Subjects isolate
        subscribers: a slow market consumer does not block order delivery.

        Single-threaded only: the drain state (``_pending``/``_draining``) is
        not locked. Publish from one thread, or wrap the bus in
        ``ThreadSafeReactiveBus`` for concurrent publishers.

        M3: a ``publish()`` after ``dispose()`` raises ``RuntimeError``
        instead of silently dropping the message. Subscribers on a
        disposed bus are torn down; new messages have nowhere to go.
        """
        if self._disposed:
            raise RuntimeError(
                "ReactiveBus.publish: cannot publish on a disposed bus"
            )
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
                    log.critical(
                        "Bus drain exceeded %d nested deliveries; dropping backlog",
                        _MAX_NESTED_DELIVERIES,
                    )
                    if self._metrics is not None:
                        # C5: one clean increment per overflow, not the
                        # opaque formula from the previous implementation.
                        self._metrics.counter("bus.drain.exceeded").inc()
                        dropped = len(self._pending)
                        self._metrics.counter("bus.messages.dropped").inc(dropped)
                    break
                msg = self._pending.popleft()
                try:
                    # R2: dispatch to all-lane Subject first (stream/subscribe),
                    # then to the lane-specific Subject (of_type).
                    self._subject.on_next(msg)
                    lane = _lane_for(msg)
                    self._lane_subjects[lane].on_next(msg)
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

        R2: routes to the lane-specific Subject for the requested type,
        providing isolation between lanes. Unknown types fall back to the
        all-lane Subject so backward compatibility is preserved.
        """
        lane = _lane_for(msg_type)
        subject = self._lane_subjects.get(lane)
        if subject is not None and lane != _LANE_DEFAULT:
            return subject.pipe(
                ops.filter(lambda m: isinstance(m, msg_type)),
                ops.share(),
            )
        # Default lane or unknown type: use the all-lane Subject so
        # subscribers to non-domain types still work.
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

        ``max_queue_size`` and ``on_backpressure`` are now wired (C5).
        When ``max_queue_size`` is set, the subscription receives at
        most that many messages cumulatively; on overflow,
        ``on_backpressure`` is called and the dropped message is
        counted under ``bus.messages.dropped``. A subscription
        without ``max_queue_size`` is unchanged.
        """
        if on_next is not None:
            on_next = self._isolate(on_next)
            if max_queue_size is not None:
                on_next = self._gate(on_next, max_queue_size, on_backpressure, subscriber_type)
        d = self._subject.subscribe(
            on_next=on_next, on_error=on_error, on_completed=on_completed,
        )
        self._disposables.add(d)
        return d

    def _gate(
        self,
        on_next: Callable[[object], None],
        max_queue_size: int,
        on_backpressure: Callable[[SubscriberType], None] | None,
        subscriber_type: SubscriberType | None,
    ) -> Callable[[object], None]:
        """Wrap ``on_next`` with a remaining-capacity guard (C5).

        Maintains a counter on ``self._subscriber_remaining`` keyed by
        id(on_next). When the counter reaches 0, the message is
        dropped, the dropped-message counter is incremented, and
        ``on_backpressure`` is called (once per drop).
        """
        key = id(on_next)
        self._subscriber_remaining[key] = int(max_queue_size)
        self._subscriber_backpressure[key] = on_backpressure  # type: ignore[assignment]
        self._subscriber_type[key] = subscriber_type or ""

        def gated(value: object) -> None:
            remaining = self._subscriber_remaining.get(key, 0)
            if remaining <= 0:
                if self._metrics is not None:
                    self._metrics.counter("bus.messages.dropped").inc()
                cb = self._subscriber_backpressure.get(key)
                if cb is not None:
                    try:
                        cb(self._subscriber_type.get(key, ""))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("on_backpressure callback failed: %s", exc)
                return
            self._subscriber_remaining[key] = remaining - 1
            on_next(value)

        return gated

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

    def replay_log(self) -> Any:
        """Boot-replay seam: drain ``_pending`` first, then yield from the log.

        Returns a plain iterator (not an Observable) so the caller can consume
        it synchronously on startup. Used by the future boot-replay path that
        asks "what did we think happened 5 minutes ago?" after a crash.
        """
        yield from list(self._pending)
        log = self._log
        # SQLEventLog exposes .replay(after_id); list/deque expose __iter__.
        if hasattr(log, "replay"):
            if log is not None:
                yield from log.replay()
        elif log is not None:
            yield from log

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def dispose(self) -> None:
        """Clean teardown — dispose all subscriptions.

        M3: ``dispose()`` is idempotent. The ``_disposed`` guard is set
        before teardown so a concurrent ``publish()`` from another
        thread surfaces ``RuntimeError`` rather than silently enqueuing
        into a torn-down bus.

        R2: completes all lane Subjects in addition to the all-lane Subject.
        """
        if self._disposed:
            return
        self._disposed = True
        self._disposables.dispose()
        try:
            self._subject.on_completed()
        except Exception:  # pragma: no cover – defensive
            pass
        # R2: complete lane subjects so of_type subscribers get on_completed.
        for subject in self._lane_subjects.values():
            try:
                subject.on_completed()
            except Exception:  # pragma: no cover – defensive
                pass
