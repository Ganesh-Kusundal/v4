from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from tradex_domain import BrokerId
from tradex_interfaces.cli import run_cli


def _live_session() -> MagicMock:
    session = MagicMock()
    session.broker_id = BrokerId.DHAN
    session.mode = "live"
    session.state = "READY"
    return session


def test_serve_forwards_live_host_and_broker() -> None:
    session = _live_session()

    with patch(
        "tradex_runtime.startup.boot",
        return_value=session,
    ) as boot, patch(
        "tradex_interfaces.fastapi_app.start_fastapi_server",
    ) as start:
        result = run_cli(
            [
                "serve",
                "--host",
                "::1",
                "--broker",
                "dhan",
                "--api-key",
                "secret",
            ]
        )

    assert result == 0
    # A live serve boots through the composition root with the operator's risk
    # policy (AC-10 refuses to boot an incomplete one), not through the
    # TradingSession.live() factory, which cannot carry a policy.
    boot.assert_called_once()
    assert boot.call_args.args[0].broker_id is BrokerId.DHAN
    assert boot.call_args.args[0].live_enabled is True
    assert start.call_args.args[0] is session
    assert start.call_args.kwargs["host"] == "::1"


def test_live_non_loopback_serve_fails_before_session_creation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch(
        "tradex_runtime.session_impl.TradingSession.live",
    ) as live, patch(
        "tradex_interfaces.fastapi_app.start_fastapi_server",
    ) as start:
        result = run_cli(
            [
                "serve",
                "--host",
                "0.0.0.0",
                "--broker",
                "dhan",
                "--api-key",
                "secret",
            ]
        )

    assert result == 1
    output = capsys.readouterr().out
    assert "DHAN" in output
    assert "0.0.0.0" in output
    assert "loopback" in output.lower()
    live.assert_not_called()
    start.assert_not_called()


def test_live_missing_key_fails_before_session_creation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch(
        "tradex_runtime.session_impl.TradingSession.live",
    ) as live, patch(
        "tradex_interfaces.fastapi_app.start_fastapi_server",
    ) as start:
        result = run_cli(
            [
                "serve",
                "--host",
                "127.0.0.1",
                "--broker",
                "dhan",
            ]
        )

    assert result == 1
    output = capsys.readouterr().out
    assert "API key" in output
    live.assert_not_called()
    start.assert_not_called()
