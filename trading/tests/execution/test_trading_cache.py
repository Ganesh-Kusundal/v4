"""WS-E contract tests: TradingCache — ported from v3.

Adapted for v4 API: update_order/update_position (not set_*),
string-keyed lookups, no snapshot/restore.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import (
    Equity,
    Money,
    Order,
    OrderId,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Price,
    Quantity,
    TimeInForce,
)

from tradex_trading.execution.trading_cache import TradingCache


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _order(order_id: str = "o-1", status: OrderStatus = OrderStatus.PENDING) -> Order:
    return Order(
        order_id=OrderId(value=order_id),
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=status,
    )


def _position() -> Position:
    return Position(
        instrument=_eq(),
        quantity=Quantity(value=Decimal("10")),
        avg_price=Price(value=Decimal("100")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )


def test_order_update_and_get_by_str() -> None:
    cache = TradingCache()
    cache.update_order(_order("o-1"))
    assert cache.get_order("o-1") is not None
    assert cache.get_order("missing") is None


def test_all_orders_returns_insertion_set() -> None:
    cache = TradingCache()
    cache.update_order(_order("o-1"))
    cache.update_order(_order("o-2"))
    assert {o.order_id.value for o in cache.all_orders()} == {"o-1", "o-2"}


def test_position_keyed_by_symbol() -> None:
    cache = TradingCache()
    cache.update_position(_position())
    assert cache.get_position(_eq().symbol).quantity.value == 10
    assert cache.get_position("TCS") is None


def test_clear_drops_everything() -> None:
    cache = TradingCache()
    cache.update_order(_order("o-1"))
    cache.update_position(_position())
    cache.clear()
    assert cache.get_order("o-1") is None
    assert cache.get_position(_eq().symbol) is None


def test_order_update_overwrites() -> None:
    cache = TradingCache()
    cache.update_order(_order("o-1", OrderStatus.PENDING))
    cache.update_order(_order("o-1", OrderStatus.FILLED))
    order = cache.get_order("o-1")
    assert order is not None
    assert order.status is OrderStatus.FILLED


def test_set_order_alias() -> None:
    cache = TradingCache()
    cache.set_order(_order("o-1"))
    assert cache.get_order("o-1") is not None


def test_get_order_by_order_id() -> None:
    cache = TradingCache()
    cache.update_order(_order("o-1"))
    oid = OrderId(value="o-1")
    assert cache.get_order(oid) is not None


def test_set_position_and_get_by_instrument() -> None:
    cache = TradingCache()
    cache.set_position(_position())
    eq = _eq()
    # get_position accepts Instrument, InstrumentId, or str
    assert cache.get_position(eq) is not None
    assert cache.get_position(str(eq.instrument_id)) is not None


def test_snapshot_and_restore() -> None:
    cache = TradingCache()
    cache.set_order(_order("o-1"))
    cache.set_position(_position())
    snap = cache.snapshot()

    cache.clear()
    assert cache.get_order("o-1") is None

    cache.restore(snap)
    assert cache.get_order("o-1") is not None
    assert cache.get_position(_eq()) is not None


def test_instrument_key_variants() -> None:
    eq = _eq()
    # str passthrough
    assert TradingCache._instrument_key("RELIANCE") == "RELIANCE"
    # InstrumentId
    assert TradingCache._instrument_key(eq.instrument_id) == str(eq.instrument_id)
    # Instrument
    assert TradingCache._instrument_key(eq) == str(eq.instrument_id)
