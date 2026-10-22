"""`tradex serve` must boot through the same config and strategy gate."""

from __future__ import annotations

import pytest
from tradex_interfaces import fastapi_app as fa


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(fa._SERVE_ENV_BROKER, "DHAN")
    monkeypatch.setenv(fa._SERVE_ENV_MODE, "live")
    monkeypatch.setenv(fa._SERVE_ENV_API_KEY, "test-key")
    monkeypatch.setenv(fa._SERVE_ENV_HOST, "127.0.0.1")
    monkeypatch.delenv("TRADEX_STRATEGY_IDS", raising=False)
    monkeypatch.delenv("TRADEX_PERSISTENCE_PATH", raising=False)


def _capture_boot(monkeypatch):
    import tradex_runtime.startup as startup

    captured: dict = {}

    def fake_boot(config, **kwargs):
        captured["config"] = config
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(startup, "boot", fake_boot)
    monkeypatch.setattr(fa, "create_app", lambda session, api_key=None: "app")
    return captured


def test_live_serve_enables_live_and_passes_named_strategies(monkeypatch) -> None:
    captured = _capture_boot(monkeypatch)
    monkeypatch.setenv("TRADEX_STRATEGY_IDS", "deployed")

    assert fa.serve_app() == "app"

    config = captured["config"]
    assert config.live_enabled is True
    assert config.persistence.path
    assert captured["kwargs"]["wire_strategies"] is True


def test_live_serve_without_named_strategies_does_not_wire_discovery(monkeypatch) -> None:
    captured = _capture_boot(monkeypatch)

    assert fa.serve_app() == "app"

    assert captured["kwargs"]["wire_strategies"] is False
