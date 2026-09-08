"""N3 (P2) — order mutations are rejected while the session is in replay mode.

The principal-quant review (architecture-flow-verification-2026-09-04, N3)
found that during WS tick-replay on a live session, POST /orders executed
against the live account while the chart showed historical bars. This is
the pragmatic interim gate: the four mutation routes preflight
``session.mode`` and reject replay-mode submissions server-side — the
frontend pill is never trusted.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tradex_trading.interface.fastapi_app import create_app


class _OID:
    def __init__(self, value: str) -> None:
        self.value = value


class _Status:
    value = "ACK"


class _Receipt:
    message = "submitted"
    status = _Status()
    order_id = _OID("ord-1")


class _Order:
    order_id = _OID("ord-1")
    status = _Status()
    price = None
    quantity = None
    side = None
    order_type = None
    time_in_force = None
    stop_loss_price = None
    target_price = None


class _FakeEngine:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def submit(self, request: object) -> Any:
        self.calls.append("submit")
        return _Receipt()

    def modify(self, order_id: object, request: object) -> Any:
        self.calls.append("modify")
        return _Order()

    def cancel(self, order_id: object, correlation_id: object | None = None) -> Any:
        self.calls.append("cancel")
        return _Order()

    def get_order(self, order_id: object) -> Any:
        return _Order()


class _FakeSession:
    def __init__(self, mode: str | None) -> None:
        self.mode = mode
        self.engine = _FakeEngine()
        self._broker = None


def _client(mode: str | None) -> TestClient:
    return TestClient(create_app(session=_FakeSession(mode)))


_ORDER_BODY = {
    "exchange": "NSE",
    "symbol": "RELIANCE",
    "side": "BUY",
    "order_type": "LIMIT",
    "quantity": "10",
    "price": "2500",
}


def test_replay_mode_rejects_all_mutations() -> None:
    """Contract 1: replay session refuses every mutation without the engine."""
    session = _FakeSession("replay")
    client = TestClient(create_app(session=session))

    post = client.post("/orders", json=_ORDER_BODY, headers={"Idempotency-Key": "k1"})
    bracket = client.post(
        "/orders/bracket",
        json={**_ORDER_BODY, "stop_loss_price": "2400", "target_price": "2600"},
        headers={"Idempotency-Key": "k2"},
    )
    put = client.put(
        "/orders/ord-1", json={"quantity": "20"}, headers={"Idempotency-Key": "k3"}
    )
    delete = client.delete("/orders/ord-1", headers={"Idempotency-Key": "k4"})

    for resp in (post, bracket, put, delete):
        assert resp.status_code == 422, resp.text
        assert "orders are disabled during replay" in resp.json()["error"]["message"]
    assert session.engine.calls == []


@pytest.mark.parametrize("mode", ["paper", "live", "backtest", None])
def test_non_replay_modes_pass_through(mode: str | None) -> None:
    """Contract 2+3: paper/live/backtest (and unknown) modes are unchanged."""
    session = _FakeSession(mode)
    client = TestClient(create_app(session=session))

    resp = client.post(
        "/orders", json=_ORDER_BODY, headers={"Idempotency-Key": "k1"}
    )
    assert resp.status_code == 200, resp.text
    assert session.engine.calls == ["submit"]