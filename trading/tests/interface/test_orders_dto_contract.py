"""Spec 0003 tranche 1a — order mutation DTO contract tests."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tradex_interfaces.fastapi_app import create_app  # noqa: E402


def _client():
    from unittest.mock import MagicMock

    session = MagicMock()
    session.state = "READY"
    session.mode = "paper"
    session.engine = MagicMock()
    session.engine.all_orders.return_value = []
    return TestClient(create_app(session=session))


def test_missing_quantity_rejected():
    r = _client().post(
        "/orders",
        json={"instrument_id": "NSE:RELIANCE", "side": "BUY"},
        headers={"Idempotency-Key": "k1"},
    )
    assert r.status_code == 422


def test_extra_keys_rejected():
    r = _client().post(
        "/orders",
        json={"instrument_id": "NSE:RELIANCE", "side": "BUY", "quantity": 1, "foo": 1},
        headers={"Idempotency-Key": "k2"},
    )
    assert r.status_code == 422


def test_bracket_extra_keys_rejected():
    r = _client().post(
        "/orders/bracket",
        json={
            "instrument_id": "NSE:RELIANCE",
            "side": "BUY",
            "quantity": 1,
            "stop_loss_price": "2450",
            "target_price": "2600",
            "bar": 1,
        },
        headers={"Idempotency-Key": "k3"},
    )
    assert r.status_code == 422


def test_live_order_gate_disabled():
    from unittest.mock import MagicMock

    session = MagicMock()
    session.state = "READY"
    session.mode = "live"
    session.engine = MagicMock()
    session.engine.all_orders.return_value = []
    session._live_orders_enabled = False
    client = TestClient(create_app(session=session))
    r = client.post(
        "/orders",
        json={"instrument_id": "NSE:RELIANCE", "side": "BUY", "quantity": 1},
        headers={"Idempotency-Key": "live-gate"},
    )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "live_orders_disabled"


def test_live_order_gate_enabled():
    from unittest.mock import MagicMock

    session = MagicMock()
    session.state = "READY"
    session.mode = "live"
    session.engine = MagicMock()
    session.engine.all_orders.return_value = []
    session._live_orders_enabled = True
    session.engine.submit.return_value = MagicMock(
        order_id=MagicMock(value="o1"), status="SUBMITTED", message="ok"
    )
    client = TestClient(create_app(session=session))
    app = client.app
    app.state.live_orders_enabled = True
    r = client.post(
        "/orders",
        json={"instrument_id": "NSE:RELIANCE", "side": "BUY", "quantity": 1},
        headers={"Idempotency-Key": "live-gate-ok"},
    )
    # reaches engine (may fail later but not 503 live_orders_disabled)
    assert r.status_code != 503 or r.json()["error"]["code"] != "live_orders_disabled"