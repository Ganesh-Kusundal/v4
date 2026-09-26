"""CSRF enforcement on order mutations — B8.

Verifies that:
  - Session-authenticated order mutations require a valid X-CSRF-Token.
  - API-key-authenticated mutations (legacy path) are exempt from CSRF.
  - Safe methods (GET) are never CSRF-checked.
  - Wrong origin and missing/wrong token produce 403.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tradex_trading.interface.fastapi_app import create_app  # noqa: E402
from tradex_trading.reactive.bus import ReactiveBus  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_client(api_key: str = "test-key") -> tuple[TestClient, str]:
    """Return (TestClient, csrf_token) after a successful login.

    The client's cookie jar carries the session cookie for subsequent calls.
    """
    app = _make_app(api_key)
    client = TestClient(app, raise_server_exceptions=True)
    r = client.post("/auth/login", json={"password": api_key})
    assert r.status_code == 200
    return client, r.json()["csrf_token"]


def _make_app(api_key: str | None = "test-key") -> object:
    bus = ReactiveBus()
    session = MagicMock()
    session.state = "READY"
    session.bus = bus
    session.mode = "paper"
    session.engine = MagicMock()
    session.engine.all_orders.return_value = []
    return create_app(session=session, api_key=api_key)


# ---------------------------------------------------------------------------
# GET (safe method) — never CSRF-gated
# ---------------------------------------------------------------------------


def test_get_orders_with_session_requires_no_csrf() -> None:
    client, _ = _session_client()
    r = client.get("/orders")
    assert r.status_code == 200  # no CSRF token needed for GET


# ---------------------------------------------------------------------------
# POST /orders — session path requires CSRF
# ---------------------------------------------------------------------------


def test_post_orders_with_session_and_valid_csrf_passes_auth_gate() -> None:
    """A correctly-formed order mutation with CSRF token must reach the handler.
    The handler may fail (503/422 — no real session), but NOT with 403.
    """
    client, csrf = _session_client()
    r = client.post(
        "/orders",
        json={
            "instrument_id": "NSE:RELIANCE",
            "side": "BUY",
            "quantity": 1,
            "order_type": "MARKET",
        },
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "k-valid-csrf",
        },
    )
    # 503 or similar from business logic is fine; 403 is the auth failure
    assert r.status_code != 403


def test_post_orders_with_session_and_missing_csrf_returns_403() -> None:
    client, _ = _session_client()
    r = client.post(
        "/orders",
        json={"instrument_id": "NSE:RELIANCE", "side": "BUY", "quantity": 1},
        headers={"Idempotency-Key": "k-no-csrf"},
        # No X-CSRF-Token
    )
    assert r.status_code == 403


def test_post_orders_with_session_and_wrong_csrf_returns_403() -> None:
    client, _ = _session_client()
    r = client.post(
        "/orders",
        json={"instrument_id": "NSE:RELIANCE", "side": "BUY", "quantity": 1},
        headers={"X-CSRF-Token": "wrong-token", "Idempotency-Key": "k-wrong-csrf"},
    )
    assert r.status_code == 403


def test_post_orders_with_session_and_wrong_origin_returns_403() -> None:
    client, csrf = _session_client()
    r = client.post(
        "/orders",
        json={"instrument_id": "NSE:RELIANCE", "side": "BUY", "quantity": 1},
        headers={
            "X-CSRF-Token": csrf,
            "Origin": "https://evil.example.com",
            "Idempotency-Key": "k-bad-origin",
        },
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /orders — legacy API-key path is CSRF-exempt
# ---------------------------------------------------------------------------


def test_post_orders_with_api_key_requires_no_csrf() -> None:
    """API-key callers are programmatic; no CSRF enforcement."""
    app = _make_app("test-key")
    client = TestClient(app)
    r = client.post(
        "/orders",
        json={
            "instrument_id": "NSE:RELIANCE",
            "side": "BUY",
            "quantity": 1,
            "order_type": "MARKET",
        },
        headers={
            "X-API-Key": "test-key",
            "Idempotency-Key": "k-apikey",
        },
    )
    # Must not be 403 (auth/CSRF rejection)
    assert r.status_code != 403


# ---------------------------------------------------------------------------
# DELETE /orders/{id} — CSRF enforced on DELETE as well
# ---------------------------------------------------------------------------


def test_delete_order_with_session_and_missing_csrf_returns_403() -> None:
    client, _ = _session_client()
    r = client.delete(
        "/orders/some-id",
        headers={"Idempotency-Key": "k-del"},
        # No CSRF token
    )
    assert r.status_code == 403


def test_delete_order_with_session_and_valid_csrf_passes_auth_gate() -> None:
    client, csrf = _session_client()
    r = client.delete(
        "/orders/some-id",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "k-del-ok"},
    )
    # 404/422 from business logic OK; 403 is the auth failure
    assert r.status_code != 403


# ---------------------------------------------------------------------------
# No auth configured — CSRF never enforced (dev/paper mode)
# ---------------------------------------------------------------------------


def test_post_orders_without_any_auth_requires_no_csrf() -> None:
    """When no api_key is configured, CSRF is never enforced."""
    app = _make_app(api_key=None)
    client = TestClient(app)
    r = client.post(
        "/orders",
        json={
            "instrument_id": "NSE:RELIANCE",
            "side": "BUY",
            "quantity": 1,
            "order_type": "MARKET",
        },
        headers={"Idempotency-Key": "k-noauth"},
    )
    assert r.status_code != 403
