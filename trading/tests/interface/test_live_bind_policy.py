from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from tradex_domain import BrokerId

from tradex_interfaces import fastapi_app
from tradex_interfaces.fastapi_app import _require_live_bind_loopback


@pytest.fixture(autouse=True)
def _clean_serve_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TRADEX_SERVE_BROKER",
        "TRADEX_SERVE_MODE",
        "TRADEX_SERVE_API_KEY",
        "TRADEX_SERVE_HOST",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_live_loopback_hosts_are_allowed(host: str) -> None:
    _require_live_bind_loopback("DHAN", "live", host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10"])
def test_live_non_loopback_hosts_are_rejected(host: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        _require_live_bind_loopback("DHAN", "live", host)

    message = str(exc_info.value)
    assert "DHAN" in message
    assert host in message
    assert "loopback" in message.lower()


def test_live_mode_with_paper_broker_rejects_non_loopback() -> None:
    with pytest.raises(ValueError) as exc_info:
        _require_live_bind_loopback("PAPER", "live", "0.0.0.0")

    message = str(exc_info.value)
    assert "PAPER" in message
    assert "0.0.0.0" in message
    assert "loopback" in message.lower()


@pytest.mark.parametrize("mode", ["paper", "backtest", "replay"])
def test_non_live_dhan_bind_is_not_blocked(mode: str) -> None:
    _require_live_bind_loopback("DHAN", mode, "0.0.0.0")


def _session(*, broker: str, mode: str, state: str = "READY") -> MagicMock:
    session = MagicMock()
    session.broker_id = broker
    session.mode = mode
    session.state = state
    return session


def test_start_checks_api_key_before_live_bind_policy() -> None:
    with patch("uvicorn.run") as uvicorn_run:
        with pytest.raises(ValueError, match="API key"):
            fastapi_app.start_fastapi_server(
                _session(broker="DHAN", mode="live", state="NEW"),
                host="0.0.0.0",
                api_key=None,
            )

    uvicorn_run.assert_not_called()


def test_start_rejects_live_non_loopback_before_readiness() -> None:
    with patch("uvicorn.run") as uvicorn_run:
        with pytest.raises(ValueError) as exc_info:
            fastapi_app.start_fastapi_server(
                _session(broker="DHAN", mode="live", state="NEW"),
                host="0.0.0.0",
                api_key="secret",
            )

    assert "loopback" in str(exc_info.value).lower()
    uvicorn_run.assert_not_called()


def test_live_mode_with_paper_broker_requires_api_key() -> None:
    with patch("uvicorn.run") as uvicorn_run:
        with pytest.raises(ValueError, match="API key"):
            fastapi_app.start_fastapi_server(
                _session(broker="PAPER", mode="live"),
                host="127.0.0.1",
                api_key=None,
            )

    uvicorn_run.assert_not_called()


def test_live_mode_with_paper_broker_rejects_non_loopback_start() -> None:
    with patch("uvicorn.run") as uvicorn_run:
        with pytest.raises(ValueError) as exc_info:
            fastapi_app.start_fastapi_server(
                _session(broker="PAPER", mode="live", state="NEW"),
                host="0.0.0.0",
                api_key="secret",
            )

    message = str(exc_info.value)
    assert "PAPER" in message
    assert "0.0.0.0" in message
    assert "loopback" in message.lower()
    uvicorn_run.assert_not_called()


@pytest.mark.parametrize("mode", ["backtest", "replay"])
def test_backtest_or_replay_dhan_allows_non_loopback_without_key(
    mode: str,
) -> None:
    with patch("uvicorn.run") as uvicorn_run:
        fastapi_app.start_fastapi_server(
            _session(broker="DHAN", mode=mode),
            host="0.0.0.0",
        )

    assert uvicorn_run.call_args.kwargs["host"] == "0.0.0.0"


def test_paper_mode_allows_non_loopback_without_key() -> None:
    with patch("uvicorn.run") as uvicorn_run:
        fastapi_app.start_fastapi_server(
            _session(broker="PAPER", mode="paper"),
            host="0.0.0.0",
        )

    assert uvicorn_run.call_args.kwargs["host"] == "0.0.0.0"


def test_host_is_normalized_before_uvicorn() -> None:
    with patch("uvicorn.run") as uvicorn_run:
        fastapi_app.start_fastapi_server(
            _session(broker="PAPER", mode="paper"),
            host="  LOCALHOST  ",
        )

    assert uvicorn_run.call_args.kwargs["host"] == "localhost"


def test_factory_start_forwards_normalized_host_and_mode() -> None:
    with patch("uvicorn.run") as uvicorn_run:
        fastapi_app.start_fastapi_server(
            _session(broker="DHAN", mode="backtest"),
            host="  LOCALHOST  ",
            workers=2,
        )

    assert uvicorn_run.call_args.kwargs["host"] == "localhost"
    assert os.environ["TRADEX_SERVE_HOST"] == "localhost"
    assert os.environ["TRADEX_SERVE_MODE"] == "backtest"


def test_serve_app_rejects_live_non_loopback_before_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADEX_SERVE_BROKER", "DHAN")
    monkeypatch.setenv("TRADEX_SERVE_MODE", "live")
    monkeypatch.setenv("TRADEX_SERVE_API_KEY", "secret")
    monkeypatch.setenv("TRADEX_SERVE_HOST", "0.0.0.0")

    with patch("tradex_trading.sdk.session.TradingSession.live") as live:
        with pytest.raises(ValueError) as exc_info:
            fastapi_app.serve_app()

    assert "loopback" in str(exc_info.value).lower()
    live.assert_not_called()


@pytest.mark.parametrize("mode", ["backtest", "replay"])
def test_serve_app_builds_non_live_dhan_session(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("TRADEX_SERVE_BROKER", "DHAN")
    monkeypatch.setenv("TRADEX_SERVE_MODE", mode)
    monkeypatch.setenv("TRADEX_SERVE_HOST", "0.0.0.0")
    session = MagicMock()
    app = object()

    with patch(
        "tradex_trading.sdk.session.TradingSession.live",
    ) as live, patch(
        "tradex_runtime.startup.boot",
        return_value=session,
    ) as boot, patch.object(
        fastapi_app,
        "create_app",
        return_value=app,
    ) as create_app:
        result = fastapi_app.serve_app()

    live.assert_not_called()
    config = boot.call_args.args[0]
    assert config.broker_id is BrokerId.DHAN
    assert config.mode == mode
    create_app.assert_called_once_with(session, api_key=None)
    assert result is app
