"""POST /orders and POST /orders/bracket — the ``product`` field reaches the engine.

The shell renders a live MIS/CNC/NRML selector and sends the chosen value on
every order request, but the route used to build ``OrderRequest`` /
``BracketOrderRequest`` without it, so the selection was silently discarded
and every order reached the broker as the ``INTRADAY`` default — a real
margin-treatment defect, not a cosmetic one.

These tests pin the wire-string → ``ProductType`` contract on BOTH request
builders:

* the three strings the frontend actually sends map to the intended product,
* an absent ``product`` still yields the pre-existing default (existing
  callers unaffected),
* an UNKNOWN product is refused with 422 rather than coerced to the default —
  a silently-wrong product is precisely the bug being fixed, so a fallback
  would reintroduce it,
* the bracket path honours the same mapping.

The fake session/engine shape mirrors ``test_bracket_order.py``: the engine
records the request the route built, so assertions run on the real domain
object on its way to submission, not on a mock.
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from tradex_domain.enums import (  # noqa: E402
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    TimeInForce,
)
from tradex_domain.execution import (  # noqa: E402
    BracketOrderRequest,
    OrderReceipt,
    OrderRequest,
)
from tradex_domain.value_objects import OrderId  # noqa: E402

from tradex_interfaces.routes.orders import (  # noqa: E402
    _PRODUCT_BY_WIRE,
    _parse_product,
)
from tradex_trading.interface.fastapi_app import create_app  # noqa: E402


def _order_body(**overrides: Any) -> dict:
    body = {
        "instrument_id": "NSE:RELIANCE",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 10,
        "price": "2500.00",
    }
    body.update(overrides)
    return body


def _bracket_body(**overrides: Any) -> dict:
    body = _order_body(
        stop_loss_price="2450.00",
        target_price="2600.00",
    )
    body.update(overrides)
    return body


class _FakeCaps:
    supports_super_order = True


class _FakeBroker:
    def __init__(self) -> None:
        self.capabilities = _FakeCaps()


class _FakeEngine:
    """Records the request the route built; returns a canned receipt."""

    def __init__(self, result: Any = None) -> None:
        self.submitted: list[OrderRequest] = []
        self._result = result or OrderReceipt(
            order_id=OrderId(value="product-1"),
            status=OrderStatus.SUBMITTED,
            message="submitted",
        )

    def submit(self, request: OrderRequest) -> Any:
        self.submitted.append(request)
        return self._result


class _FakeSession:
    def __init__(self, engine: _FakeEngine | None = None) -> None:
        self._broker = _FakeBroker()
        self.engine = engine or _FakeEngine()


def _post_order(
    session: _FakeSession, key: str, body: dict | None = None, path: str = "/orders",
) -> Any:
    client = TestClient(create_app(session=session))
    return client.post(
        path,
        json=body if body is not None else _order_body(),
        headers={"Idempotency-Key": key},
    )


# ---------------------------------------------------------------------------
# The wire -> domain mapping itself
# ---------------------------------------------------------------------------


def test_wire_map_covers_exactly_the_frontend_vocabulary() -> None:
    """The frontend's MIS/CNC/NRML selector (plus the MTF it may add) is the
    whole vocabulary: every key resolves, and nothing else is reachable."""
    assert _PRODUCT_BY_WIRE == {
        "MIS": ProductType.INTRADAY,
        "CNC": ProductType.DELIVERY,
        "NRML": ProductType.MARGIN,
        "MTF": ProductType.MTF,
    }
    for wire, product in _PRODUCT_BY_WIRE.items():
        assert _parse_product({"product": wire}) is product


def test_parse_product_absent_keeps_domain_default() -> None:
    assert _parse_product({}) is ProductType.INTRADAY
    assert _parse_product({"product": None}) is ProductType.INTRADAY


def test_parse_product_unknown_raises_422() -> None:
    with pytest.raises(Exception) as excinfo:
        _parse_product({"product": "GARBAGE"})
    assert getattr(excinfo.value, "status_code", None) == 422


# ---------------------------------------------------------------------------
# POST /orders — the plain order path
# ---------------------------------------------------------------------------


def test_order_mis_maps_to_intraday() -> None:
    session = _FakeSession()
    resp = _post_order(session, "product-mis", _order_body(product="MIS"))
    assert resp.status_code == 200, resp.text
    assert len(session.engine.submitted) == 1
    request = session.engine.submitted[0]
    assert isinstance(request, OrderRequest)
    assert request.product_type is ProductType.INTRADAY


def test_order_cnc_maps_to_delivery() -> None:
    session = _FakeSession()
    resp = _post_order(session, "product-cnc", _order_body(product="CNC"))
    assert resp.status_code == 200, resp.text
    request = session.engine.submitted[0]
    assert request.product_type is ProductType.DELIVERY


def test_order_nrml_maps_to_margin() -> None:
    session = _FakeSession()
    resp = _post_order(session, "product-nrml", _order_body(product="NRML"))
    assert resp.status_code == 200, resp.text
    request = session.engine.submitted[0]
    assert request.product_type is ProductType.MARGIN


def test_order_absent_product_keeps_existing_default() -> None:
    """No regression for callers that predate the field: absent stays INTRADAY."""
    session = _FakeSession()
    body = _order_body()
    assert "product" not in body
    resp = _post_order(session, "product-absent", body)
    assert resp.status_code == 200, resp.text
    request = session.engine.submitted[0]
    assert request.product_type is ProductType.INTRADAY


def test_order_unknown_product_422_not_silent_default() -> None:
    """An unrecognised product is refused BEFORE the engine runs — falling back
    to the default would place the order on the wrong margin block."""
    session = _FakeSession()
    resp = _post_order(session, "product-garbage", _order_body(product="GARBAGE"))
    assert resp.status_code == 422, resp.text
    assert "GARBAGE" in resp.text
    assert session.engine.submitted == []


def test_order_product_ignored_for_nothing_else_in_body() -> None:
    """Adding `product` must not disturb the rest of the built request."""
    session = _FakeSession()
    resp = _post_order(
        session,
        "product-shape",
        _order_body(product="CNC", order_type="LIMIT", time_in_force="DAY"),
    )
    assert resp.status_code == 200, resp.text
    request = session.engine.submitted[0]
    assert request.product_type is ProductType.DELIVERY
    assert request.side is OrderSide.BUY
    assert request.order_type is OrderType.LIMIT
    assert request.time_in_force is TimeInForce.DAY
    assert request.price is not None and request.price.value == 2500


# ---------------------------------------------------------------------------
# POST /orders/bracket — the composite path honours the same mapping
# ---------------------------------------------------------------------------


def test_bracket_cnc_maps_to_delivery() -> None:
    session = _FakeSession()
    resp = _post_order(
        session, "bracket-product-cnc", _bracket_body(product="CNC"),
        path="/orders/bracket",
    )
    assert resp.status_code == 200, resp.text
    request = session.engine.submitted[0]
    assert isinstance(request, BracketOrderRequest)
    assert request.product_type is ProductType.DELIVERY


def test_bracket_nrml_maps_to_margin() -> None:
    session = _FakeSession()
    resp = _post_order(
        session, "bracket-product-nrml", _bracket_body(product="NRML"),
        path="/orders/bracket",
    )
    assert resp.status_code == 200, resp.text
    request = session.engine.submitted[0]
    assert request.product_type is ProductType.MARGIN


def test_bracket_absent_product_keeps_existing_default() -> None:
    session = _FakeSession()
    resp = _post_order(
        session, "bracket-product-absent", _bracket_body(), path="/orders/bracket",
    )
    assert resp.status_code == 200, resp.text
    request = session.engine.submitted[0]
    assert request.product_type is ProductType.INTRADAY


def test_bracket_unknown_product_422_not_silent_default() -> None:
    session = _FakeSession()
    resp = _post_order(
        session, "bracket-product-garbage", _bracket_body(product="GARBAGE"),
        path="/orders/bracket",
    )
    assert resp.status_code == 422, resp.text
    assert "GARBAGE" in resp.text
    assert session.engine.submitted == []
