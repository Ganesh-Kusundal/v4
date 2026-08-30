"""WebSocket authentication — /ws/stream must refuse anonymous connections
when the API key is configured.

Browsers cannot set custom headers on a WebSocket, so the key travels as the
``?api_key=`` query parameter. When no key is configured (paper/dev) the WS
stays open as before.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from fastapi.websockets import WebSocketDisconnect  # noqa: E402

from tradex_trading.interface.fastapi_app import create_app  # noqa: E402
from tradex_trading.reactive.bus import ReactiveBus  # noqa: E402


def _app(api_key: str | None = None) -> object:
    bus = ReactiveBus()
    session = MagicMock()
    session.state = "READY"
    session.bus = bus
    return create_app(session=session, api_key=api_key)


def test_ws_no_key_configured_allows_connect() -> None:
    with TestClient(_app()).websocket_connect("/ws/stream") as ws:
        ws.send_json({"type": "subscribe_bars",
                      "bars": [{"instrument": "NSE:TEST", "interval": "1m"}]})
        resp = ws.receive_json()
        assert resp["type"] == "subscribed_bars"


def test_ws_configured_rejects_missing_key() -> None:
    with pytest.raises(WebSocketDisconnect):
        with TestClient(_app("secret-key")).websocket_connect("/ws/stream") as ws:
            ws.receive_json()  # pragma: no cover


def test_ws_configured_rejects_wrong_key() -> None:
    with pytest.raises(WebSocketDisconnect):
        with TestClient(_app("secret-key")).websocket_connect(
            "/ws/stream?api_key=wrong"
        ) as ws:
            ws.receive_json()  # pragma: no cover


def test_ws_configured_accepts_correct_key() -> None:
    with TestClient(_app("secret-key")).websocket_connect(
        "/ws/stream?api_key=secret-key"
    ) as ws:
        ws.send_json({"type": "subscribe_bars",
                      "bars": [{"instrument": "NSE:TEST", "interval": "1m"}]})
        resp = ws.receive_json()
        assert resp["type"] == "subscribed_bars"