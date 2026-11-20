"""Smoke over a real uvicorn socket: what TestClient's in-process transport cannot show.

TestClient never opens a port, so handshake rejection codes, concurrent HTTP
clients and WS close behaviour are only exercised here.
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest
import uvicorn
import websockets
from gateway_support import FakeBroker, gateway_config
from tradex_gateway.app import create_app
from tradex_gateway.brokers import BrokerRuntime


@pytest.fixture
def server():
    app = create_app(
        gateway_config(max_ws_connections=2),
        {
            "dhan": BrokerRuntime("dhan", FakeBroker("dhan")),
            "upstox": BrokerRuntime("upstox", FakeBroker("upstox")),
        },
    )
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        if not thread.is_alive():
            pytest.skip("cannot bind a local port here (sandbox?)")
        if time.monotonic() > deadline:
            pytest.fail("uvicorn did not start")
        time.sleep(0.02)
    port = srv.servers[0].sockets[0].getsockname()[1]
    yield f"127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


def test_concurrent_health_requests_all_succeed(server):
    async def run():
        async with httpx.AsyncClient(base_url=f"http://{server}") as client:
            return await asyncio.gather(*(client.get("/v1/health") for _ in range(100)))

    responses = asyncio.run(run())
    assert {r.status_code for r in responses} == {200}


def test_ws_cap_refusal_is_an_http_403_handshake_rejection(server):
    async def run():
        async with websockets.connect(f"ws://{server}/v1/stream") as a:
            async with websockets.connect(f"ws://{server}/v1/stream") as b:
                await a.recv()
                await b.recv()
                with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
                    async with websockets.connect(f"ws://{server}/v1/stream"):
                        pass
                return exc.value.response.status_code

    assert asyncio.run(run()) == 403


def test_ws_slot_is_freed_after_client_closes(server):
    async def run():
        for _ in range(5):
            async with websockets.connect(f"ws://{server}/v1/stream") as ws:
                assert "status" in await ws.recv()

    asyncio.run(run())
