"""Fail-closed security tests — a live broker session must not be served
without an API key configured (real-money posture).

Prior behaviour: the API key was optional even for live, and when unset the
HTTP write endpoints ran unauthenticated. This suite pins the new contract:
live ⇒ key required, refuse to start otherwise (paper/dev may run keyless).
"""

from __future__ import annotations

from typing import Any

import pytest

from tradex_trading.interface import fastapi_app


def test_require_api_key_for_live_fails_closed_without_key() -> None:
    """Live broker with no key => refus to serve."""
    with pytest.raises(ValueError, match="API key"):
        fastapi_app._require_api_key_for_live("DHAN", None)


def test_require_api_key_for_live_allows_paper_without_key() -> None:
    """Paper is the dev path — it may run without a key."""
    fastapi_app._require_api_key_for_live("PAPER", None)  # must not raise
    fastapi_app._require_api_key_for_live("", None)  # default paper


def test_require_api_key_for_live_allows_live_with_key() -> None:
    fastapi_app._require_api_key_for_live("DHAN", "secret-key")  # must not raise


def test_serve_app_live_without_api_key_fails_closed(monkeypatch: Any) -> None:
    """The env-driven serve path refuses to build a live-session app keyless."""
    monkeypatch.setenv("TRADEX_SERVE_BROKER", "DHAN")
    monkeypatch.delenv("TRADEX_SERVE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        fastapi_app.serve_app()


class _FakeSession:
    broker_id = "DHAN"


def test_start_server_live_without_key_fails_closed() -> None:
    """Programmatic start with a live session + no key => refuse to bind."""
    with pytest.raises(ValueError, match="API key"):
        fastapi_app.start_fastapi_server(session=_FakeSession(), api_key=None)