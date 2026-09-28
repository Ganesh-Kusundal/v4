"""AC-12: every broker WebSocket connect carries a ping/pong keepalive.

A TCP half-open socket blocks ``recv()`` forever with no exception, so the
library-level keepalive is the only thing that turns a silently dead socket
into a reconnect. Both providers expose exactly one ``default_ws_factory``
that every one of their sockets (Dhan order/market/depth, Upstox
portfolio/market) routes through, so asserting on the factory pins the whole
set.

The assertion is on the kwargs actually handed to ``websockets.sync.client
.connect`` — not on a constant — so a later edit that drops the ping params
fails here rather than silently shipping a socket with no heartbeat.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from tradex_brokers.common.ws_shared import WS_PING_INTERVAL, WS_PING_TIMEOUT
from tradex_brokers.dhan import ws_streams as dhan_ws
from tradex_brokers.upstox import ws_streams as upstox_ws


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    def send(self, data: str) -> None:
        self.sent.append(data)

    def recv(self, *args: Any, **kwargs: Any) -> str:
        return "{}"

    def ping(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def recorded_connect(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch ``websockets.sync.client.connect`` and record every call's kwargs.

    The broker factories do ``from websockets.sync.client import connect``
    *inside* the function body, so patching the module attribute is enough
    for the import to pick up the double.
    """
    calls: list[dict[str, Any]] = []

    def _connect(url: str, **kwargs: Any) -> _FakeSocket:
        calls.append({"url": url, **kwargs})
        return _FakeSocket()

    client = types.ModuleType("websockets.sync.client")
    client.connect = _connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "websockets.sync.client", client)
    return calls


@pytest.mark.parametrize(
    "factory",
    [dhan_ws.default_ws_factory, upstox_ws.default_ws_factory],
    ids=["dhan", "upstox"],
)
def test_default_ws_factory_sends_ping_and_timeout(
    factory: Any, recorded_connect: list[dict[str, Any]]
) -> None:
    factory("wss://example.invalid/feed")

    assert len(recorded_connect) == 1
    call = recorded_connect[0]
    assert call["ping_interval"] == WS_PING_INTERVAL == 20.0
    assert call["ping_timeout"] == WS_PING_TIMEOUT == 20.0


@pytest.mark.parametrize(
    "factory",
    [dhan_ws.default_ws_factory, upstox_ws.default_ws_factory],
    ids=["dhan", "upstox"],
)
def test_default_ws_factory_keeps_max_size(
    factory: Any, recorded_connect: list[dict[str, Any]]
) -> None:
    """The heartbeat must be additive — the existing frame cap stays."""
    factory("wss://example.invalid/feed")

    assert recorded_connect[0]["max_size"] == 2**20


@pytest.mark.parametrize(
    "factory",
    [dhan_ws.default_ws_factory, upstox_ws.default_ws_factory],
    ids=["dhan", "upstox"],
)
def test_default_ws_factory_does_not_silence_heartbeat(
    factory: Any, recorded_connect: list[dict[str, Any]]
) -> None:
    """Negative control: ping_timeout=0/None would disable pong checking.

    ``ping_timeout=None`` tells the library to wait forever for a pong — the
    exact half-open behaviour this lane exists to prevent — so the positive
    value is pinned explicitly rather than merely "present".
    """
    factory("wss://example.invalid/feed")

    call = recorded_connect[0]
    assert call["ping_timeout"] not in (None, 0)
    assert call["ping_interval"] not in (None, 0)
