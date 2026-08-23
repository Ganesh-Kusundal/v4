"""C1 regression: live fills must use broker average traded price, not reference price."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import OrderFilled
from tradex_domain.execution import Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.sdk.live_fill_bridge import LiveFillBridge


def _instrument() -> Equity:
    return Equity.of("NSE", "RELIANCE")


class _FakeCache:
    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}

    def get_order(self, order_id: str):  # type: ignore[no-untyped-def]
        return self._orders.get(order_id)

    def all_orders(self) -> list[Order]:
        return list(self._orders.values())


class _FakeEngine:
    def __init__(self) -> None:
        self.cache = _FakeCache()


class _FakeBus:
    def __init__(self) -> None:
        self.published: list[OrderFilled] = []

    def publish(self, event: object) -> None:
        if isinstance(event, OrderFilled):
            self.published.append(event)


class _FakeSub:
    def dispose(self) -> None:
        pass


def _make_bridge(bus: _FakeBus, engine: _FakeEngine) -> LiveFillBridge:
    return LiveFillBridge(
        bus=bus,  # type: ignore[arg-type]
        engine=engine,  # type: ignore[arg-type]
        subscribe_orders=lambda handler: _FakeSub(),  # type: ignore[arg-type]
    )


def test_live_fill_uses_average_traded_price() -> None:
    """Broker row averagePrice=100.5 must win over order.price=100 (reference)."""
    bus = _FakeBus()
    engine = _FakeEngine()
    bridge = _make_bridge(bus, engine)
    try:
        order = Order(
            order_id=OrderId(value="test-1"),
            instrument=_instrument(),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.FILLED,
            filled_quantity=Quantity(value=Decimal("5")),
            avg_price_traded=Price(value=Decimal("100.5")),
        )
        bridge._on_order(order)
        assert len(bus.published) == 1
        fill = bus.published[0].fill
        assert fill.price.value == Decimal("100.5"), f"Fill used reference price {fill.price.value} instead of traded 100.5"
        assert fill.quantity.value == Decimal("5")
    finally:
        bridge.close()


def test_live_fill_falls_back_to_reference_when_no_traded_price() -> None:
    bus = _FakeBus()
    engine = _FakeEngine()
    bridge = _make_bridge(bus, engine)
    try:
        order = Order(
            order_id=OrderId(value="test-2"),
            instrument=_instrument(),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.FILLED,
            filled_quantity=Quantity(value=Decimal("5")),
            avg_price_traded=None,
        )
        bridge._on_order(order)
        assert len(bus.published) == 1
        assert bus.published[0].fill.price.value == Decimal("100")
    finally:
        bridge.close()


def test_live_fill_falls_back_when_traded_price_zero() -> None:
    bus = _FakeBus()
    engine = _FakeEngine()
    bridge = _make_bridge(bus, engine)
    try:
        order = Order(
            order_id=OrderId(value="test-3"),
            instrument=_instrument(),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.FILLED,
            filled_quantity=Quantity(value=Decimal("5")),
            avg_price_traded=Price(value=Decimal("0")),
        )
        bridge._on_order(order)
        assert len(bus.published) == 1
        assert bus.published[0].fill.price.value == Decimal("100")
    finally:
        bridge.close()
