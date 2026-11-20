"""Wire → domain order shaping lives in the application layer (no HTTP)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from tradex_application.orders import (
    build_modify_request,
    build_order_request,
    lookup_order,
    parse_price,
    parse_product,
)
from tradex_domain.enums import OrderSide, OrderType, ProductType
from tradex_domain.execution import BracketOrderRequest, OrderRequest
from tradex_domain.value_objects import CorrelationId, OrderId, Quantity

CID = CorrelationId(value="k1")


def test_parse_product_default_and_unknown() -> None:
    assert parse_product(None) is ProductType.INTRADAY
    assert parse_product("CNC") is ProductType.DELIVERY
    with pytest.raises(ValueError, match="unknown product"):
        parse_product("GARBAGE")


def test_parse_price_rejects_garbage_with_value_error() -> None:
    assert parse_price("10.5", "price").value == Decimal("10.5")
    with pytest.raises(ValueError, match="invalid price"):
        parse_price("abc", "price")


def test_build_order_request_plain() -> None:
    req = build_order_request(
        {"exchange": "NSE", "symbol": "RELIANCE", "side": "BUY", "quantity": 5}, CID
    )
    assert isinstance(req, OrderRequest)
    assert req.side is OrderSide.BUY and req.order_type is OrderType.MARKET
    assert req.price is None and req.correlation_id == CID


@pytest.mark.parametrize(
    "extra",
    [{"quantity": "abc"}, {"price": "abc"}, {"trigger_price": "abc"}],
)
def test_build_order_request_bad_decimals_are_value_errors(extra: dict) -> None:
    body = {"instrument_id": "NSE:RELIANCE", "side": "BUY", "quantity": 1, **extra}
    with pytest.raises(ValueError):
        build_order_request(body, CID)


def test_build_order_request_missing_field_is_key_error() -> None:
    with pytest.raises(KeyError):
        build_order_request({"instrument_id": "NSE:X", "quantity": 1}, CID)


def _existing(**kw):
    base = dict(
        side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=Quantity(Decimal(1)), price=None,
        time_in_force="DAY", instrument="inst",
        stop_loss_price=None, target_price=None, trailing_jump=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_modify_request_plain_branch() -> None:
    req = build_modify_request(
        _existing(), {"quantity": 3, "price": "99"}, "inst", CID
    )
    assert type(req) is OrderRequest
    assert req.price.value == Decimal("99")


def test_build_modify_request_picks_bracket_when_legs_exist() -> None:
    from tradex_domain.value_objects import Price

    existing = _existing(
        price=Price(Decimal("100")),
        stop_loss_price=Price(Decimal("90")),
        target_price=Price(Decimal("120")),
        trailing_jump=Price(Decimal("0")),
    )
    req = build_modify_request(existing, {"target_price": "130"}, "inst", CID)
    assert isinstance(req, BracketOrderRequest)
    assert req.target_price.value == Decimal("130")


def test_lookup_order_falls_back_to_broker() -> None:
    oid = OrderId(value="o1")
    engine = SimpleNamespace(get_order=lambda _o: None)
    broker = SimpleNamespace(get_order=lambda o: ("from-broker", o))
    assert lookup_order(engine, broker, oid) == ("from-broker", oid)
    engine2 = SimpleNamespace(get_order=lambda _o: "from-engine")
    assert lookup_order(engine2, broker, oid) == "from-engine"
