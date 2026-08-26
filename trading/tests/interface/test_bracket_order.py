"""POST /orders/bracket — super-order (bracket) placement endpoint.

The endpoint is session-bound and capability-gated: no session -> 503
(mirroring ``/orders``), a broker without super-order support -> 422.
Tests inject a fake session carrying a fake broker so no live/auth
dependency is needed.
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from tradex_domain.value_objects import OrderId  # noqa: E402

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


def _client() -> TestClient:
    return TestClient(create_app(session=None))


class _FakeCaps:
    supports_super_order = True


class _FakeBroker:
    capabilities = _FakeCaps()

    def __init__(self) -> None:
        self.submitted: list[Any] = []

    def submit_super_order(self, request: Any) -> OrderId:
        self.submitted.append(request)
        return OrderId(value="bracket-1")


class _FakeSession:
    def __init__(self) -> None:
        self._broker = _FakeBroker()


def test_bracket_no_session_503() -> None:
    client = _client()
    resp = client.post("/orders/bracket", json=_bracket_body())
    assert resp.status_code == 503, resp.text


def test_bracket_rejects_missing_protective_prices() -> None:
    session = _FakeSession()
    client = TestClient(create_app(session=session))
    body = _bracket_body()
    del body["stop_loss_price"]
    del body["target_price"]
    resp = client.post("/orders/bracket", json=body)
    assert resp.status_code == 422, resp.text


def test_bracket_places_via_fake_broker() -> None:
    session = _FakeSession()
    client = TestClient(create_app(session=session))
    resp = client.post("/orders/bracket", json=_bracket_body())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"order_id": "bracket-1"}
    assert len(session._broker.submitted) == 1
