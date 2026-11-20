"""A non-ASCII ``X-API-Key`` is a rejected credential, never a server error."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from tradex_interfaces.fastapi_app import create_app  # noqa: E402
from tradex_reactive.bus import ReactiveBus  # noqa: E402


def _client() -> TestClient:
    session = MagicMock()
    session.state = "READY"
    session.bus = ReactiveBus()
    session.mode = "paper"
    session.engine = MagicMock()
    session.engine.all_orders.return_value = []
    return TestClient(create_app(session=session, api_key="secret"), raise_server_exceptions=False)


def test_non_ascii_login_password_is_401_not_500() -> None:
    r = _client().post("/auth/login", json={"password": "é"})
    assert r.status_code == 401, r.text
