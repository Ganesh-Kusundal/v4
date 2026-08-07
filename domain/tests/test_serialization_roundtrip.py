"""Tests for to_dict()/from_dict() roundtrip on domain objects."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    TimeInForce,
)
from tradex_domain.execution import Fill, Order, Position
from tradex_domain.instruments import Equity
from tradex_domain.serialization import from_dict, to_dict
from tradex_domain.value_objects import (
    InstrumentId,
    Money,
    OrderId,
    Price,
    Quantity,
)


class TestPriceRoundtrip:
    def test_roundtrip(self):
        p = Price(Decimal("123.45"))
        d = p.to_dict()
        restored = Price.from_dict(d)
        assert restored.value == p.value


class TestQuantityRoundtrip:
    def test_roundtrip(self):
        q = Quantity(Decimal("100"))
        d = q.to_dict()
        restored = Quantity.from_dict(d)
        assert restored.value == q.value


class TestMoneyRoundtrip:
    def test_roundtrip(self):
        m = Money(Decimal("999.99"), "INR")
        d = m.to_dict()
        restored = Money.from_dict(d)
        assert restored.amount == m.amount
        assert restored.currency == m.currency


class TestOrderIdRoundtrip:
    def test_roundtrip(self):
        oid = OrderId("ORD-123")
        d = oid.to_dict()
        restored = OrderId.from_dict(d)
        assert restored.value == oid.value


class TestInstrumentIdRoundtrip:
    def test_equity_roundtrip(self):
        iid = InstrumentId.equity("NSE", "RELIANCE")
        d = iid.to_dict()
        restored = InstrumentId.from_dict(d)
        assert restored == iid

    def test_option_roundtrip(self):
        from datetime import date
        iid = InstrumentId.option("NSE", "NIFTY", date(2026, 1, 30), Decimal("20000"), "CE")
        d = iid.to_dict()
        restored = InstrumentId.from_dict(d)
        assert restored == iid


class TestOrderRoundtrip:
    def test_roundtrip(self):
        instrument = Equity.of("NSE", "RELIANCE")
        order = Order(
            order_id=OrderId("ORD-1"),
            instrument=instrument,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(Decimal("10")),
            price=Price(Decimal("100")),
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.NEW,
            product_type=ProductType.INTRADAY,
        )
        d = to_dict(order)
        restored = from_dict(Order, d)
        assert restored.order_id == order.order_id
        assert restored.side == order.side
        assert restored.status == order.status
        assert restored.quantity.value == order.quantity.value
        assert restored.instrument.symbol == "RELIANCE"


class TestFillRoundtrip:
    def test_roundtrip(self):
        instrument = Equity.of("NSE", "RELIANCE")
        fill = Fill(
            order_id=OrderId("ORD-1"),
            instrument=instrument,
            side=OrderSide.BUY,
            quantity=Quantity(Decimal("10")),
            price=Price(Decimal("100.50")),
            timestamp=datetime(2026, 1, 1, 9, 15, 0, tzinfo=UTC),
        )
        d = to_dict(fill)
        restored = from_dict(Fill, d)
        assert restored.order_id == fill.order_id
        assert restored.price.value == fill.price.value
        assert restored.quantity.value == fill.quantity.value


class TestPositionRoundtrip:
    def test_roundtrip(self):
        instrument = Equity.of("NSE", "RELIANCE")
        position = Position(
            instrument=instrument,
            quantity=Quantity(Decimal("50")),
            avg_price=Price(Decimal("200")),
            realized_pnl=Money(Decimal("500")),
            unrealized_pnl=Money(Decimal("250")),
        )
        d = to_dict(position)
        restored = from_dict(Position, d)
        assert restored.quantity.value == position.quantity.value
        assert restored.avg_price.value == position.avg_price.value
        assert restored.realized_pnl.amount == position.realized_pnl.amount
        assert restored.unrealized_pnl.amount == position.unrealized_pnl.amount
