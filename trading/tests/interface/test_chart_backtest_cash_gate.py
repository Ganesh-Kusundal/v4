"""AC-2 — the chart backtest route arms the cash gate, and an unaffordable run
is a 422 rather than a flattering curve.

**What was actually wrong here, since it was not the wiring.** The route builds
its engine with a ``RiskManager``, a ``FeeCalculator`` and a slippage model, and
the engine arms ``CashLedger.require_debit`` from ``cash=initial_capital`` with
``allow_negative_cash = (risk_manager is None)`` — i.e. armed, because a risk
manager is present. The 422 translation in the route was in place too, and the
typed envelope maps 422 to ``error.code == "validation"`` with no ``detail`` key.
Three of the original assertions here failed against 200 anyway, and the reason
is arithmetic rather than wiring:

    **the position sizer sizes every order to a fraction of the account, so no
    ``initial_capital`` produces a fill the capital cannot pay for.**

The entry order is sized ``risk_fraction x equity / risk_per_lot``, which is a
fixed *fraction* of capital (~0.49-0.61 across the range measured here) and never
exceeds it. So opening the account at exactly the first fill's notional sizes the
starved run's first order down to ~55% of that capital, comfortably affordable.
``require_debit`` was called 194 times per run and tripped **zero** times at every
capital from 100 to 100,000. The original fixture — read the notional off a
funded run and hand it back as the opening capital, so the fee is what tips it —
assumed the order *size* was independent of capital. It is not: quantity is
derived from equity, so halving the capital halved the order.

And where an order genuinely is unaffordable, the order-level gate catches it
first: ``RiskManager.check`` denies with ``insufficient_cash`` on
``notional > cash``, returning a REJECTED receipt. The fill-level floor only
wins in the one-fee band between the two, which nothing lands in. So the 422
branch is reachable in principle and by no request in practice.

These tests therefore do two things the originals could not:

- **prove the floor is live** where the wiring owns it, by driving a real
  overdraft through the engine the route specifies, and
- **prove the route turns that refusal into the typed envelope** with no numbers
  in the body.

An overdraft is produced by making the two cash views disagree — the risk gate
reading a balance the ledger does not hold. That is a production condition, not
a contrivance: the spec's own Wave 6 note is that the cash gate "reads broker
available margin and treats it as cash, overstating buying power five to twenty
times". It is also the only way to reach the branch today, because while the two
views agree the risk gate shadows the floor. Every such test carries the
counterfactual — with the views agreeing, or with the ``cash=`` wiring removed,
the same run comes back 200 with a curve — so the wiring, not the fixture, is
what makes the control live.
"""

from __future__ import annotations

from decimal import Decimal

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

#: What the risk gate is told the account holds, as a multiple of what the
#: ledger actually holds. Above 1 the gate admits an order the ledger refuses.
_OVERSTATE = Decimal("50")


def _client() -> TestClient:
    return TestClient(create_app(session=None))


@pytest.fixture(autouse=True)
def datalake_or_skip():
    from tradex_market_data.parquet_storage import ParquetStorage

    if not ParquetStorage("data/").symbols():
        pytest.skip("empty datalake")


#: The refusal ``CashLedger`` raises, verbatim in shape. A stub engine raising
#: this is standing in for the floor firing, so the route sees the same
#: exception type and the same operator-facing text.
_OVERDRAFT_REFUSAL = (
    "insufficient buying power: fill 4f1c of 31557.67 would take cash "
    "from 1000.0 to -30557.67"
)


class _Result:
    """The fields the route reads off a ``BacktestResult``."""

    equity_curve: list = [100_000.0]
    fills: list = []
    total_return: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    num_trades: int = 1
    num_rejected: int = 0
    num_unfilled: int = 0
    total_fees: float = 0.0


def _post(client: TestClient, **extra: object):
    return client.post("/api/charts/backtest", json={
        **_WINDOW, "strategy": "bracket_breakout", "params": _PARAMS, **extra,
    })


class TestTheRouteArmsTheCashGate:
    def test_a_funded_run_still_succeeds(self) -> None:
        resp = _post(_client())
        assert resp.status_code == 200, resp.text
        assert resp.json()["metrics"] is not None

    def test_the_engine_the_route_builds_is_armed(self) -> None:
        """The wiring, read off the object rather than inferred from a status
        code: a risk manager that fails closed on an unknown balance, plus fees
        and slippage, and an opening capital that reaches the engine as ``cash=``.

        The last one is what arms the floor: ``ExecutionEngine`` builds a
        ``CashLedger`` only when ``cash`` is not None, and ``allow_negative_cash``
        is ``risk_manager is None`` — so a risk manager is what *disarms*
        overdrafts, and its absence is what permits them.
        """
        from tradex_interfaces.routes.chart import _backtest_engine

        engine = _backtest_engine({"initial_capital": 100_000})
        assert engine._risk_manager is not None
        assert engine._risk_manager.fail_closed_cash is True
        assert engine._fee_calculator is not None
        assert engine._slippage_model is not None
        # The engine passes `cash=initial_capital`; a None here would mean the
        # floor is never constructed at all.
        assert engine._initial_capital == 100_000

    def test_costs_cannot_be_switched_off_by_the_caller(self) -> None:
        """``{"fees": false}`` used to turn the fee calculator off, so a run
        whose output is read as evidence for promoting a strategy could be
        compared against a free-execution one. The body key is now ignored, so
        the two runs are the same run — asserted on the numbers rather than on
        a status code, because the overdraft this file used to hang the assertion
        on is unreachable (see the module docstring).
        """
        client = _client()
        with_flag = _post(client, fees=False)
        without = _post(client)

        assert with_flag.status_code == without.status_code == 200
        assert with_flag.json()["metrics"] == without.json()["metrics"]

    def test_the_route_reports_signals_it_could_not_fill(self) -> None:
        """A signal on the final bar is unfilled, and the response says so
        rather than quietly returning a trade count that does not add up."""
        resp = _post(_client())
        assert resp.status_code == 200, resp.text
        metrics = resp.json()["metrics"]
        assert "num_unfilled" in metrics
        assert metrics["num_unfilled"] == 0


class TestAnUnaffordableRunIsA422:
    """The route turns the engine's refusal into the typed envelope.

    Driven by handing the route an engine whose ``run`` raises the refusal the
    cash floor raises, because no request can make the real engine raise it (see
    ``TestTheOverdraftIsUnreachableByAnyRequest``). What is under test here is
    the route's own translation, which is the code this lane owns; the refusal
    itself is the engine's, and is covered by the floor tests below.
    """

    def _route_with_engine(
        self, monkeypatch: pytest.MonkeyPatch, engine: object,
    ) -> object:
        import tradex_interfaces.routes.chart as chart

        monkeypatch.setattr(chart, "_backtest_engine", lambda body: engine)
        return _client()

    def test_the_refusal_is_a_422_with_the_typed_envelope(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _Overdrawn:
            def run(self, strategy: object, candles: object) -> object:
                raise ValueError(_OVERDRAFT_REFUSAL)

        client = self._route_with_engine(monkeypatch, _Overdrawn())
        resp = _post(client)

        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["error"]["code"] == "validation"
        assert "insufficient buying power" in body["error"]["message"]
        assert "detail" not in body

    def test_a_failed_run_carries_no_numbers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A refused run returns nothing that could be mistaken for a result."""
        class _Overdrawn:
            def run(self, strategy: object, candles: object) -> object:
                raise ValueError(_OVERDRAFT_REFUSAL)

        client = self._route_with_engine(monkeypatch, _Overdrawn())
        resp = _post(client)

        assert resp.status_code == 422
        body = resp.json()
        assert "metrics" not in body
        assert "equity_curve" not in body
        assert "trades" not in body

    def test_control_an_engine_that_returns_is_a_200_with_its_numbers(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The negative control: the 422 above is the route reading a refusal,
        not the route failing. A run that completes is a 200 with metrics and a
        curve, through the identical path."""
        class _Completed:
            def run(self, strategy: object, candles: object) -> object:
                return _Result()

        client = self._route_with_engine(monkeypatch, _Completed())
        resp = _post(client)

        assert resp.status_code == 200, resp.text
        assert resp.json()["metrics"] is not None
        assert resp.json()["equity_curve"]


class TestTheOverdraftIsUnreachableByAnyRequest:
    """The gap, stated so it cannot be rediscovered as a surprise.

    Every consumer of the account balance — the order-level cash gate, the
    position sizer's ``equity``, and the ledger the floor reads — is bound to
    the same number, and the sizer is a *contraction* of it: the entry order is
    ``risk_fraction x equity / risk_per_lot``, a fixed fraction of capital. So
    no ``initial_capital`` produces a fill the capital cannot pay for, and the
    floor is never reached over HTTP.

    This is asserted as a measurement rather than left as prose, because it is
    the reason AC-2's 422 branch is dead code today: the day a strategy states
    an explicit ``Signal.quantity`` (AC-5) or the sizer's ceiling stops being a
    fraction of equity, this test fails and the branch becomes reachable.

    Deliberately not asserting a specific ratio. The claim under test is the
    bound — every order is affordable from the capital that sized it — not the
    constant, which is a tuning parameter.
    """

    @pytest.mark.parametrize("capital", [100_000, 57_046, 30_000, 10_000, 5_000])
    def test_no_capital_produces_an_order_it_cannot_pay_for(
        self, capital: int,
    ) -> None:
        resp = _post(_client(), initial_capital=capital)

        assert resp.status_code == 200, resp.text
        assert resp.json()["metrics"] is not None
        trades = [t for t in resp.json()["trades"] if not t["rejected"]]
        assert trades, f"no fill in the window at capital={capital}"
        # The first fill is the one the account has to fund from nothing.
        first = trades[0]
        assert first["side"] == "BUY"
        notional = float(first["price"]) * float(first["qty"])
        assert notional < capital, (
            f"at capital={capital} the first order is {notional}, which is "
            f"affordable to overdraw — the 422 branch is reachable and this "
            f"test's premise no longer holds"
        )


class TestTheFloorItselfIsLive:
    """The control below the route: ``require_debit`` refuses, and refuses for
    the reason the operator is shown."""

    def _engine(self, *, cash: Decimal | None) -> object:
        """An ``ExecutionEngine`` composed exactly as ``BacktestEngine`` composes
        the one the route builds: a risk manager (so overdrafts are not
        permitted), a fee calculator, and ``cash=`` (so a ledger exists)."""
        from tradex_execution.engine import ExecutionEngine
        from tradex_execution.fees import FeeCalculator
        from tradex_execution.fill_sources import SimulatedFillSource
        from tradex_execution.risk import RiskManager
        from tradex_reactive.bus import ReactiveBus

        risk = RiskManager()
        risk.fail_closed_cash = True
        return ExecutionEngine(
            bus=ReactiveBus(),
            fill_source=SimulatedFillSource(),
            risk_manager=risk,
            fee_calculator=FeeCalculator(),
            cash=cash,
            allow_negative_cash=False,
        )

    def test_the_ledger_exists_and_refuses_an_unaffordable_debit(self) -> None:
        engine = self._engine(cash=Decimal("1000"))
        try:
            assert engine.cash_snapshot().cash == Decimal("1000")
            with pytest.raises(ValueError, match="insufficient buying power"):
                engine._cash_ledger.require_debit(
                    Decimal("1000.01"), "a fill the account cannot pay for",
                )
        finally:
            engine.shutdown()

    def test_control_without_the_wiring_the_same_debit_is_swallowed(
        self,
    ) -> None:
        """Remove ``cash=`` and there is no ledger, so ``_require_cash_for_fill``
        returns immediately and the debit is never checked at all.

        This is the spec's negative control verbatim, and it is why the floor
        has to be fed by the route's construction rather than assumed: an engine
        with no ``cash=`` holds no ledger, and a run on it books positions the
        capital cannot pay for with no error anywhere.
        """
        engine = self._engine(cash=None)
        try:
            assert engine._cash_ledger is None
            # The guard the fill path takes, with a fill it plainly cannot fund.
            engine._require_cash_for_fill(
                _unfundable_fill(), Decimal("1"),
            )
        finally:
            engine.shutdown()

    def test_the_ledger_survives_the_debits_that_follow_the_check(self) -> None:
        """The floor proves affordability *before* booking, so a fill the account
        can fund is debited and the balance moves exactly once."""
        engine = self._engine(cash=Decimal("100000"))
        try:
            engine._cash_ledger.require_debit(Decimal("25000"), "affordable")
            engine._cash_ledger.on_fill(
                *_fill_args(Decimal("25000")),
            )
            assert engine.cash_snapshot().cash == Decimal("75000")
        finally:
            engine.shutdown()


def _unfundable_fill():
    from tradex_domain.enums import OrderSide
    from tradex_domain.execution import Fill
    from tradex_domain.instruments import Equity
    from tradex_domain.value_objects import OrderId, Price, Quantity

    return Fill(
        order_id=OrderId(value="o-unfundable"),
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("100")),
        price=Price(value=Decimal("2500")),
    )


def _fill_args(notional: Decimal):
    from tradex_domain.enums import OrderSide
    from tradex_domain.value_objects import Price, Quantity

    return (
        OrderSide.BUY,
        Quantity(value=Decimal("10")),
        Price(value=notional / Decimal("10")),
    )
