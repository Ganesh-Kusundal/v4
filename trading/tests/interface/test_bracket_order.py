"""POST /orders/bracket, PUT + DELETE on bracket orders.

The bracket endpoint is session-bound and capability-gated: no session -> 503
(mirroring ``/orders``), a broker without super-order support -> 422, a
missing Idempotency-Key -> 422. Submissions go through the ExecutionEngine
pipeline (``session.engine.submit``) — the same idempotency/risk/OMS spine as
``/orders``. Cancelling or modifying a placed bracket also runs through the
engine, which dispatches to the venue's super-order endpoints. Tests inject a
fake session carrying a fake broker + fake engine so no live/auth dependency
is needed.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from tradex_domain.enums import (  # noqa: E402
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from tradex_domain.execution import (  # noqa: E402
    BracketOrderRequest,
    Order,
    OrderReceipt,
)
from tradex_domain.instruments import Equity  # noqa: E402
from tradex_domain.value_objects import (  # noqa: E402
    CorrelationId,
    OrderId,
    Price,
    Quantity,
)

from tradex_trading.interface.fastapi_app import create_app  # noqa: E402


def _bracket_body(**overrides: Any) -> dict:
    body = {
        "instrument_id": "NSE:RELIANCE",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 10,
        "price": "2500.00",
        "stop_loss_price": "2450.00",
        "target_price": "2600.00",
    }
    body.update(overrides)
    return body


def _bracket_order(order_id: str = "bracket-9") -> Order:
    """An ACK'd bracket order as the engine cache would hold it."""
    return Order(
        order_id=OrderId(value=order_id),
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500.00")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.ACK,
        stop_loss_price=Price(value=Decimal("2450.00")),
        target_price=Price(value=Decimal("2600.00")),
        trailing_jump=Price(value=Decimal("0")),
    )


def _client() -> TestClient:
    return TestClient(create_app(session=None))


class _FakeCaps:
    supports_super_order = True


class _NoSuperCaps:
    supports_super_order = False


class _FakeBroker:
    def __init__(self, *, supports_super_order: bool = True) -> None:
        self.capabilities = _FakeCaps() if supports_super_order else _NoSuperCaps()


class _FakeEngine:
    """Records engine calls; canned results for submit/get_order/modify/cancel."""

    def __init__(
        self,
        result: Any = None,
        existing: Order | None = None,
    ) -> None:
        self.submitted: list[BracketOrderRequest] = []
        self.modified: list[tuple[OrderId, BracketOrderRequest]] = []
        self.cancelled: list[OrderId] = []
        self.existing = existing
        self._result = result or OrderReceipt(
            order_id=OrderId(value="bracket-1"),
            status=OrderStatus.SUBMITTED,
            message="submitted",
        )

    def submit(self, request: BracketOrderRequest) -> Any:
        self.submitted.append(request)
        return self._result

    def get_order(self, order_id: OrderId) -> Order | None:
        if self.existing is not None and self.existing.order_id == order_id:
            return self.existing
        return None

    def modify(self, order_id: OrderId, request: BracketOrderRequest) -> Order:
        self.modified.append((order_id, request))
        assert self.existing is not None
        return replace(
            self.existing,
            price=request.price,
            stop_loss_price=request.stop_loss_price,
            target_price=request.target_price,
            trailing_jump=request.trailing_jump,
            quantity=request.quantity,
        )

    def cancel(
        self, order_id: OrderId, correlation_id: object | None = None,
    ) -> Order:
        self.cancelled.append(order_id)
        assert self.existing is not None
        return replace(self.existing, status=OrderStatus.CANCELLED)


class _FakeSession:
    def __init__(
        self,
        engine: _FakeEngine | None = None,
        broker: _FakeBroker | None = None,
    ) -> None:
        self._broker = broker or _FakeBroker()
        self.engine = engine or _FakeEngine()


def _post_bracket(client: TestClient, key: str, body: dict | None = None) -> Any:
    return client.post(
        "/orders/bracket",
        json=body if body is not None else _bracket_body(),
        headers={"Idempotency-Key": key},
    )


# ---------------------------------------------------------------------------
# POST /orders/bracket — placement through the engine pipeline
# ---------------------------------------------------------------------------


def test_bracket_no_session_503() -> None:
    client = _client()
    resp = _post_bracket(client, "bracket-no-session")
    assert resp.status_code == 503, resp.text


def test_bracket_requires_idempotency_key() -> None:
    session = _FakeSession()
    client = TestClient(create_app(session=session))
    resp = client.post("/orders/bracket", json=_bracket_body())
    assert resp.status_code == 422, resp.text
    assert session.engine.submitted == []


def test_bracket_rejects_missing_protective_prices() -> None:
    session = _FakeSession()
    client = TestClient(create_app(session=session))
    body = _bracket_body()
    del body["stop_loss_price"]
    del body["target_price"]
    headers = {"Idempotency-Key": "bracket-missing-protection"}
    resp = client.post("/orders/bracket", json=body, headers=headers)
    assert resp.status_code == 422, resp.text
    assert session.engine.submitted == []


def test_bracket_rejects_invalid_protective_ordering() -> None:
    """Domain-side validation refuses stop-above-entry before the engine runs."""
    session = _FakeSession()
    client = TestClient(create_app(session=session))
    body = _bracket_body(price="2500.00", stop_loss_price="2600.00", target_price="2700.00")
    headers = {"Idempotency-Key": "bracket-bad-order"}
    resp = client.post("/orders/bracket", json=body, headers=headers)
    assert resp.status_code == 422, resp.text
    assert session.engine.submitted == []


def test_bracket_unsupported_broker_422_before_engine() -> None:
    """A broker without super-order capability rejects before the engine sees it."""
    session = _FakeSession(broker=_FakeBroker(supports_super_order=False))
    client = TestClient(create_app(session=session))
    resp = _post_bracket(client, "bracket-unsupported")
    assert resp.status_code == 422, resp.text
    assert "does not support super orders" in resp.text
    assert session.engine.submitted == []


def test_bracket_routes_through_engine_pipeline() -> None:
    """The route delegates to engine.submit with a composite request stamped
    with the Idempotency-Key correlation id — no direct broker call."""
    session = _FakeSession()
    client = TestClient(create_app(session=session))
    resp = _post_bracket(client, "bracket-key-1")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "order_id": "bracket-1",
        "status": "SUBMITTED",
        "message": "submitted",
    }
    assert len(session.engine.submitted) == 1
    request = session.engine.submitted[0]
    assert isinstance(request, BracketOrderRequest)
    assert request.correlation_id == CorrelationId(value="bracket-key-1")
    assert request.price is not None
    assert request.stop_loss_price is not None
    assert request.target_price is not None


def test_bracket_idempotent_replay_returns_original_id() -> None:
    """A completed-key replay surfaces the original OrderId from the guard —
    the same contract as POST /orders."""
    session = _FakeSession(engine=_FakeEngine(result=OrderId(value="bracket-original")))
    client = TestClient(create_app(session=session))
    resp = _post_bracket(client, "bracket-replay")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "order_id": "bracket-original",
        "status": "SUBMITTED",
        "message": "idempotency_replay",
    }
    assert len(session.engine.submitted) == 1


def test_bracket_risk_rejection_is_surfaced() -> None:
    """A risk-rejected bracket returns the engine receipt (status REJECTED)
    so clients can tell a refused bracket from a placed one."""
    session = _FakeSession(
        engine=_FakeEngine(
            result=OrderReceipt(
                order_id=OrderId(value="rejected"),
                status=OrderStatus.REJECTED,
                message="risk_check_failed",
            )
        )
    )
    client = TestClient(create_app(session=session))
    resp = _post_bracket(client, "bracket-risk")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "order_id": "rejected",
        "status": "REJECTED",
        "message": "risk_check_failed",
    }


# ---------------------------------------------------------------------------
# PUT /orders/{id} — modifying a bracket through the engine
# ---------------------------------------------------------------------------


def test_bracket_modify_sends_composite_with_defaults() -> None:
    """PUT on a bracket order builds a BracketOrderRequest (entry + legs) so
    the engine dispatches to modify_super_order; omitted fields default to the
    current order values."""
    existing = _bracket_order()
    session = _FakeSession(engine=_FakeEngine(existing=existing))
    client = TestClient(create_app(session=session))

    resp = client.put(
        "/orders/bracket-9",
        json={"target_price": "2700.00"},
        headers={"Idempotency-Key": "bracket-modify-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["order_id"] == "bracket-9"
    assert body["status"] == "modified"

    assert len(session.engine.modified) == 1
    order_id, request = session.engine.modified[0]
    assert order_id == OrderId(value="bracket-9")
    assert isinstance(request, BracketOrderRequest)
    # Only target_price changed; the rest carried forward from the order.
    assert request.price == Price(value=Decimal("2500.00"))
    assert request.stop_loss_price == Price(value=Decimal("2450.00"))
    assert request.target_price == Price(value=Decimal("2700.00"))
    assert request.quantity.value == Decimal("10")
    assert request.correlation_id == CorrelationId(value="bracket-modify-1")


def test_bracket_modify_invalid_ordering_422() -> None:
    """A bracket modify that breaks the side-aware protective ordering is
    refused (422) before the engine runs."""
    existing = _bracket_order()
    session = _FakeSession(engine=_FakeEngine(existing=existing))
    client = TestClient(create_app(session=session))

    resp = client.put(
        "/orders/bracket-9",
        json={"target_price": "2400.00"},  # below stop 2450 — invalid for BUY
        headers={"Idempotency-Key": "bracket-modify-bad"},
    )
    assert resp.status_code == 422, resp.text
    assert session.engine.modified == []


# ---------------------------------------------------------------------------
# DELETE /orders/{id} — cancelling a bracket through the engine
# ---------------------------------------------------------------------------


def test_bracket_delete_cancels_via_engine() -> None:
    """DELETE on a bracket order runs through engine.cancel (which reaches the
    venue's cancel_super_order) and returns the cancelled record."""
    existing = _bracket_order()
    session = _FakeSession(engine=_FakeEngine(existing=existing))
    client = TestClient(create_app(session=session))

    resp = client.delete(
        "/orders/bracket-9",
        headers={"Idempotency-Key": "bracket-cancel-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["order_id"] == "bracket-9"
    assert body["status"] == "cancelled"
    assert session.engine.cancelled == [OrderId(value="bracket-9")]
