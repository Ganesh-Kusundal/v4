"""Defect 3 — the backtest route must arm the gate the cash fix depends on.

``POST /api/charts/backtest`` built ``BacktestEngine(initial_capital=..., **kwargs)``
where ``kwargs`` carried a fee calculator only if the request body asked for
fees. No risk manager, no slippage model, no cash ledger. So the AC-2 repair was
invisible from the one entry point an operator actually uses: a run that
overdrew its capital still came back 200 with a flattering curve.

These tests go through the HTTP surface, because that is the surface the defect
was about, and the overdraft is produced from the platform's own numbers — the
first fill's notional is read off a funded run and handed back as the opening
capital — so no price is hard-coded and none of it depends on the datalake
happening to hold a particular series.

**Why the fee is what makes the overdraft reachable at all.** The
``RiskManager``'s cash gate compares a BUY's *notional* against cash; the
ledger's debit floor compares *notional + fee*. Opening the account at exactly
the first fill's notional therefore passes the risk gate and fails the floor,
which is the real-world case: the last order an account can afford is the one
its own fee makes unaffordable. A run with costs switched off cannot reach the
floor, so this test also proves the fee calculator is wired.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from tradex_interfaces.fastapi_app import create_app  # noqa: E402

pytest.importorskip("pandas")

# Wide enough that the daily indicators warm up and the strategy actually
# trades. ``bracket_breakout`` is the one shipped strategy whose first order is
# always a BUY: its exits are engine-generated protective legs, so nothing can
# credit the account before the first entry.
_WINDOW = {
    "exchange": "NSE", "symbol": "RELIANCE", "interval": "D",
    "from": 1_700_000_000, "to": 1_900_000_000,
}
_PARAMS = {"lookback": 10, "atr_period": 5}


def _client() -> TestClient:
    return TestClient(create_app(session=None))


@pytest.fixture(autouse=True)
def datalake_or_skip():
    from tradex_market_data.parquet_storage import ParquetStorage

    if not ParquetStorage("data/").symbols():
        pytest.skip("empty datalake")


def _first_fill_notional(client: TestClient) -> float:
    """The notional of the first fill of a comfortably funded run."""
    resp = client.post("/api/charts/backtest", json={
        **_WINDOW, "strategy": "bracket_breakout", "params": _PARAMS,
    })
    assert resp.status_code == 200, resp.text
    fills = [t for t in resp.json()["trades"] if not t["rejected"]]
    if not fills:
        pytest.skip("no fill in this window")
    first = fills[0]
    assert first["side"] == "BUY", "the first order must be a BUY for this test to mean anything"
    return float(first["price"]) * float(first["qty"])


class TestTheRouteArmsTheCashGate:
    def test_a_funded_run_still_succeeds(self) -> None:
        resp = _client().post("/api/charts/backtest", json={
            **_WINDOW, "strategy": "bracket_breakout", "params": _PARAMS,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["metrics"] is not None

    def test_a_run_that_cannot_pay_for_its_first_fill_is_a_422(self) -> None:
        """The gate is armed, end to end, over HTTP.

        Opening capital equal to the first fill's notional buys exactly the
        notional and nothing for its fee, so the RiskManager's cash gate admits
        the order (notional ≤ cash) and the ledger's debit floor refuses it
        (notional + fee > cash). That combination is only reachable when the
        route passes a risk manager, a fee calculator AND the engine's
        ``cash=``, so the 422 is evidence about the wiring and not about a
        fixture.
        """
        client = _client()
        notional = _first_fill_notional(client)

        resp = client.post("/api/charts/backtest", json={
            **_WINDOW, "strategy": "bracket_breakout", "params": _PARAMS,
            "initial_capital": notional,
        })

        assert resp.status_code == 422, resp.text
        body = resp.json()
        # The repository's typed error envelope, not a bare FastAPI `detail`.
        assert body["error"]["code"] == "validation"
        assert "insufficient buying power" in body["error"]["message"]
        assert "detail" not in body

    def test_the_overdraft_is_not_a_500_and_carries_no_numbers(self) -> None:
        """A failed run returns nothing that could be mistaken for a result."""
        client = _client()
        notional = _first_fill_notional(client)

        resp = client.post("/api/charts/backtest", json={
            **_WINDOW, "strategy": "bracket_breakout", "params": _PARAMS,
            "initial_capital": notional,
        })

        assert resp.status_code == 422
        assert "metrics" not in resp.json()
        assert "equity_curve" not in resp.json()

    def test_the_engine_the_route_builds_is_armed(self) -> None:
        """The wiring, read off the object rather than inferred from a status
        code: a risk manager that fails closed on an unknown balance, plus fees
        and slippage."""
        from tradex_interfaces.routes.chart import _backtest_engine

        engine = _backtest_engine({"initial_capital": 100_000})
        assert engine._risk_manager is not None
        assert engine._risk_manager.fail_closed_cash is True
        assert engine._fee_calculator is not None
        assert engine._slippage_model is not None

    def test_costs_cannot_be_switched_off_by_the_caller(self) -> None:
        """``{"fees": false}`` used to turn the fee calculator off, so a run
        whose output is read as evidence for promoting a strategy could be
        compared against a free-execution one. The body key is now ignored."""
        client = _client()
        notional = _first_fill_notional(client)

        resp = client.post("/api/charts/backtest", json={
            **_WINDOW, "strategy": "bracket_breakout", "params": _PARAMS,
            "initial_capital": notional, "fees": False,
        })

        assert resp.status_code == 422, resp.text
        assert "insufficient buying power" in resp.json()["error"]["message"]

    def test_the_route_reports_signals_it_could_not_fill(self) -> None:
        """A signal on the final bar is unfilled, and the response says so
        rather than quietly returning a trade count that does not add up."""
        resp = _client().post("/api/charts/backtest", json={
            **_WINDOW, "strategy": "bracket_breakout", "params": _PARAMS,
        })
        assert resp.status_code == 200, resp.text
        metrics = resp.json()["metrics"]
        assert "num_unfilled" in metrics
        assert metrics["num_unfilled"] == 0
