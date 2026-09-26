"""WebSocket authentication via session cookie — B8.

Covers the new session-cookie path alongside the legacy ?api_key= path.
stream.py is NOT touched; auth is validated in fastapi_app's wrapper.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from fastapi.websockets import WebSocketDisconnect  # noqa: E402

from tradex_trading.interface.fastapi_app import create_app  # noqa: E402
from tradex_trading.reactive.bus import ReactiveBus  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _app(api_key: str | None = None) -> object:
    bus = ReactiveBus()
    session = MagicMock()
    session.state = "READY"
    session.bus = bus
    return create_app(session=session, api_key=api_key)


def _logged_in_client(api_key: str = "secret") -> TestClient:
    """Return a TestClient that has an active session cookie."""
    client = TestClient(_app(api_key))
    r = client.post("/auth/login", json={"password": api_key})
    assert r.status_code == 200, r.text
    return client


# ---------------------------------------------------------------------------
# Dev/paper mode — no auth configured
# ---------------------------------------------------------------------------


def test_ws_no_auth_configured_allows_connect() -> None:
    """Without api_key the WS is open to any client (dev/paper mode)."""
    with TestClient(_app()).websocket_connect("/ws/stream") as ws:
        ws.send_json({"type": "subscribe_bars",
                      "bars": [{"instrument": "NSE:TEST", "interval": "1m"}]})
        resp = ws.receive_json()
        assert resp["type"] == "subscribed_bars"


# ---------------------------------------------------------------------------
# Legacy ?api_key= path (unchanged behaviour)
# ---------------------------------------------------------------------------


def test_ws_api_key_configured_accepts_correct_query_param() -> None:
    with TestClient(_app("secret")).websocket_connect(
        "/ws/stream?api_key=secret"
    ) as ws:
        ws.send_json({"type": "subscribe_bars",
                      "bars": [{"instrument": "NSE:TEST", "interval": "1m"}]})
        assert ws.receive_json()["type"] == "subscribed_bars"


def test_ws_api_key_configured_rejects_missing_query_param() -> None:
    with pytest.raises(WebSocketDisconnect):
        with TestClient(_app("secret")).websocket_connect("/ws/stream") as ws:
            ws.receive_json()


def test_ws_api_key_configured_rejects_wrong_query_param() -> None:
    with pytest.raises(WebSocketDisconnect):
        with TestClient(_app("secret")).websocket_connect(
            "/ws/stream?api_key=wrong"
        ) as ws:
            ws.receive_json()


# ---------------------------------------------------------------------------
# New session-cookie path
# ---------------------------------------------------------------------------


def test_ws_session_cookie_allows_connect() -> None:
    """A session cookie from POST /auth/login must authenticate the WS."""
    client = _logged_in_client("secret")
    with client.websocket_connect("/ws/stream") as ws:
        ws.send_json({"type": "subscribe_bars",
                      "bars": [{"instrument": "NSE:TEST", "interval": "1m"}]})
        resp = ws.receive_json()
        assert resp["type"] == "subscribed_bars"


def test_ws_invalid_session_cookie_is_rejected() -> None:
    """A tampered or expired cookie must be rejected."""
    client = TestClient(_app("secret"))
    # Plant a fake session cookie manually
    client.cookies.set("tradex_session", "not-a-real-session-id")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/stream") as ws:
            ws.receive_json()


def test_ws_after_logout_session_cookie_is_rejected() -> None:
    """A revoked session must not reopen a WS connection."""
    client = _logged_in_client("secret")
    # Confirm cookie connects fine before logout
    with client.websocket_connect("/ws/stream") as ws:
        ws.send_json({"type": "subscribe_bars",
                      "bars": [{"instrument": "NSE:TEST", "interval": "1m"}]})
        assert ws.receive_json()["type"] == "subscribed_bars"
    # Revoke via logout
    client.post("/auth/logout")
    # Now the same cookie should be rejected
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/stream") as ws:
            ws.receive_json()
