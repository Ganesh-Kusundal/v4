"""Recovery ports and an event-backed order, position, and cash projection."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from tradex_domain.enums import OrderStatus, OrderType, TimeInForce
from tradex_domain.events import (
    CashAccountInitialized,
    CashReconciled,
    DomainEvent,
    OrderFilled,
)
from tradex_domain.execution import Order
from tradex_domain.value_objects import Money, Price, Quantity

from tradex_execution.trading_cache import TradingCache


class CashStateUnknownError(RuntimeError):
    """No opening cash anchor was found in the event log.

    Cash cannot be folded from fills alone. Reporting a zero balance here would
    let a live process size orders against a balance it never learned, so
    recovery fails loudly instead.
    """


@dataclass(frozen=True, slots=True)
class CashState:
    """Cash and fee state rebuilt from the event log."""

    cash: Decimal
    total_fees: Decimal
    opening_cash: Decimal


@runtime_checkable
class OrderRepository(Protocol):
    def save(self, order: Order) -> None: ...
    def get(self, order_id: str) -> Order | None: ...
    def all(self) -> Sequence[Order]: ...


@runtime_checkable
class EventStore(Protocol):
    def append(self, event: DomainEvent) -> int: ...
    def replay(self, stream: str = "orders") -> Iterator[DomainEvent]: ...


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    orders_recovered: int
    events_replayed: int
    last_sequence: int


class InMemoryEventStore:
    """Append only event store used by tests and single process recovery."""

    def __init__(self) -> None:
        self._events: list[DomainEvent] = []

    def append(self, event: DomainEvent) -> int:
        self._events.append(event)
        return len(self._events)

    def replay(self, stream: str = "orders") -> Iterator[DomainEvent]:
        del stream
        return iter(tuple(self._events))


class SessionRecovery:
    """Rebuild an OMS projection from an event store.

    Wave C1 audit events (``RiskDecision``, ``BrokerOrderRequested``,
    ``CancelRequested``, ``UnknownSubmission``, ``ReconciliationResult``)
    are replayed and counted in ``events_replayed`` but do not mutate the
    order repository — only carriers with an ``order`` attribute or
    ``OrderFilled`` update the projection.
    """

    def __init__(self, event_store: EventStore, order_repository: OrderRepository) -> None:
        self._event_store = event_store
        self._order_repository = order_repository

    def recover(self) -> RecoveryResult:
        events = 0
        orders = 0
        for event in self._event_store.replay("orders"):
            events += 1
            # Direct order carrier (OrderPlaced, OrderCancelled, OrderRejected,
            # BrokerOrderAcknowledged, …)
            order = getattr(event, "order", None)
            if order is not None:
                self._order_repository.save(order)
                orders += 1
            elif isinstance(event, OrderFilled):
                # Fill carrier: reconstruct a minimal Order so the repository
                # has a record even when the matching OrderPlaced event was lost.
                fill = event.fill
                existing = self._order_repository.get(fill.order_id.value)
                if existing is None:

                    from tradex_domain.enums import OrderStatus, OrderType, TimeInForce
                    from tradex_domain.value_objects import Price, Quantity

                    stub = Order(
                        order_id=fill.order_id,
                        instrument=fill.instrument,
                        side=fill.side,
                        order_type=OrderType.MARKET,
                        quantity=Quantity(value=fill.quantity.value),
                        price=Price(value=fill.price.value),
                        time_in_force=TimeInForce.DAY,
                        status=OrderStatus.FILLED,
                        filled_quantity=Quantity(value=fill.quantity.value),
                    )
                    self._order_repository.save(stub)
                    orders += 1
        return RecoveryResult(
            orders_recovered=orders,
            events_replayed=events,
            last_sequence=events,
        )


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """What a rebuild produced: the book, plus cash when the log anchors it."""

    recovery: RecoveryResult
    cash: CashState | None

    @property
    def events_replayed(self) -> int:
        return self.recovery.events_replayed

    @property
    def orders_recovered(self) -> int:
        return self.recovery.orders_recovered


#: Statuses a replayed fill must not advance past.
#:
#: CANCELLED and REJECTED only. Their carriers carry the post-transition state
#: and the venue will never fill them again. FILLED is deliberately excluded: a
#: carrier can be snapshotted as FILLED while still carrying
#: ``filled_quantity=0`` (the order is filled by a later event in the same
#: stream), and treating that as terminal would drop the fill entirely.
_TERMINAL_AFTER_FILL = frozenset({
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
})


def _stub_order(fill) -> Order:
    """A minimal FILLED order for a fill whose OrderPlaced event was lost."""
    return Order(
        order_id=fill.order_id,
        instrument=fill.instrument,
        side=fill.side,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=fill.quantity.value),
        price=Price(value=fill.price.value),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.FILLED,
        filled_quantity=Quantity(value=fill.quantity.value),
    )


def _order_totals(events: Sequence[DomainEvent]) -> dict[str, Decimal]:
    """Total orderable quantity per order, from the first carrier seen."""
    totals: dict[str, Decimal] = {}
    for event in events:
        order = getattr(event, "order", None)
        if order is not None:
            totals.setdefault(order.order_id.value, order.quantity.value)
    return totals


def _is_duplicate(
    key: str,
    seen: set[str],
    cumulative: dict[str, Decimal],
    order_totals: dict[str, Decimal],
    fill,
) -> bool:
    """True when *fill* is a re-delivery rather than a new partial.

    A venue ``fill_id`` is authoritative: the same id is the same execution.
    Without one, two equal-lot fills of one order are only separable by whether
    the order still has room. Accept while the cumulative filled quantity stays
    within the order; the fill that overshoots is the re-delivery. Treating
    every equal-lot fill as a duplicate silently loses real fills, and treating
    them all as new double-counts a re-delivery — this is the only reading that
    does neither.
    """
    if key not in seen:
        return False
    if fill.fill_id is not None:
        return True
    total = order_totals.get(fill.order_id.value)
    if total is None:
        # No order record to bound the sequence: the conservative reading is
        # that an identical repeat is a re-delivery.
        return True
    running = cumulative.get(fill.order_id.value, Decimal(0))
    return running + fill.quantity.value > total


def _fill_identity(fill) -> str:
    """Stable identity for a fill, used to skip re-delivered broker fills.

    The in-process ``FillDedup`` LRU does not survive a restart, so replay has
    to recognise a fill it has already folded. ``fill_id`` is preferred; the
    order/side/quantity/price tuple is the fallback for feeds that omit it, and
    :func:`_is_duplicate` decides what a repeated tuple actually means.
    """
    return fill.fill_id or (
        f"{fill.order_id.value}|{fill.side.value}|"
        f"{fill.quantity.value}|{fill.price.value}"
    )


def fold_cash(events: Sequence[DomainEvent]) -> CashState:
    """Fold cash, fees, and the opening anchor out of an ordered event log.

    Uses :class:`~tradex_execution.cash_ledger.CashLedger` — the same model the
    engine applies fills to — so a rebuilt balance cannot drift from a live one
    by construction. Fees come from the event, never from a fee schedule, which
    would re-derive a different number than the one charged.

    The fold starts at the LATEST opening anchor, so a re-anchoring restates
    the balance rather than being applied on top of the fills it already
    includes. Raises when the log carries no anchor at all: unknown cash must
    never be reported as zero.
    """
    from tradex_execution.cash_ledger import CashLedger

    # The LATEST anchor is the opening balance, and the fold starts there. A
    # re-anchoring restates the balance as of that moment, so fills recorded
    # before it are already included in the figure and must not be applied
    # again. Folding from the first anchor would resume with a stale balance;
    # folding every fill from the latest would double-count the history.
    last_anchor = max(
        (
            index
            for index, event in enumerate(events)
            if isinstance(event, CashAccountInitialized)
        ),
        default=None,
    )
    if last_anchor is None:
        raise CashStateUnknownError(
            "event log has no CashAccountInitialized anchor; cash cannot be "
            "reconstructed (unknown is not zero)",
        )
    opening = events[last_anchor].amount
    ledger = CashLedger(initial=opening, allow_negative=True)

    order_totals = _order_totals(events)
    seen: set[str] = set()
    cumulative: dict[str, Decimal] = {}
    for event in events[last_anchor + 1:]:
        if isinstance(event, CashReconciled):
            ledger.restate(event.amount - event.previous)
        elif isinstance(event, OrderFilled):
            fill = event.fill
            key = _fill_identity(fill)
            if _is_duplicate(key, seen, cumulative, order_totals, fill):
                continue
            seen.add(key)
            order_key = fill.order_id.value
            cumulative[order_key] = (
                cumulative.get(order_key, Decimal(0)) + fill.quantity.value
            )
            ledger.on_fill(fill.side, fill.quantity, fill.price)
            if event.fee_amount is not None and event.fee_amount != 0:
                ledger.on_fee(Money(amount=event.fee_amount))

    snapshot = ledger.snapshot()
    return CashState(
        cash=snapshot.cash,
        total_fees=snapshot.total_fees,
        opening_cash=opening,
    )


def recover_trading_cache(
    event_store: EventStore,
    cache: TradingCache,
) -> RecoveryOutcome:
    """Rebuild orders, positions, and cash by replaying *event_store*.

    Order carriers are projected via :class:`SessionRecovery`. Each
    ``OrderFilled`` is then re-applied through the same OMS helpers used at
    runtime, so ``filled_quantity`` and open lots match a live engine after a
    crash. Cash folds through the shared ledger rather than a parallel model.

    Returns a :class:`RecoveryOutcome`. Its ``cash`` is ``None`` when the log
    carries no opening anchor: the book is still rebuilt — orders and positions
    do not depend on cash — but cash is reported as *unknown* rather than zero.
    Callers that gate money (live readiness, risk sizing) must treat that as a
    stop, not a balance; :func:`fold_cash` is available when a caller wants the
    failure to be loud instead.
    """

    class _CacheRepository:
        def save(self, order: Order) -> None:
            cache.update_order(order)

        def get(self, order_id: str) -> Order | None:
            return cache.get_order(order_id)

        def all(self) -> Sequence[Order]:
            return cache.all_orders()

    # A rebuild replaces the book rather than adding to it: replaying a log
    # into a cache that already holds those fills would double-count them.
    cache.clear()

    from tradex_execution.order_manager import OrderManager
    from tradex_execution.position_manager import PositionManager

    order_manager = OrderManager(cache)
    position_manager = PositionManager(cache)
    events = tuple(event_store.replay("orders"))
    order_totals = _order_totals(events)
    seen: set[str] = set()
    cumulative: dict[str, Decimal] = {}
    orders = 0

    # One ordered pass. Carriers carry the post-transition snapshot — including
    # any fills already applied and any terminal status — so installing them in
    # stream order lets the final snapshot win. Projecting carriers and then
    # re-applying every fill to that final snapshot (two passes) would
    # double-count filled quantity and resurrect cancelled orders.
    for event in events:
        order = getattr(event, "order", None)
        if order is not None:
            cache.update_order(order)
            orders += 1
            continue
        if not isinstance(event, OrderFilled):
            continue
        fill = event.fill
        key = _fill_identity(fill)
        if _is_duplicate(key, seen, cumulative, order_totals, fill):
            continue
        seen.add(key)
        order_key = fill.order_id.value
        cumulative[order_key] = (
            cumulative.get(order_key, Decimal(0)) + fill.quantity.value
        )
        current = cache.get_order(order_key)
        if current is None:
            current = _stub_order(fill)
            cache.update_order(current)
            orders += 1
        # A carrier already carried this order past the fill (cancelled,
        # rejected, or filled with the quantity already included), so advancing
        # it again would double-count or resurrect it.
        if current.status not in _TERMINAL_AFTER_FILL:
            order_manager.on_order_filled(current, fill)
        position_manager.on_fill(fill)
        if event.fee_amount is not None and event.fee_amount != 0:
            position_manager.on_fee(fill, Money(amount=event.fee_amount))

    recovery = RecoveryResult(
        orders_recovered=orders,
        events_replayed=len(events),
        last_sequence=len(events),
    )

    cash = (
        fold_cash(events)
        if any(isinstance(e, CashAccountInitialized) for e in events)
        else None
    )
    return RecoveryOutcome(recovery=recovery, cash=cash)







# Re-exported for callers that reach the event store through recovery.
from tradex_execution.sqlite_event_store import SQLiteEventStore  # noqa: E402,F401
