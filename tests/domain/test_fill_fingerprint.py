"""Tests for fill_fingerprint."""
from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.fill_fingerprint import fingerprint
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity


def _make_fill(**overrides) -> Fill:
    fields = {
        "order_id": OrderId(value="ord-1"),
        "instrument": Equity.of("NSE", "RELIANCE"),
        "side": OrderSide.BUY,
        "quantity": Quantity(value=Decimal("10")),
        "price": Price(value=Decimal("2500.50")),
        "timestamp": datetime(2026, 9, 29, 9, 30, tzinfo=UTC),
        "fill_id": "trade-abc",
    }
    fields.update(overrides)
    return Fill(**fields)


def test_fingerprint_is_deterministic():
    fill = _make_fill()
    assert fingerprint(fill) == fingerprint(fill)


def test_fingerprint_is_16_hex_chars():
    fp = fingerprint(_make_fill())
    assert len(fp) == 16
    int(fp, 16)


def test_fingerprint_changes_with_fill_id():
    base = _make_fill()
    other = _make_fill(fill_id="trade-xyz")
    assert fingerprint(base) != fingerprint(other)


def test_fingerprint_changes_with_order_id():
    base = _make_fill()
    other = _make_fill(order_id=OrderId(value="ord-2"))
    assert fingerprint(base) != fingerprint(other)


def test_fingerprint_changes_with_side():
    base = _make_fill()
    other = _make_fill(side=OrderSide.SELL)
    assert fingerprint(base) != fingerprint(other)


def test_fingerprint_changes_with_quantity():
    base = _make_fill()
    other = _make_fill(quantity=Quantity(value=Decimal("20")))
    assert fingerprint(base) != fingerprint(other)


def test_fingerprint_changes_with_price():
    base = _make_fill()
    other = _make_fill(price=Price(value=Decimal("2501.00")))
    assert fingerprint(base) != fingerprint(other)


def test_fingerprint_ignores_timestamp():
    base = _make_fill()
    other = _make_fill(timestamp=datetime(2026, 9, 29, 10, 15, tzinfo=UTC))
    assert fingerprint(base) == fingerprint(other)


def test_fingerprint_handles_none_fill_id():
    fill = _make_fill(fill_id=None)
    fp = fingerprint(fill)
    assert len(fp) == 16
    int(fp, 16)
