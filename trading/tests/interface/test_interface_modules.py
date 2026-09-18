"""Tests for interface modules — cli.

Tests the ported _build_parser, run_cli, main.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tradex_trading.interface.cli import _build_parser, main, run_cli

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

    def test_parser_has_sync_command(self) -> None:
        """sync command should parse with its documented defaults."""
        parser = _build_parser()
        args = parser.parse_args(["sync"])
        assert args.command == "sync"
        assert args.universe == "nifty500"
        assert args.timeframe == "1m"
        assert args.broker == "dhan"
        assert args.workers == 4
        assert args.batch_size == 20
        assert args.skip_existing is True
        assert args.dry_run is False

    def test_parser_sync_with_overrides(self) -> None:
        """sync --start/--end/--universe/--dry-run should parse."""
        parser = _build_parser()
        args = parser.parse_args(
            ["sync", "--start", "2026-09-01", "--end", "2026-09-05",
             "--universe", "nifty50", "--timeframe", "5m", "--dry-run"]
        )
        assert args.start == "2026-09-01"
        assert args.end == "2026-09-05"
        assert args.universe == "nifty50"
        assert args.timeframe == "5m"
        assert args.dry_run is True

    def test_parser_sync_no_skip_existing(self) -> None:
        """--no-skip-existing should flip skip_existing to False."""
        parser = _build_parser()
        args = parser.parse_args(["sync", "--no-skip-existing"])
        assert args.skip_existing is False

    def test_parser_sync_rejects_invalid_universe(self) -> None:
        """sync should reject an unknown universe (choices are constrained)."""
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["sync", "--universe", "nasdaq100"])


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
        # Patch resolution imports fastapi_app, whose top level imports
        # fastapi — declare the api-extra dep so a fastapi-less env skips
        # instead of failing opaquely (mirrors test_fastapi_app.py).
        pytest.importorskip("fastapi")
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
        pytest.importorskip("fastapi")
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
        pytest.importorskip("fastapi")
        with patch(
            "tradex_trading.interface.fastapi_app.start_fastapi_server",
            side_effect=RuntimeError("port in use"),
        ):
            result = run_cli(["serve"])
        assert result == 1

    def test_sync_dry_run_is_paper_and_writes_nothing(self) -> None:
        """sync --dry-run must never touch the network or the store.

        This is the safety guarantee of the command: --dry-run swaps in the
        paper broker and can be run without credentials.
        """
        with patch("tradex_trading.datalake.simple_sync.simple_sync") as simple:
            simple.return_value = MagicMock(
                requested=50, fetched=0, written=0, failed=[]
            )
            result = run_cli(
                ["sync", "--dry-run", "--universe", "nifty50",
                 "--start", "2026-09-01", "--end", "2026-09-02"]
            )
        assert result == 0
        simple.assert_called_once()
        # The broker handed to the pipeline must be a PaperBroker, not dhan.
        broker_arg = simple.call_args.args[0]
        assert type(broker_arg).__name__ == "PaperBroker"

    def test_serve_reuses_runtime_session(self) -> None:
        """main() must pass its own session to serve — never boot a second."""
        pytest.importorskip("fastapi")
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


class TestModuleEntryPoint:
    """`python -m tradex_trading.interface.cli` must actually dispatch.

    Regression: cli.py had no ``__main__`` guard, so runpy executed the file
    top-to-bottom and exited without calling main() — `python -m ... serve`
    silently did nothing (the console-script entry point was unaffected).
    """

    def test_help_via_dash_m_exits_zero(self) -> None:
        import os
        import subprocess
        import sys
        from pathlib import Path

        trading_src = Path(tradex_cli_file()).parents[2]  # .../trading/src
        repo_pkgs = trading_src.parent  # v4/trading
        srcs = os.pathsep.join(str(x / "src") for x in (repo_pkgs, repo_pkgs.parent / "domain", repo_pkgs.parent / "brokers"))
        env = dict(os.environ)
        env["PYTHONPATH"] = srcs + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "tradex_trading.interface.cli", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert proc.returncode == 0
        assert "serve" in proc.stdout


def tradex_cli_file() -> str:
    import tradex_trading.interface.cli as m
    return m.__file__
