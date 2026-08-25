"""Strategy/backtest/scanner endpoint tests — engine parity and validation.

The backtest endpoint must reproduce the same numbers as the canonical
datalake backtest path (scripts/backtest_datalake.py): same BacktestEngine,
same single-sourced candle construction, same resample. These tests pin that
parity plus the 422 contract for unknown strategies/params.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from tradex_trading.interface.fastapi_app import create_app  # noqa: E402

pytest.importorskip("pandas")


def _client() -> TestClient:
    return TestClient(create_app(session=None))


@pytest.fixture(autouse=True)
def datalake_or_skip():
    from tradex_trading.datalake.parquet_storage import ParquetStorage

    store = ParquetStorage("data/")
    if not store.symbols():
        pytest.skip("empty datalake")


# 2026-06-01 .. 2026-07-20 UTC — inside RELIANCE datalake coverage; wide
# enough that D1 resampling yields >= 25 bars for the sma(20) parity test.
_WINDOW = {"exchange": "NSE", "symbol": "RELIANCE", "interval": "D",
           "from": 1780262400, "to": 1784496000}


class TestStrategiesEndpoint:
    def test_lists_discovered_strategies_and_scanners(self):
        resp = _client().get("/api/charts/strategies")
        assert resp.status_code == 200
        body = resp.json()
        scanner_ids = {s["id"] for s in body["scanners"]}
        # The extensions package ships these three; discovery is the feature.
        assert {"momentum_scanner", "nifty500_technical_scanner", "pullback_scanner"} <= scanner_ids
        for s in body["scanners"]:
            assert s["universe_size"] > 0


class TestBacktestEndpoint:
    def test_matches_direct_backtest_engine_run(self):
        """Endpoint metrics equal a direct BacktestEngine.run over identical candles."""
        from datetime import timedelta

        from tradex_trading.interface.routes.chart import _backtest_candles, _window
        from tradex_trading.replay.backtest import BacktestEngine

        client = _client()
        start, end = _window(_WINDOW["from"], _WINDOW["to"])
        instrument = _instrument()
        candles = _backtest_candles(instrument, _timeframe("D"), start - timedelta(days=1), end)
        if len(candles) < 25:
            pytest.skip("window too thin for sma(20)")

        direct = BacktestEngine(initial_capital=100000).run(
            _strategy(instrument), candles,
        )
        resp = client.post("/api/charts/backtest", json={**_WINDOW, "strategy": "sma_cross"})
        assert resp.status_code == 200, resp.text
        served = resp.json()["metrics"]
        assert served["num_trades"] == direct.num_trades
        assert served["total_return"] == pytest.approx(float(direct.total_return))
        assert served["sharpe"] == pytest.approx(float(direct.sharpe))
        assert served["max_drawdown"] == pytest.approx(float(direct.max_drawdown))

    def test_equity_curve_times_are_utc_seconds(self):
        resp = _client().post("/api/charts/backtest", json={**_WINDOW, "strategy": "sma_cross"})
        assert resp.status_code == 200
        curve = resp.json()["equity_curve"]
        if not curve:
            pytest.skip("flat window produced no equity points")
        for point in curve:
            assert isinstance(point["time"], int)
            assert point["value"] > 0

    def test_unknown_strategy_is_422_with_choices(self):
        resp = _client().post(
            "/api/charts/backtest", json={**_WINDOW, "strategy": "moon_shot"}
        )
        assert resp.status_code == 422
        assert "sma_cross" in resp.json()["detail"]

    def test_unknown_param_is_422(self):
        resp = _client().post(
            "/api/charts/backtest",
            json={**_WINDOW, "strategy": "sma_cross", "params": {"fast": 5, "slow": 20, "bogus": 1}},
        )
        assert resp.status_code == 422

    def test_rejected_signals_surfaced_in_trades(self):
        """Trades carry a rejected flag so the chart can render them distinctly."""
        resp = _client().post(
            "/api/charts/backtest",
            json={**_WINDOW, "strategy": "mean_reversion"},
        )
        assert resp.status_code == 200
        for trade in resp.json()["trades"]:
            assert trade["rejected"] is False
            assert trade["side"] in {"BUY", "SELL"}


class TestScannerEndpoint:
    def test_runs_momentum_scanner_ranked(self):
        resp = _client().post("/api/charts/scanner/run", json={"id": "momentum_scanner"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["scanner"] == "momentum_scanner"
        ranks = [r["rank"] for r in body["results"]]
        assert ranks == sorted(ranks) == list(range(1, len(ranks) + 1))

    def test_nifty500_scanner_reports_matched_conditions(self):
        resp = _client().post(
            "/api/charts/scanner/run", json={"id": "nifty500_technical_scanner"}
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        if not results:
            pytest.skip("no symbol matched (thin datalake)")
        top = results[0]
        assert set(top["matched"]) <= {"close", "roc"}
        assert 0.0 <= top["score"] <= 1.0

    def test_unknown_scanner_is_404_with_choices(self):
        resp = _client().post("/api/charts/scanner/run", json={"id": "nope"})
        assert resp.status_code == 404
        assert "momentum_scanner" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _instrument():
    from tradex_domain.instruments import Equity

    return Equity.of("NSE", "RELIANCE")


def _timeframe(code: str):
    from tradex_domain.enums import Timeframe

    return Timeframe.D1 if code == "D" else None


def _strategy(instrument):
    from tradex_trading.strategy.extensions.strategies.sma_cross import (
        SmaCrossStrategy,
    )

    return SmaCrossStrategy(strategy_id="parity_test", instrument=instrument)
