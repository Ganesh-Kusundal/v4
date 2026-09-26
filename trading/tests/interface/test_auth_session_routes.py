"""Integration tests for /auth/* endpoints — B8 session auth routes.

Tests cover: GET /auth/session, POST /auth/login, POST /auth/logout.
Uses real InMemorySessionStore (no mocking) and TestClient cookies.
"""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tradex_trading.interface.fastapi_app import create_app  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(api_key: str | None = "test-key") -> TestClient:
    """Create a TestClient with an optional API key configured."""
    return TestClient(create_app(api_key=api_key), raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# GET /auth/session
# ---------------------------------------------------------------------------


def test_session_endpoint_unauthenticated_returns_not_authenticated() -> None:
    """No cookie → authenticated: false, session_auth_available: true."""
    client = _client()
    r = client.get("/auth/session")
    assert r.status_code == 200
    data = r.json()
    assert data["authenticated"] is False
    assert data["session_auth_available"] is True


def test_session_endpoint_after_login_returns_csrf_token() -> None:
    client = _client()
    login_r = client.post("/auth/login", json={"password": "test-key"})
    assert login_r.status_code == 200
    # The cookie is automatically carried by the TestClient
    session_r = client.get("/auth/session")
    data = session_r.json()
    assert data["authenticated"] is True
    assert data["subject"] == "operator"
    assert "csrf_token" in data
    assert len(data["csrf_token"]) == 64  # SHA-256 hex


def test_session_endpoint_csrf_token_is_stable_across_calls() -> None:
    """GET /auth/session is idempotent; same session → same CSRF token."""
    client = _client()
    client.post("/auth/login", json={"password": "test-key"})
    r1 = client.get("/auth/session")
    r2 = client.get("/auth/session")
    assert r1.json()["csrf_token"] == r2.json()["csrf_token"]


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------


def test_login_with_correct_password_returns_200() -> None:
    client = _client()
    r = client.post("/auth/login", json={"password": "test-key"})
    assert r.status_code == 200
    data = r.json()
    assert data["authenticated"] is True
    assert data["subject"] == "operator"
    assert "csrf_token" in data


def test_login_sets_httponly_cookie() -> None:
    client = _client()
    r = client.post("/auth/login", json={"password": "test-key"})
    # TestClient reflects the set-cookie header
    assert "tradex_session" in r.cookies or "tradex_session" in client.cookies


def test_login_with_wrong_password_returns_401() -> None:
    client = _client()
    r = client.post("/auth/login", json={"password": "wrong"})
    assert r.status_code == 401


def test_login_with_empty_password_returns_401() -> None:
    client = _client()
    r = client.post("/auth/login", json={"password": ""})
    assert r.status_code == 401


def test_login_with_bad_json_returns_400() -> None:
    client = _client()
    r = client.post(
        "/auth/login",
        content=b"not-json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


def test_login_without_api_key_configured_returns_503() -> None:
    """No credentials configured → can't log in."""
    client = _client(api_key=None)
    r = client.post("/auth/login", json={"password": "anything"})
    assert r.status_code == 503


def test_login_creates_distinct_sessions_per_call() -> None:
    """Two login calls produce two independent session cookies."""
    c1 = _client()
    c2 = _client()
    r1 = c1.post("/auth/login", json={"password": "test-key"})
    r2 = c2.post("/auth/login", json={"password": "test-key"})
    assert r1.json()["csrf_token"] != r2.json()["csrf_token"]


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------


def test_logout_clears_session() -> None:
    client = _client()
    client.post("/auth/login", json={"password": "test-key"})
    # Confirm authenticated
    assert client.get("/auth/session").json()["authenticated"] is True
    # Logout
    r = client.post("/auth/logout")
    assert r.status_code == 200
    assert r.json()["logged_out"] is True
    # Session should now be gone
    assert client.get("/auth/session").json()["authenticated"] is False


def test_logout_without_session_is_idempotent() -> None:
    """Logout when no session is present must not error."""
    client = _client()
    r = client.post("/auth/logout")
    assert r.status_code == 200
    assert r.json()["logged_out"] is True


def test_logout_revokes_session_in_store() -> None:
    """The session store must mark the record revoked after logout."""
    app = create_app(api_key="test-key")
    client = TestClient(app)
    client.post("/auth/login", json={"password": "test-key"})
    assert app.state.session_store.active_count() == 1
    client.post("/auth/logout")
    assert app.state.session_store.active_count() == 0
