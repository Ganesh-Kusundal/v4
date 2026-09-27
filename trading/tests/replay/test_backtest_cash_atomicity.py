"""AC-2 — a fill the capital cannot pay for must stop the run, not flatter it.

**Where the money was lost.** The backtest debited its cash ledger from a bus
subscriber on ``OrderFilled``, which runs *after* ``ExecutionEngine`` has already
booked the position. An overdraft raised in that subscriber and the bus's
per-subscriber error isolation swallowed it: the position was booked, no cash
moved, the equity curve came out better than reality, and nothing anywhere
raised. Every number downstream of that curve — return, Sharpe, drawdown — was
wrong in the flattering direction, silently.

**The fix is one ledger and one operation.** The backtest hands its opening
capital to the ``ExecutionEngine`` (``cash=``), so the engine's own
``require_debit`` proves affordability *before* it books anything and then
applies booking, notional and fee as one sequence that cannot half-apply. The
subscriber no longer moves money at all, and an overdraft is re-raised out of
``run()`` instead of being logged and dropped.

**Why fees are in every one of these tests.** The ``RiskManager``'s cash gate
compares a BUY's *notional* against cash, and the ledger floor compares
*notional + fee*. The gap between those two numbers is the only way to reach the
floor, and it is exactly the real case: the last order the account can afford is
the one its own fee makes unaffordable. An engine with costs switched off has an
unreachable floor, which is one more reason the route always passes a
``FeeCalculator``.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tradex_domain import OHLC, Candle, OrderSide, Signal, Timeframe
from tradex_domain.errors import OrderSubmissionUnknownError
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.fees import FeeCalculator
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_execution.risk import RiskManager
from tradex_replay import backtest as backtest_module
from tradex_replay.backtest import BacktestEngine

INSTRUMENT = Equity.of("NSE", "RELIANCE")

#: One share fills at exactly 999.00 on bar 2, so a run opened with 999.00 has
#: exactly enough cash for the notional and not one paisa for the fee. No fee
#: arithmetic is baked into the test: any fee at all tips it over.
FILL_PRICE = Decimal("999.00")
CAPITAL = FILL_PRICE

#: The ledger's own message shape: what of how-much, from which balance, to
#: which. Asserting the whole sentence rather than a substring means the run
#: failed for the affordability reason and not for some unrelated ValueError.
_OVERDRAFT = re.compile(
    r"insufficient buying power: fill \S+ of 999\.\d+ "
    r"would take cash from 999\.00 to -0\.\d+"
)


def _candles() -> list[Candle]:
    out: list[Candle] = []
    for day, price in ((1, "100.00"), (2, "999.00"), (3, "500.00")):
        p = Price(value=Decimal(price))
        out.append(Candle(
            instrument=INSTRUMENT,
            timeframe=Timeframe.D1,
            ohlc=OHLC(open=p, high=p, low=p, close=p),
            volume=Quantity(value=Decimal("1000")),
            timestamp=datetime(2026, 9, day, tzinfo=UTC),
        ))
    return out


class _OneShareOnBarOne:
    """Buys exactly one share on bar 1 — the order that fills at 999.00.

    The size is ``Signal.quantity``, not ``strength``. Every assertion in this
    file is an arithmetic identity on *one* share at 999.00 against 999.00 of
    cash, and it used to get there because ``strength=1.0`` was read as the
    order quantity. It is now read as what it is — signal quality — and the
    size is stated where the data model says a size belongs.
    """

    def __init__(self) -> None:
        self._signals: list[Signal] = []
        self._bar = 0

    @property
    def strategy_id(self) -> str:
        return "one-share"

    @property
    def signals(self) -> list[Signal]:
        return list(self._signals)

    def on_bar(self, context, candle) -> object:
        self._bar += 1
        if self._bar != 1:
            return None
        signal = Signal(
            instrument=candle.instrument,
            direction=OrderSide.BUY,
            strength=1.0,
            quantity=Quantity(value=Decimal("1")),
            reason="bar1",
            timestamp=candle.timestamp,
        )
        self._signals.append(signal)
        return signal

    def on_quote(self, context, quote) -> object:
        return None

    def on_depth(self, context, depth) -> object:
        return None

    def on_fill(self, context, fill) -> None:
        """No-op fill hook."""


def _armed(**kwargs) -> BacktestEngine:
    """A backtest with the gate armed: a risk manager, and therefore a cash
    ledger whose debit floor is live (``allow_negative`` follows the manager)."""
    return BacktestEngine(
        initial_capital=CAPITAL,
        risk_manager=RiskManager(),
        fee_calculator=FeeCalculator(),
        **kwargs,
    )


class TestAnOverdraftStopsTheRun:
    def test_the_fill_the_capital_cannot_pay_for_raises_out_of_run(self) -> None:
        with pytest.raises(ValueError, match="insufficient buying power"):
            _armed().run(_OneShareOnBarOne(), _candles())

    def test_the_raised_error_names_the_exact_debit_that_did_not_fit(self) -> None:
        """Not just "something went wrong": the ledger's own message carries the
        debit, the balance it was taken from, and the negative balance it would
        have produced — 999.00 of notional plus the fee, against 999.00 of cash.
        """
        with pytest.raises(ValueError) as excinfo:
            _armed().run(_OneShareOnBarOne(), _candles())

        assert _OVERDRAFT.search(str(excinfo.value)), str(excinfo.value)

    def test_the_run_stops_at_the_first_unaffordable_fill(self) -> None:
        """The gate is a precheck, so nothing is booked and the run does not
        carry on to the next order: the fill source is asked exactly once."""
        source = SimulatedFillSource()
        with pytest.raises(ValueError, match="insufficient buying power"):
            _armed(fill_source=source).run(_OneShareOnBarOne(), _candles())

        assert source.submit_calls == 1

    def test_the_same_run_without_fees_is_affordable_and_completes(self) -> None:
        """Isolates the cause. Capital is exactly the notional, so the order is
        admitted by the risk gate and cleared by the ledger floor when there is
        no fee to clear it — the run finishes, and it is the fee, not the gate,
        that overdraws."""
        result = BacktestEngine(
            initial_capital=CAPITAL,
            risk_manager=RiskManager(),
        ).run(_OneShareOnBarOne(), _candles())

        assert result.num_trades == 1
        assert [f["price"] for f in result.fills] == [float(FILL_PRICE)]
        assert result.num_rejected == 0

    def test_the_overdraft_is_logged_as_well_as_raised(self, caplog) -> None:
        with caplog.at_level(logging.WARNING), pytest.raises(ValueError):
            _armed().run(_OneShareOnBarOne(), _candles())

        assert "insufficient buying power" in caplog.text


class TestNegativeControlRemoveTheCashArgument:
    """The brief's negative control, and the reason it is worth having.

    Dropping ``cash=`` from the ``ExecutionEngine`` construction removes the
    ``require_debit`` precheck, and with it the only thing in the run that can
    stop an unaffordable order. The run then finishes normally and the
    affordability failure is absorbed — which is the whole defect. Before the
    fix the same removal produced a booked position with no cash movement and a
    flattering curve; now that the backtest keeps ONE ledger (the engine's), the
    missing balance shows up as every order being refused against a zero cash
    reading. Different symptom, same silence: nothing raises either way.

    If the control ever starts raising, the ``cash=`` argument — and not some
    incidental change — is what is doing the work.
    """

    @staticmethod
    def _drop_the_cash_argument(monkeypatch) -> None:
        real = backtest_module.ExecutionEngine

        class _NoCashEngine(real):  # type: ignore[misc, valid-type]
            def __init__(self, *args, **kw):
                kw.pop("cash", None)
                super().__init__(*args, **kw)

        monkeypatch.setattr(backtest_module, "ExecutionEngine", _NoCashEngine)

    def test_without_the_cash_argument_the_overdraft_is_swallowed(
        self, monkeypatch,
    ) -> None:
        self._drop_the_cash_argument(monkeypatch)

        # No pytest.raises: the point is that nothing raises.
        result = _armed().run(_OneShareOnBarOne(), _candles())

        # The run reached a terminal state and reported it, rather than failing
        # on the order it could not pay for.
        assert isinstance(result, backtest_module.BacktestResult)
        assert result.num_trades == 0
        assert [f["rejected"] for f in result.fills] == [True]

    def test_with_the_cash_argument_the_same_run_raises(self) -> None:
        with pytest.raises(ValueError, match="insufficient buying power"):
            _armed().run(_OneShareOnBarOne(), _candles())


class _UnknownOutcomeSource(SimulatedFillSource):
    """A fill source that can leave the process.

    A submit failure from such a source is an *unknown outcome* — the venue may
    have taken the order — so the engine raises rather than filing a rejection.
    That is the one shape of pipeline failure the backtest cannot describe with
    a number, which is why the run has to stop for it.
    """

    reaches_venue = True

    def submit(self, request):  # type: ignore[override]
        self.submit_calls += 1
        raise RuntimeError("the venue may or may not have taken it")


class TestPipelineFailuresAreNotSwallowedEither:
    """The same swallow, one layer up.

    ``ExecutionEngine``'s own order subscriber catches every exception and
    republishes it as ``ErrorOccurred`` — which the bus then isolates like any
    other subscriber. That is right for a live session (one bad order must not
    kill the process) and wrong for a backtest, whose entire output is a number
    an operator is about to trust. ``run()`` re-raises the first one, after
    tearing the pipeline down, so a failed run leaves nothing behind either.
    """

    def test_a_failing_fill_source_raises_out_of_the_run(self) -> None:
        with pytest.raises(
            OrderSubmissionUnknownError, match="the venue may or may not have taken it",
        ):
            BacktestEngine(fill_source=_UnknownOutcomeSource()).run(
                _OneShareOnBarOne(), _candles(),
            )

    def test_the_failure_is_logged_before_it_is_raised(self, caplog) -> None:
        with caplog.at_level(logging.ERROR), pytest.raises(
            OrderSubmissionUnknownError,
        ):
            BacktestEngine(fill_source=_UnknownOutcomeSource()).run(
                _OneShareOnBarOne(), _candles(),
            )

        assert "backtest pipeline failed" in caplog.text
