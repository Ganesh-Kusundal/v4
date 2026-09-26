"""A restarted process must not report a stale or invented mark.

Marks are a live-derived value: ``mark_price``/``unrealized_pnl`` are recomputed
from the quote feed by ``MarkToMarketService``, never folded from the event log.
Two properties matter after a restart:

  * a recovered position must not claim a mark it never had, and
  * reconciliation must not compare a field the recovery fold cannot restore,
    or it would report drift for a difference that is not a difference.

``avg_price`` and ``quantity`` ARE reconstructible from fills and ARE compared,
which is correct: those are real economic state the venue also reports.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import Position
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Money, Price, Quantity
from tradex_execution.reconciliation import ReconciliationEngine
from tradex_execution.recovery import InMemoryEventStore, recover_trading_cache
from tradex_execution.trading_cache import TradingCache

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _fill_book() -> tuple[InMemoryEventStore, TradingCache]:
    """A store holding one buy fill, and a cache rebuilt from it."""
    from tradex_domain.enums import OrderStatus
    from tradex_domain.events import OrderFilled, OrderPlaced
    from tradex_domain.execution import Fill, Order
    from tradex_domain.value_objects import OrderId

    order_id = OrderId("o-mark")
    store = InMemoryEventStore()
    order = Order(
        order_id=order_id,
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.FILLED,
        filled_quantity=Quantity(value=Decimal("10")),
    )
    store.append(OrderPlaced(order=order))
    store.append(
        OrderFilled(
            fill=Fill(
                order_id=order_id,
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                quantity=Quantity(value=Decimal("10")),
                price=Price(value=Decimal("100")),
                fill_id="f-mark",
            ),
        ),
    )
    cache = TradingCache()
    recover_trading_cache(store, cache)
    return store, cache


def test_recovered_position_carries_no_invented_mark() -> None:
    """Recovery must not fabricate a mark it never observed."""
    _store, cache = _fill_book()
    position = cache.get_position(INSTRUMENT)

    assert position is not None
    assert position.mark_price is None, (
        "a recovered position must not carry a mark; marks are quote-derived "
        "and the fold never saw a quote"
    )
    assert position.marked_at is None
    assert position.unrealized_pnl.amount == Decimal("0"), (
        "unrealized P&L without a mark must be zero, not a guess"
    )


def test_reconciliation_does_not_compare_unrestorable_marks() -> None:
    """Reconcile must compare only what recovery can rebuild.

    Comparing ``mark_price`` would flag drift for a local ``None`` against a
    broker mark on every single restart, which trains operators to ignore the
    reconciliation gate — the exact failure the gate exists to prevent.
    """
    _store, cache = _fill_book()
    local = cache.all_positions()
    # The broker reports the same economic position, with a live mark.
    broker_marked = [
        Position(
            instrument=INSTRUMENT,
            quantity=Quantity(value=Decimal("10")),
            avg_price=Price(value=Decimal("100")),
            realized_pnl=Money(amount=Decimal("0")),
            unrealized_pnl=Money(amount=Decimal("50")),
            mark_price=Price(value=Decimal("105")),
        ),
    ]

    drifts = ReconciliationEngine().reconcile(local, broker_marked)

    assert drifts == [], (
        "an equal position with a broker-side mark must not read as drift: "
        f"{[d.reason for d in drifts]}"
    )


def test_reconciliation_still_catches_real_position_drift() -> None:
    """The gate must not be weakened into uselessness."""
    _store, cache = _fill_book()
    local = cache.all_positions()
    broker_wrong_qty = [
        Position(
            instrument=INSTRUMENT,
            quantity=Quantity(value=Decimal("9")),
            avg_price=Price(value=Decimal("100")),
            realized_pnl=Money(amount=Decimal("0")),
            unrealized_pnl=Money(amount=Decimal("0")),
        ),
    ]

    drifts = ReconciliationEngine().reconcile(local, broker_wrong_qty)

    assert drifts, "a genuine quantity mismatch must still be reported"
    assert any(d.diff == Decimal("1") for d in drifts)


def test_reconciliation_still_catches_avg_price_drift() -> None:
    """avg_price is reconstructible from fills, so it must stay compared."""
    _store, cache = _fill_book()
    local = cache.all_positions()
    broker_wrong_price = [
        Position(
            instrument=INSTRUMENT,
            quantity=Quantity(value=Decimal("10")),
            avg_price=Price(value=Decimal("107")),
            realized_pnl=Money(amount=Decimal("0")),
            unrealized_pnl=Money(amount=Decimal("0")),
        ),
    ]

    drifts = ReconciliationEngine().reconcile(local, broker_wrong_price)

    assert any("avg_price" in (d.reason or "") for d in drifts), (
        "a real avg_price mismatch must still be reported"
    )
