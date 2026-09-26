"""Fail-closed security tests — a live session must not be served without an API key configured.

The policy is mode-based: paper/backtest/replay remain development paths, while
live sessions require authentication regardless of the broker label.
"""

from __future__ import annotations

from typing import Any

import pytest

from tradex_trading.interface import fastapi_app


def test_require_api_key_for_live_fails_closed_without_key() -> None:
    with pytest.raises(ValueError, match="API key"):
        fastapi_app._require_api_key_for_live("live", None)


@pytest.mark.parametrize("mode", ["paper", "backtest", "replay"])
def test_require_api_key_for_non_live_mode_allows_missing_key(mode: str) -> None:
    fastapi_app._require_api_key_for_live(mode, None)


def test_require_api_key_for_live_allows_live_with_key() -> None:
    fastapi_app._require_api_key_for_live("live", "secret-key")


def test_serve_app_live_without_api_key_fails_closed(monkeypatch: Any) -> None:
    monkeypatch.setenv("TRADEX_SERVE_BROKER", "DHAN")
    monkeypatch.setenv("TRADEX_SERVE_MODE", "live")
    monkeypatch.delenv("TRADEX_SERVE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        fastapi_app.serve_app()


class _FakeSession:
    broker_id = "PAPER"
    mode = "live"


def test_start_server_live_without_key_fails_closed() -> None:
    with pytest.raises(ValueError, match="API key"):
        fastapi_app.start_fastapi_server(session=_FakeSession(), api_key=None)
