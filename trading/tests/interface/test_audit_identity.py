"""Audit identity on money commands — B8.

Verifies that order mutations emit a structured audit log entry containing
the caller identity ('session:<subject>', 'api_key', or 'anonymous').
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

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
    session.mode = "paper"
    session.engine = MagicMock()
    session.engine.all_orders.return_value = []
    return create_app(session=session, api_key=api_key)


def _post_order(client: TestClient, extra_headers: dict | None = None) -> object:
    headers = {"Idempotency-Key": "audit-test-key"}
    if extra_headers:
        headers.update(extra_headers)
    return client.post(
        "/orders",
        json={"instrument_id": "NSE:RELIANCE", "side": "BUY", "quantity": 1},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Audit log contains identity
# ---------------------------------------------------------------------------


def test_api_key_auth_logs_api_key_identity(caplog: pytest.LogCaptureFixture) -> None:
    app = _app("test-key")
    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="tradex_interfaces.routes.orders"):
        _post_order(client, {"X-API-Key": "test-key"})
    audit_lines = [r.message for r in caplog.records if "audit.order" in r.message]
    assert audit_lines, "no audit.order log line emitted"
    assert any("api_key" in line for line in audit_lines)


def test_session_auth_logs_session_identity(caplog: pytest.LogCaptureFixture) -> None:
    app = _app("test-key")
    client = TestClient(app)
    # Login to get session cookie + CSRF token
    login_r = client.post("/auth/login", json={"password": "test-key"})
    csrf = login_r.json()["csrf_token"]
    with caplog.at_level(logging.INFO, logger="tradex_interfaces.routes.orders"):
        _post_order(client, {"X-CSRF-Token": csrf})
    audit_lines = [r.message for r in caplog.records if "audit.order" in r.message]
    assert audit_lines, "no audit.order log line emitted"
    assert any("session:operator" in line for line in audit_lines)


def test_no_auth_logs_anonymous_identity(caplog: pytest.LogCaptureFixture) -> None:
    app = _app(api_key=None)  # dev/paper mode
    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="tradex_interfaces.routes.orders"):
        _post_order(client)
    audit_lines = [r.message for r in caplog.records if "audit.order" in r.message]
    assert audit_lines, "no audit.order log line emitted"
    assert any("anonymous" in line for line in audit_lines)


def test_audit_log_includes_action(caplog: pytest.LogCaptureFixture) -> None:
    app = _app(api_key=None)
    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="tradex_interfaces.routes.orders"):
        _post_order(client)
    audit_lines = [r.message for r in caplog.records if "audit.order" in r.message]
    assert any("place_order" in line for line in audit_lines)
