"""Event-store-authoritative reconstruction of cash, fees, and positions.

The event store is the authority for recovery: a restarted process must rebuild
the same book a live engine held, without re-deriving anything the log already
records. These tests pin that contract against the *runtime* engine rather than
a parallel implementation, so a second accounting model cannot creep in.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.events import CashAccountInitialized, OrderFilled
from tradex_domain.execution import Fill, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.recovery import (
    CashStateUnknownError,
    InMemoryEventStore,
    recover_trading_cache,
)
from tradex_execution.trading_cache import TradingCache


def _instrument() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request(side: OrderSide = OrderSide.BUY, qty: str = "10") -> OrderRequest:
    return OrderRequest(
        instrument=_instrument(),
        side=side,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )


def _fill_event(
    order_id: str,
    side: OrderSide,
    qty: str,
    price: str,
    fee: str | None = None,
) -> OrderFilled:
    oid = OrderId(value=order_id)
    return OrderFilled(
        fill=Fill(
            order_id=oid,
            instrument=_instrument(),
            side=side,
            quantity=Quantity(value=Decimal(qty)),
            price=Price(value=Decimal(price)),
            fill_id=f"{order_id}-{side.value}-{qty}-{price}",
        ),
        fee_amount=Decimal(fee) if fee is not None else None,
    )


def test_reconstruction_restores_cash_from_fills():
    """Cash is a fold over the event log, seeded by the opening anchor."""
    store = InMemoryEventStore()
    store.append(CashAccountInitialized(amount=Decimal("1000")))
    store.append(_fill_event("o1", OrderSide.BUY, "10", "100"))  # -1000
    store.append(_fill_event("o2", OrderSide.SELL, "10", "110"))  # +1100

    cache = TradingCache()
    cash = recover_trading_cache(store, cache).cash

    assert cash.cash == Decimal("1100")
    assert cash.opening_cash == Decimal("1000")


def test_reconstruction_restores_fees_deducted_from_cash():
    """Fees recorded on the event are replayed; they are never recomputed."""
    store = InMemoryEventStore()
    store.append(CashAccountInitialized(amount=Decimal("2000")))
    store.append(_fill_event("o1", OrderSide.BUY, "10", "100", fee="20"))

    cache = TradingCache()
    cash = recover_trading_cache(store, cache).cash

    # 2000 opening - 1000 notional - 20 fee.
    assert cash.cash == Decimal("980")
    assert cash.total_fees == Decimal("20")


def test_reconstruction_restores_position_and_realized_pnl():
    """Position math folds through the same accountant the engine uses."""
    store = InMemoryEventStore()
    store.append(CashAccountInitialized(amount=Decimal("1000")))
    store.append(_fill_event("o1", OrderSide.BUY, "10", "100"))
    store.append(_fill_event("o2", OrderSide.SELL, "4", "110"))

    cache = TradingCache()
    recover_trading_cache(store, cache)

    pos = cache.get_position(_instrument())
    assert pos is not None
    assert pos.quantity.value == Decimal("6")
    assert pos.avg_price.value == Decimal("100")
    assert pos.realized_pnl.amount == Decimal("40")


def test_reconstruction_is_idempotent_under_duplicate_events():
    """A fill re-delivered by the venue must be folded exactly once.

    A broker re-stream after restart is a real failure mode and the
    process-local FillDedup LRU does not survive it, so the log itself can
    contain the same fill twice.
    """
    store = InMemoryEventStore()
    store.append(CashAccountInitialized(amount=Decimal("1000")))
    first = _fill_event("o1", OrderSide.BUY, "10", "100")
    store.append(first)
    store.append(first)  # venue re-delivers the same fill
    store.append(_fill_event("o2", OrderSide.SELL, "4", "110"))

    cache = TradingCache()
    cash = recover_trading_cache(store, cache).cash

    # 1000 - 1000 buy + 440 sell. The duplicate buy must not be charged twice.
    assert cash.cash == Decimal("440")
    pos = cache.get_position(_instrument())
    assert pos.quantity.value == Decimal("6")
    assert pos.avg_price.value == Decimal("100")
    assert pos.realized_pnl.amount == Decimal("40")


def test_reconstruction_is_idempotent_when_run_twice():
    """Re-running a rebuild over the same log must not double-apply a fill."""
    store = InMemoryEventStore()
    store.append(CashAccountInitialized(amount=Decimal("1000")))
    store.append(_fill_event("o1", OrderSide.BUY, "10", "100"))
    store.append(_fill_event("o2", OrderSide.SELL, "4", "110"))

    cache = TradingCache()
    first = recover_trading_cache(store, cache).cash
    second = recover_trading_cache(store, cache).cash

    assert second.cash == first.cash
    assert second.total_fees == first.total_fees
    pos = cache.get_position(_instrument())
    assert pos.quantity.value == Decimal("6")


def test_reconstruction_without_anchor_does_not_report_zero():
    """No opening anchor means unknown cash, not a confident zero.

    A silent 0 is what makes a live process size orders against a balance it
    never learned, so the rebuild reports ``None`` and the strict fold raises.
    """
    from tradex_execution.recovery import fold_cash

    store = InMemoryEventStore()
    store.append(_fill_event("o1", OrderSide.BUY, "10", "100"))

    cache = TradingCache()
    assert recover_trading_cache(store, cache).cash is None
    # The book is still rebuilt — positions do not depend on cash.
    assert cache.get_position(_instrument()) is not None

    with pytest.raises(CashStateUnknownError):
        fold_cash(tuple(store.replay("orders")))
