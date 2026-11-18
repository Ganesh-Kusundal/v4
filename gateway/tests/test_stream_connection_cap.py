from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from gateway_support import FakeBroker, gateway_config
from starlette.websockets import WebSocketDisconnect
from tradex_gateway.app import create_app
from tradex_gateway.brokers import BrokerRuntime
from tradex_gateway.config import GatewayConfig


def _client(cap: int) -> TestClient:
    app = create_app(
        gateway_config(max_ws_connections=cap),
        {
            "dhan": BrokerRuntime("dhan", FakeBroker("dhan")),
            "upstox": BrokerRuntime("upstox", FakeBroker("upstox")),
        },
    )
    return TestClient(app)


def _refused(client: TestClient) -> int:
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/v1/stream"):
            pass
    return exc.value.code


def test_ws_refused_beyond_cap():
    with _client(2) as client:
        with client.websocket_connect("/v1/stream"), client.websocket_connect("/v1/stream"):
            assert _refused(client) == 1013


def test_ws_slot_freed_on_disconnect():
    with _client(1) as client:
        with client.websocket_connect("/v1/stream"):
            assert _refused(client) == 1013
        with client.websocket_connect("/v1/stream") as ws:
            assert ws.receive_json()["op"] == "status"


def test_max_ws_connections_env_and_validation():
    cfg = GatewayConfig.from_env({"GATEWAY_MAX_WS_CONNECTIONS": "7", "GATEWAY_ALLOW_ANON": "1"})
    assert cfg.max_ws_connections == 7
    assert GatewayConfig().max_ws_connections == 100
    with pytest.raises(ValueError, match="GATEWAY_MAX_WS_CONNECTIONS must be > 0"):
        GatewayConfig(allow_anon=True, max_ws_connections=0).validate()
