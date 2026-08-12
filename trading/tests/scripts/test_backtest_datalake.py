"""Tests for the ``backtest_datalake`` CLI script.

The script is standalone (not part of ``tradex_trading``), so it is imported
by file path; its module-level ``sys.path`` bootstrap runs on import.
"""

from __future__ import annotations

import argparse
import importlib.util
from datetime import datetime as _real_datetime
from pathlib import Path

import pandas as pd
import pytest

from tradex_trading.datalake.parquet_storage import ParquetStorage

_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "backtest_datalake.py"
)
_spec = importlib.util.spec_from_file_location("backtest_datalake", _SCRIPT)
backtest_datalake = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(backtest_datalake)

main = backtest_datalake.main


def _upsert_series(store: ParquetStorage, symbol: str, closes: list[float]) -> None:
    """Write one 1m bar per day with the given close values (July 2026)."""
    rows = []
    for day, close in enumerate(closes, start=1):
        rows.append(
            dict(symbol=symbol, exchange="NSE", kind="equity", timeframe="1m",
                 timestamp=f"2026-07-{day:02d} 09:15:00", open=close, high=close,
                 low=close, close=close, volume=1000)
        )
    store.upsert(pd.DataFrame(rows))


class TestValidation:
    """Argparse-level guards that return 1 before any data is loaded."""

    def test_walk_forward_requires_optimize(self) -> None:
        assert main(["--walk-forward"]) == 1

    @pytest.mark.parametrize("strategy", ["sma_cross", "multi_symbol"])
    def test_rejects_fast_gte_slow(self, strategy: str) -> None:
        assert main(["--strategy", strategy, "--fast", "20", "--slow", "10"]) == 1

    def test_rejects_oversold_gte_overbought(self) -> None:
        assert (
            main(
                ["--strategy", "mean_reversion",
                 "--oversold", "80", "--overbought", "60"]
            )
            == 1
        )


class TestHelpers:
    def test_param_grid_by_strategy(self) -> None:
        mr = argparse.Namespace(strategy="mean_reversion")
        assert backtest_datalake._param_grid(mr) == {"period": [7, 14, 21]}
        for name in ("sma_cross", "multi_symbol"):
            grid = backtest_datalake._param_grid(argparse.Namespace(strategy=name))
            assert set(grid) == {"fast", "slow"}

    def test_make_strategy_applies_overrides(self) -> None:
        args = argparse.Namespace(
            fast=5, slow=20, period=14, overbought=70.0, oversold=30.0,
        )
        strat = backtest_datalake._make_strategy("sma_cross", "RELIANCE", args, fast=3)
        assert strat._fast == 3  # noqa: SLF001 – probe the injected override
        assert strat._slow == 20  # noqa: SLF001
        mr = backtest_datalake._make_strategy(
            "mean_reversion", "RELIANCE", args, period=7
        )
        assert mr._period == 7  # noqa: SLF001


class TestEndToEnd:
    def test_main_runs_portfolio_against_tmp_store(
        self, tmp_path, capsys, monkeypatch,
    ) -> None:
        """main() with a tmp data root: load → portfolio pass → table row."""

        class _FixedNow:
            @staticmethod
            def now():
                return _real_datetime(2026, 8, 10)

        monkeypatch.setattr(backtest_datalake, "datetime", _FixedNow)

        store = ParquetStorage(tmp_path)
        _upsert_series(store, "RELIANCE", [10, 11, 10, 9, 15])
        _upsert_series(store, "TCS", [10, 12, 10, 8, 16])

        code = main([
            "--data-root", str(tmp_path),
            "--symbols", "RELIANCE,TCS",
            "--strategy", "multi_symbol",
            "--fast", "2", "--slow", "3",
            "--timeframe", "1d", "--months", "2",
            "--max-workers", "2", "--log-level", "ERROR",
        ])
        assert code == 0
        assert "PORTFOLIO" in capsys.readouterr().out

    def test_main_returns_1_on_empty_store(self, tmp_path) -> None:
        store = ParquetStorage(tmp_path)
        assert store.symbols() == []
        with pytest.warns(UserWarning, match="no data"):
            code = main([
                "--data-root", str(tmp_path),
                "--symbols", "RELIANCE",
                "--log-level", "ERROR",
            ])
        assert code == 1

    def test_limit_caps_universe_before_load(self, tmp_path, monkeypatch) -> None:
        """--limit caps the datalake load (instruments), not just the backtest
        loop — the fix that avoids reading 500 symbols to backtest a few."""
        from tradex_domain.instruments import Equity

        class _FakeLoader:
            def __init__(self, **kwargs) -> None:
                self.calls: list[dict] = []

            def load(self, **kwargs):
                self.calls.append(kwargs)
                return []

        fake = _FakeLoader()
        monkeypatch.setattr(backtest_datalake, "ParquetBacktestLoader", lambda **kw: fake)
        monkeypatch.setattr(
            "tradex_trading.datalake.universe.load_universe",
            lambda name: [Equity.of("NSE", s) for s in ("A", "B", "C")],
        )

        code = main(["--universe", "nifty50", "--limit", "2", "--log-level", "ERROR"])
        assert code == 1  # fake loader returns no candles
        assert fake.calls
        kwargs = fake.calls[0]
        assert len(kwargs["instruments"]) == 2
        assert kwargs["universe"] is None
