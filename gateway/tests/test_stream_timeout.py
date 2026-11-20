from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient
from gateway_support import FakeBroker, gateway_config
from tradex_gateway.app import create_app
from tradex_gateway.brokers import BrokerRuntime


def _app(**cfg):
    return create_app(
        gateway_config(**cfg),
        {
            "dhan": BrokerRuntime("dhan", FakeBroker("dhan")),
            "upstox": BrokerRuntime("upstox", FakeBroker("upstox")),
        },
    )


def test_stalled_subscribe_yields_error_frame_and_socket_stays_usable():
    app = _app(request_timeout=0.2)
    with TestClient(app) as client:
        hub = app.state.hub
        hub.subscribe = lambda *a, **k: time.sleep(1.0)
        with client.websocket_connect("/v1/stream") as ws:
            assert ws.receive_json()["op"] == "status"
            ws.send_json({"op": "subscribe", "instruments": ["NSE:RELIANCE"], "mode": "quote"})
            frame = ws.receive_json()
            assert frame["op"] == "error"
            assert frame["code"] == "broker_unavailable"
            ws.send_json({"op": "ping"})
            assert ws.receive_json()["op"] == "pong"


def test_remove_client_not_dropped_when_request_pool_saturated():
    app = _app(request_timeout=0.2, broker_workers=1)
    release = threading.Event()
    with TestClient(app) as client:
        hub = app.state.hub
        with client.websocket_connect("/v1/stream") as ws:
            assert ws.receive_json()["op"] == "status"
            app.state.executor.submit(release.wait)  # occupy the only worker
            assert hub.client_count() == 1
        # disconnected: a bounded remove would be cancelled while queued
        time.sleep(0.5)
        release.set()
        deadline = time.time() + 3
        while hub.client_count() and time.time() < deadline:
            time.sleep(0.05)
        assert hub.client_count() == 0
