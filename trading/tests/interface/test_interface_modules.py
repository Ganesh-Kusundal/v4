"""Tests for interface modules — check_connection, cli, tui.

Tests the ported diagnose, render_status, _build_parser, run_cli,
_token_path, _cooldown_path, _check, main.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from tradex_trading.interface.check_connection import (
    _check,
    _cooldown_path,
    _token_path,
)
from tradex_trading.interface.check_connection import (
    main as check_main,
)
from tradex_trading.interface.cli import _build_parser, main, run_cli
from tradex_trading.interface.tui import diagnose, render_status

# ---------------------------------------------------------------------------
# Check Connection — _token_path, _cooldown_path, _check, main
# ---------------------------------------------------------------------------


class TestTokenPath:
    """_token_path — broker token path resolution."""

    def test_dhan_default_path(self) -> None:
        """Dhan should use the default runtime layout when env not set."""
        with patch.dict("os.environ", {}, clear=True):
            path = _token_path("dhan")
            assert "runtime" in path and "dhan" in path and "token_state.json" in path

    def test_dhan_env_override(self) -> None:
        """Dhan should use DHAN_TOKEN_PATH env var."""
        with patch.dict("os.environ", {"DHAN_TOKEN_PATH": "/custom/path.json"}):
            path = _token_path("dhan")
            assert path == "/custom/path.json"

    def test_upstox_live_path(self) -> None:
        """Upstox LIVE should use UPSTOX_ prefix."""
        with patch.dict("os.environ", {}, clear=True):
            path = _token_path("upstox", "LIVE")
            assert "runtime" in path and "upstox" in path and "token_state.json" in path

    def test_upstox_sandbox_path(self) -> None:
        """Upstox SANDBOX should use UPSTOX_SANDBOX_ prefix."""
        with patch.dict("os.environ", {}, clear=True):
            path = _token_path("upstox", "SANDBOX")
            assert "runtime" in path and "upstox" in path and "token_state.json" in path


class TestCooldownPath:
    """_cooldown_path — TOTP cooldown path."""

    def test_returns_path_with_broker_name(self) -> None:
        """Should include broker name in path."""
        path = _cooldown_path("dhan")
        assert "dhan" in path
        assert "cooldown" in path


class TestCheck:
    """_check — broker connectivity check."""

    def test_check_without_session(self) -> None:
        """_check without session should attempt broker connect (mocked)."""
        mock_broker = MagicMock()
        mock_broker._connected = True
        with patch(
            "tradex_trading.runtime.live.build_broker_from_env",
            return_value=mock_broker,
        ):
            result = _check("dhan")
        assert result is True

    def test_check_returns_bool(self) -> None:
        """_check should return bool."""
        mock_broker = MagicMock()
        mock_broker._connected = False
        with patch(
            "tradex_trading.runtime.live.build_broker_from_env",
            return_value=mock_broker,
        ):
            result = _check("upstox")
        assert isinstance(result, bool)

    def test_check_without_session_failure(self) -> None:
        """_check without session should return False on broker build failure."""
        with patch(
            "tradex_trading.runtime.live.build_broker_from_env",
            side_effect=RuntimeError("no credentials"),
        ):
            result = _check("dhan")
        assert result is False


class TestCheckMain:
    """main — CLI entry point for check_connection."""

    def test_main_default_args(self) -> None:
        """main with default args should check both brokers."""
        mock_broker = MagicMock()
        mock_broker._connected = True
        with patch(
            "tradex_trading.runtime.live.build_broker_from_env",
            return_value=mock_broker,
        ):
            result = check_main([])
        assert result == 0

    def test_main_single_broker(self) -> None:
        """main with --broker dhan should check only dhan."""
        mock_broker = MagicMock()
        mock_broker._connected = True
        with patch(
            "tradex_trading.runtime.live.build_broker_from_env",
            return_value=mock_broker,
        ):
            result = check_main(["--broker", "dhan"])
        assert result == 0

    def test_main_invalid_args(self) -> None:
        """main with invalid args should return error code."""
        result = check_main(["--invalid-arg"])
        assert result == 2


# ---------------------------------------------------------------------------
# CLI — _build_parser, run_cli
# ---------------------------------------------------------------------------


class TestBuildParser:
    """_build_parser — CLI parser builder."""

    def test_returns_parser(self) -> None:
        """_build_parser should return an ArgumentParser."""
        parser = _build_parser()
        assert parser is not None
        assert parser.prog == "tradex-v4"

    def test_parser_has_quote_command(self) -> None:
        """Parser should have quote command."""
        parser = _build_parser()
        args = parser.parse_args(["quote", "NSE", "RELIANCE"])
        assert args.command == "quote"
        assert args.exchange == "NSE"
        assert args.symbol == "RELIANCE"

    def test_parser_has_health_command(self) -> None:
        """Parser should have health command."""
        parser = _build_parser()
        args = parser.parse_args(["health"])
        assert args.command == "health"

    def test_parser_has_scanner_command(self) -> None:
        """Parser should have scanner command."""
        parser = _build_parser()
        args = parser.parse_args(["scanner"])
        assert args.command == "scanner"

    def test_parser_has_serve_command(self) -> None:
        """Parser should have serve command with defaults."""
        parser = _build_parser()
        args = parser.parse_args(["serve"])
        assert args.command == "serve"
        assert args.host == "127.0.0.1"
        assert args.port == 8080
        assert args.broker == "PAPER"
        assert args.workers == 1
        assert args.reload is False

    def test_parser_serve_with_overrides(self) -> None:
        """serve --host/--port/--broker/--api-key should parse."""
        parser = _build_parser()
        args = parser.parse_args(
            ["serve", "--host", "0.0.0.0", "--port", "9090", "--broker", "UPSTOX", "--api-key", "k"]
        )
        assert args.host == "0.0.0.0"
        assert args.port == 9090
        assert args.broker == "UPSTOX"
        assert args.api_key == "k"

    def test_parser_serve_workers_and_reload(self) -> None:
        """serve --workers/--reload should parse."""
        parser = _build_parser()
        args = parser.parse_args(["serve", "--workers", "4", "--reload"])
        assert args.workers == 4
        assert args.reload is True


class TestRunCli:
    """run_cli — CLI runner."""

    def test_health_without_runtime(self) -> None:
        """health command without runtime should print ok."""
        result = run_cli(["health"])
        assert result == 0

    def test_no_command_returns_2(self) -> None:
        """No command should return 2."""
        result = run_cli([])
        assert result == 2

    def test_invalid_args_returns_2(self) -> None:
        """Invalid args should return 2."""
        result = run_cli(["--invalid"])
        assert result == 2

    def test_quote_without_runtime_returns_1(self) -> None:
        """quote command without runtime should return 1."""
        result = run_cli(["quote", "NSE", "RELIANCE"])
        assert result == 1

    def test_scanner_returns_0(self) -> None:
        """scanner command should return 0."""
        result = run_cli(["scanner"], runtime=MagicMock())
        assert result == 0

    def test_serve_starts_fastapi_server(self) -> None:
        """serve should boot a paper session and start the FastAPI server."""
        with patch(
            "tradex_trading.interface.fastapi_app.start_fastapi_server"
        ) as start:
            result = run_cli(["serve"])
        assert result == 0
        start.assert_called_once()
        _session, kwargs = start.call_args
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 8080
        assert kwargs["api_key"] is None
        assert kwargs["workers"] == 1
        assert kwargs["reload"] is False

    def test_serve_forwards_workers_and_reload(self) -> None:
        """serve --workers/--reload should reach start_fastapi_server."""
        with patch(
            "tradex_trading.interface.fastapi_app.start_fastapi_server"
        ) as start:
            result = run_cli(["serve", "--workers", "3", "--reload", "--api-key", "k"])
        assert result == 0
        _session, kwargs = start.call_args
        assert kwargs["workers"] == 3
        assert kwargs["reload"] is True
        assert kwargs["api_key"] == "k"

    def test_serve_failure_returns_1(self) -> None:
        """serve should print a loud failure and return 1 on server error."""
        with patch(
            "tradex_trading.interface.fastapi_app.start_fastapi_server",
            side_effect=RuntimeError("port in use"),
        ):
            result = run_cli(["serve"])
        assert result == 1

    def test_serve_reuses_runtime_session(self) -> None:
        """main() must pass its own session to serve — never boot a second."""
        with patch(
            "tradex_trading.interface.fastapi_app.start_fastapi_server"
        ) as start, patch(
            "tradex_trading.sdk.session.TradingSession.paper",
            side_effect=AssertionError("serve must reuse the runtime session"),
        ):
            result = main(["serve", "--port", "8099"])
        assert result == 0
        start.assert_called_once()
        session = start.call_args.args[0]
        # main()'s finally stops the reused session once run_cli returns.
        assert session.state.value == "STOPPED"


class TestCliMain:
    """main — console entry point (regression: crashed before args existed).

    The old implementation read ``args.env_file`` before ``args`` was parsed,
    so every ``tradex`` invocation died with ``UnboundLocalError``.
    """

    def test_main_health_returns_zero(self, capsys) -> None:
        result = main(["health"])
        assert result == 0
        assert "READY" in capsys.readouterr().out

    def test_main_quote_boots_session_and_prints_ltp(self, capsys) -> None:
        """quote must boot a session (runtime shim) instead of 'no runtime bound'."""
        result = main(["quote", "NSE", "RELIANCE"])
        assert result == 0
        assert "ltp" in capsys.readouterr().out

    def test_main_missing_env_file_does_not_crash(self, capsys) -> None:
        result = main(["--env-file", "/nonexistent/env-does-not-exist.env"])
        assert result == 2  # no command → help printed

    def test_main_invalid_args_returns_2(self, capsys) -> None:
        result = main(["--invalid"])
        assert result == 2


# ---------------------------------------------------------------------------
# TUI — diagnose, render_status
# ---------------------------------------------------------------------------


class TestDiagnose:
    """diagnose — connectivity probe."""

    def test_diagnose_returns_dict(self) -> None:
        """diagnose should return a dict."""
        # Use a mock session
        mock_session = MagicMock()
        mock_session.state.value = "READY"
        mock_session.broker_id.value = "PAPER"
        mock_session.broker._connected = True

        # Mock the runtime context path
        mock_runtime = MagicMock()
        mock_runtime.broker._connected = True
        mock_runtime.session.state.value = "READY"
        mock_runtime.config.environment = "PAPER"
        mock_runtime.engine.all_orders.return_value = []
        mock_runtime.cache.all_positions.return_value = []

        result = diagnose(mock_runtime)
        assert isinstance(result, dict)
        assert "broker_connected" in result
        assert "session_state" in result

    def test_diagnose_has_required_keys(self) -> None:
        """diagnose result should have all required keys."""
        mock_runtime = MagicMock()
        mock_runtime.broker._connected = False
        mock_runtime.session.state.value = "NEW"
        mock_runtime.config.environment = "LIVE"
        mock_runtime.engine.all_orders.return_value = []
        mock_runtime.cache.all_positions.return_value = []

        result = diagnose(mock_runtime)
        assert "broker_connected" in result
        assert "broker_reachable" in result
        assert "session_state" in result
        assert "environment" in result
        assert "orders" in result
        assert "positions" in result


class TestRenderStatus:
    """render_status — one-line status string."""

    def test_render_status_returns_string(self) -> None:
        """render_status should return a string."""
        mock_runtime = MagicMock()
        mock_runtime.broker._connected = True
        mock_runtime.session.state.value = "READY"
        mock_runtime.config.environment = "PAPER"
        mock_runtime.engine.all_orders.return_value = []
        mock_runtime.cache.all_positions.return_value = []

        result = render_status(mock_runtime)
        assert isinstance(result, str)

    def test_render_status_contains_tradex_v4(self) -> None:
        """render_status should contain 'tradex-v4'."""
        mock_runtime = MagicMock()
        mock_runtime.broker._connected = True
        mock_runtime.session.state.value = "READY"
        mock_runtime.config.environment = "PAPER"
        mock_runtime.engine.all_orders.return_value = []
        mock_runtime.cache.all_positions.return_value = []

        result = render_status(mock_runtime)
        assert "tradex-v4" in result

    def test_render_status_contains_environment(self) -> None:
        """render_status should contain the environment."""
        mock_runtime = MagicMock()
        mock_runtime.broker._connected = True
        mock_runtime.session.state.value = "READY"
        mock_runtime.config.environment = "SANDBOX"
        mock_runtime.engine.all_orders.return_value = []
        mock_runtime.cache.all_positions.return_value = []

        result = render_status(mock_runtime)
        assert "SANDBOX" in result
