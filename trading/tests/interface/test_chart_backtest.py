"""Strategy/backtest/scanner endpoint tests — engine parity and validation.

The backtest endpoint reads its candles from the same single source the
canonical datalake backtest path does (``scripts/backtest_datalake.py``:
``ParquetMarketProvider`` and its resample), and this file pins two things
about what it then does with them.

**Parity is against a *correctly built* engine, not a bare one.** The route
used to construct ``BacktestEngine(initial_capital=...)`` and nothing else —
no cash gate, so an unaffordable run completed and reported a flattering
equity curve; and no fee calculator unless the request body asked for one, so
a caller could compare a run against a free-execution one. Both were defects
on the surface an operator actually reads, both are fixed, and the comparison
this file makes is now against the engine AC-2 mandates: a ``RiskManager``
that fails closed on an unknown balance, a ``FeeCalculator`` and a 5 bps
slippage model. That is a real claim, and it is falsifiable — a route that
dropped any of the three serves different numbers and fails
``test_matches_direct_backtest_engine_run``.

**The 422 insufficient-cash branch is reachable, and the test that says so
provokes it.** It is tempting to conclude it is dead code, because the
position sizer sizes every order to a fraction of the opening capital. That
is true only for a strategy that *declares a stop*: then the risk formula
sizes the order at ``risk_fraction x equity`` and the fraction is well under
one (measured: 200 at every capital from 100,000 down to 2,000 for
``bracket_breakout``). A strategy that declares none cannot use the risk
formula, so the sizer falls back to a **configured absolute notional** — the
engine's ``DEFAULT_MAX_ORDER_VALUE`` of Rs 100,000, which has no relationship
at all to the account it is about to be placed in. ``sma_cross`` declares
nothing, so its orders are ~Rs 99,000 whatever the capital, and a caller who
opens the chart backtest with a starved ``initial_capital`` is asking the
route to place an order the account cannot pay for. The route refuses it
(422, typed envelope, no numbers) and the test below is that refusal.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from tradex_interfaces.fastapi_app import create_app  # noqa: E402

pytest.importorskip("pandas")


def _client() -> TestClient:
    return TestClient(create_app(session=None))


@pytest.fixture(autouse=True)
def datalake_or_skip():
    from tradex_market_data.parquet_storage import ParquetStorage

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


#: Slippage the route's engine is required to assume, in basis points. Stated
#: here rather than imported from the route: the parity claim is that the route
#: builds the engine this describes, and importing the constant would make the
#: test agree with any value the route happened to pick. The route states the
#: same figure in ``chart._BACKTEST_SLIPPAGE_BPS``; a deliberate change to the
#: cost model is an act with this test's diff as its review.
_SLIPPAGE_BPS = Decimal("5")

#: The opening capital the parity claim is made at, and the one the request
#: body carries, so the route cannot answer about a different account than the
#: one compared. 100,000 is also the route's default, so the body is stating
#: the default rather than configuring a special case.
_CAPITAL = 100_000


class TestBacktestEndpoint:
    def test_matches_direct_backtest_engine_run(self):
        """Endpoint metrics equal a direct BacktestEngine.run over identical candles.

        The comparison is against an engine built from AC-2's three parts — a
        risk gate that fails closed, fees, and slippage — and not against a bare
        ``BacktestEngine(initial_capital=...)``, which is what this asserted
        until the route stopped being allowed to run without costs. A bare
        engine and the route now disagree by construction (0.29% against 0.32%
        total return on this window, before the fee total is even read), and
        the disagreement is the defect the route fix removed.

        The equity curve and the fills are compared as well as the four
        metrics, because they are where a change to the cost model shows up
        first and in full: ``total_fees`` is a single number that a fee
        calculator can be quietly dropped from, and the curve is every point of
        it.
        """
        from datetime import timedelta

        from tradex_interfaces.routes.chart import _backtest_candles, _window

        client = _client()
        start, end = _window(_WINDOW["from"], _WINDOW["to"])
        instrument = _instrument()
        candles = _backtest_candles(instrument, _timeframe("D"), start - timedelta(days=1), end)
        if len(candles) < 25:
            pytest.skip("window too thin for sma(20)")

        direct = _engine_with_costs(_CAPITAL).run(_strategy(instrument), candles)
        resp = client.post(
            "/api/charts/backtest",
            json={**_WINDOW, "strategy": "sma_cross", "initial_capital": _CAPITAL},
        )
        assert resp.status_code == 200, resp.text
        served = resp.json()["metrics"]
        assert served["num_trades"] == direct.num_trades
        assert served["total_return"] == pytest.approx(float(direct.total_return))
        assert served["sharpe"] == pytest.approx(float(direct.sharpe))
        assert served["max_drawdown"] == pytest.approx(float(direct.max_drawdown))
        # The cost model, read off both sides. Without a fee calculator these
        # are 0.0 on the route's side and cannot be reconciled with the
        # engine's, and a free-execution run is not a run.
        assert served["total_fees"] == pytest.approx(float(direct.total_fees))
        assert served["total_fees"] > 0, (
            "a run that charges no fees is not the run the route claims to make"
        )
        # And the whole curve, which is what the four metrics are computed from.
        assert [p["value"] for p in resp.json()["equity_curve"]] == pytest.approx(
            [float(v) for v in direct.equity_curve],
        )
        assert [
            (t["side"], t["price"], t["qty"], t["rejected"])
            for t in resp.json()["trades"]
        ] == [
            (f["side"], f["price"], f["qty"], f["rejected"])
            for f in direct.fills
        ]

    def test_an_unaffordable_order_is_422_with_the_typed_envelope(self):
        """A run whose capital cannot pay for an order is refused, not flattered.

        ``initial_capital`` is a request-body key, so a caller can put any
        account in front of the route — including one too small for the order
        the sizer is about to produce. The route's answer to that is a 422
        carrying the cash floor's own words and no numbers, and this is the
        test that holds it to that: the funded capital in the same request
        shape is a 200 with metrics, so the 422 is about the capital and not
        about the request.

        The capitals are swept rather than pinned to one value because the band
        is one fee wide — the order-level gate denies on ``notional > cash``
        and the ledger floor on ``notional + fee > cash``, so only an account
        sitting inside that fee tips into the floor. Measured against the real
        lake over ``_WINDOW`` with ``sma_cross``: 200 at 1,100 and above, the
        422 from 920 to 1,000, and below that a 500 from a separate defect in
        the engine's result assembly (mixed naive/aware fill timestamps, filed
        against ``replay/…/backtest.py``). A fee schedule or a lake that moves
        the band out of this range fails here — which is the intent: the range
        is the measurement, and a moved measurement should be re-measured rather
        than quietly tolerated.
        """
        client = _client()

        funded = client.post(
            "/api/charts/backtest",
            json={**_WINDOW, "strategy": "sma_cross", "initial_capital": _CAPITAL},
        )
        assert funded.status_code == 200, funded.text
        assert funded.json()["metrics"] is not None

        statuses = {}
        refusals = []
        for capital in (1_200, 1_100, 1_050, 1_000, 980, 950):
            resp = client.post(
                "/api/charts/backtest",
                json={**_WINDOW, "strategy": "sma_cross", "initial_capital": capital},
            )
            statuses[capital] = resp.status_code
            if resp.status_code == 422:
                refusals.append((capital, resp.json()))

        assert refusals, (
            f"no capital in {sorted(statuses)} was refused: "
            f"{statuses}. The cash gate is not armed over HTTP, so an "
            f"unaffordable run completes and serves a flattering curve."
        )
        for capital, body in refusals:
            assert body["error"]["code"] == "validation", body
            # The floor's own message, so a 422 from any other cause (an unknown
            # strategy, a bad param) cannot pass for the cash refusal.
            assert "insufficient buying power" in body["error"]["message"], body
            # A refused run is not a partial result.
            assert "metrics" not in body
            assert "equity_curve" not in body
            assert "trades" not in body

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
        assert "sma_cross" in resp.json()["error"]["message"]

    def test_unknown_param_is_422(self):
        resp = _client().post(
            "/api/charts/backtest",
            json={**_WINDOW, "strategy": "sma_cross",
                  "params": {"fast": 5, "slow": 20, "bogus": 1}},
        )
        assert resp.status_code == 422

    def test_composite_workspace_params_from_other_strategies_ignored(self):
        """Composite settings saved by frontend workspace (containing params from
        multiple strategies) must not crash strategy instantiation with unexpected kwargs."""
        resp = _client().post(
            "/api/charts/backtest",
            json={
                **_WINDOW,
                "strategy": "bracket_breakout",
                "params": {
                    "fast": 5,
                    "slow": 20,
                    "period": 14,
                    "atr_period": 14,
                    "lookback": 20,
                    "reward": 2,
                    "stop_atr": 1.5,
                },
            },
        )
        assert resp.status_code == 200, resp.text

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


class TestProtectiveLevelsOnFills:
    """A declared stop/target must reach the chart on the fill that carried it.

    The whole point of the contract: a strategy declares its levels, the engine
    puts them on the order, and the fill reports what the order actually had —
    so a drawn bracket describes a protection that existed rather than one the
    strategy merely intended.
    """

    # The full datalake: the breakout needs ~20 bars of warmup before it can
    # find a range to break, and a thin window would make this vacuous.
    _FULL = {"exchange": "NSE", "symbol": "RELIANCE", "interval": "D",
             "from": 1_700_000_000, "to": 1_900_000_000}

    def test_a_declaring_strategy_reports_its_levels_on_every_entry(self):
        resp = _client().post(
            "/api/charts/backtest",
            json={**self._FULL, "strategy": "bracket_breakout",
                  "params": {"lookback": 10, "atr_period": 5}},
        )
        assert resp.status_code == 200, resp.text
        trades = resp.json()["trades"]
        assert trades, "the breakout must trade over the full datalake window"

        bracketed = 0
        for t in trades:
            if t["stop"] is None and t["target"] is None:
                continue  # an exit, which protects nothing
            bracketed += 1
            assert t["stop"] is not None and t["target"] is not None
            # The legs must bracket the price the order filled at, on the side
            # it filled: the domain refuses anything else at construction, so
            # this also proves the pair survived the pipeline intact.
            if t["side"] == "BUY":
                assert t["stop"] < t["price"] < t["target"]
            else:
                assert t["target"] < t["price"] < t["stop"]
        assert bracketed > 0, "no entry fill carried a bracket"

    def test_a_strategy_that_declares_nothing_reports_null_levels(self):
        """`null` means "no protection was declared", not "protection unknown".

        The chart draws a bracket line only where a level is present, so this
        is the contract that keeps it from inventing one for the two strategies
        that trade signals rather than levels.
        """
        resp = _client().post(
            "/api/charts/backtest", json={**_WINDOW, "strategy": "sma_cross"}
        )
        assert resp.status_code == 200
        for trade in resp.json()["trades"]:
            assert trade["stop"] is None
            assert trade["target"] is None


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
        assert "momentum_scanner" in resp.json()["error"]["message"]


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
    from tradex_strategy.extensions.strategies.sma_cross import (
        SmaCrossStrategy,
    )

    return SmaCrossStrategy(strategy_id="parity_test", instrument=instrument)


def _engine_with_costs(initial_capital: int):
    """The engine AC-2 says a backtest whose numbers will be believed must be.

    Deliberately *not* the route's own ``_backtest_engine``: calling that would
    make the parity assertion agree with whatever the route decides to build,
    which is the opposite of a claim. Written out from the three named parts
    instead, so dropping the risk gate, the fee calculator or the slippage
    model on the route's side is a failing test rather than a silent redefinition
    of what parity means.

    ``fail_closed_cash`` is part of the contract, not a detail: the risk gate
    reads the run's own ledger, and an unavailable balance there must stop a
    BUY rather than wave it through.
    """
    from tradex_execution.fees import FeeCalculator
    from tradex_execution.risk import RiskManager
    from tradex_execution.slippage import PercentageSlippageModel
    from tradex_replay.backtest import BacktestEngine

    risk = RiskManager()
    risk.fail_closed_cash = True
    return BacktestEngine(
        initial_capital=initial_capital,
        risk_manager=risk,
        fee_calculator=FeeCalculator(),
        slippage_model=PercentageSlippageModel(_SLIPPAGE_BPS / Decimal("10000")),
    )
