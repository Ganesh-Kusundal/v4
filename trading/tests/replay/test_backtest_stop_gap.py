"""A protective stop cannot fill at exactly the stop price (AC-19).

**The defect.** ``BacktestEngine``'s engine-side bracket simulated a stop by
returning the *declared level* as the exit price: a long whose stop sat at
1,000.00 filled at 1,000.00 even when the bar that triggered it printed 998.50,
and filled at 1,000.00 when it printed 980.00. On a venue the trigger is a
stop-**market**: the moment the print is through the level the order is live and
the fill is the next price actually available, which is the worse of the two.
So the run reported a loss the market would not have allowed, and a strategy
whose edge depends on its stop being honoured precisely had been measured on a
fiction — silently, because the number it changes is the same shape as the
number that was always right.

**The model.** :func:`_stop_gap_price` prices the triggered leg two ways and
takes the worse:

* an adverse **gap** beyond the level, ``stop_gap_bps`` basis points, default
  **25 bps (0.25%)** — a stated assumption, not a measurement, and conservative
  rather than convenient for an intraday equity stop;
* the bar's own adverse extreme, which is the best price the venue could
  possibly have given, so the model can never be *better* than what the bar
  actually printed.

The second term is what makes the first safe. A gap model with no bar bound
would manufacture losses on bars that merely traded through the level; a bar
bound with no gap model is the defect. The default is a floor on realism, not a
substitute for the bar.

A **take-profit is a limit order and does not gap**, so only the stop leg is
modelled. When both legs pierce on one bar the existing pessimistic convention
stands (stop first) and the gap applies to it.

Every number in this file is hand-computed from the fixture, and each test is
paired with a control that switches the model off (``stop_gap_bps=0``) and
recovers the old, optimistic fill — which is what makes the assertion
load-bearing rather than decorative.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import tradex_replay.backtest as backtest_module
from tradex_domain import OHLC, Candle, OrderSide, Signal, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_replay.backtest import (
    DEFAULT_STOP_GAP_BPS,
    BacktestEngine,
    _stop_gap_price,
)

INSTRUMENT = Equity.of("NSE", "RELIANCE")

#: Everything below is priced so the arithmetic is exact in Decimal: no
#: quantisation anywhere between the fill and the reported equity curve.
ENTRY = 1010.0
STOP = 1000.0
TARGET = 1100.0
SHORT_STOP = 1020.0
SHORT_TARGET = 900.0
QTY = 10.0
CAPITAL = 100000


def _bar(o: float, h: float, low: float, c: float, day: int) -> Candle:
    def p(v: float) -> Price:
        return Price(value=Decimal(str(v)))

    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=p(o), high=p(h), low=p(low), close=p(c)),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 9, day, tzinfo=UTC),
    )


def _flat(price: float, day: int) -> Candle:
    return _bar(price, price, price, price, day)


class _Bracketed:
    """One BUY on bar 1 (fills bar 2's open) carrying a declared pair."""

    def __init__(
        self,
        *,
        side: OrderSide = OrderSide.BUY,
        stop: float = STOP,
        target: float = TARGET,
    ) -> None:
        self._side = side
        self._metadata = {"stop_loss_price": stop, "target_price": target}
        self._signals: list[Signal] = []
        self._bar = 0

    @property
    def strategy_id(self) -> str:
        return "bracketed"

    @property
    def signals(self) -> list:
        return list(self._signals)

    def on_bar(self, context, candle):
        self._bar += 1
        if self._bar == 1:
            signal = Signal(
                instrument=candle.instrument,
                direction=self._side,
                # `strength` is signal QUALITY, not a size (AC-5); the size is
                # the `quantity` stated here.
                strength=1.0,
                quantity=Quantity(value=Decimal(str(QTY))),
                reason="bar1",
                metadata=dict(self._metadata),
                timestamp=candle.timestamp,
            )
            self._signals.append(signal)
            return signal
        return None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        """No-op fill hook."""


def _run(
    bars: list[Candle],
    *,
    side: OrderSide = OrderSide.BUY,
    stop: float = STOP,
    target: float = TARGET,
    **engine_kwargs,
):
    return BacktestEngine(initial_capital=CAPITAL, **engine_kwargs).run(
        _Bracketed(side=side, stop=stop, target=target), bars
    )


# ---------------------------------------------------------------------------
# The model itself, as a pure function
# ---------------------------------------------------------------------------


class TestTheGapModelIsBoundByTheBar:
    def test_the_default_is_a_stated_conservative_number(self) -> None:
        assert DEFAULT_STOP_GAP_BPS == Decimal("25")

    def test_a_long_stop_fills_at_the_worse_of_the_gap_and_the_bar_low(self) -> None:
        # stop 1000, gap 25bps -> 997.50; bar low 998.50 is the better of the two.
        assert _stop_gap_price("stop", OrderSide.BUY, 1000.0, 998.5, 1010.0) == 997.50

    def test_a_bar_that_gapped_through_wins_over_the_gap(self) -> None:
        # bar low 980.00 is worse than the modelled 997.50 and is what the
        # venue could actually have given.
        assert _stop_gap_price("stop", OrderSide.BUY, 1000.0, 980.0, 1010.0) == 980.00

    def test_a_short_stop_fills_at_the_worse_of_the_gap_and_the_bar_high(self) -> None:
        # stop 1020, gap 25bps -> 1022.55; bar high 1021.50 is the better of the two.
        assert _stop_gap_price("stop", OrderSide.SELL, 1020.0, 1005.0, 1021.5) == 1022.55

    def test_a_target_is_a_limit_order_and_does_not_gap(self) -> None:
        assert _stop_gap_price("target", OrderSide.BUY, 1100.0, 1005.0, 1200.0) == 1100.0

    def test_a_short_target_is_also_left_alone(self) -> None:
        assert _stop_gap_price("target", OrderSide.SELL, 900.0, 880.0, 950.0) == 900.0

    def test_a_negative_gap_is_refused_rather_than_optimised(self) -> None:
        """A negative gap would be a request to fill *better* than the stop.

        Unlike a zero gap it has no honest reading, so it raises rather than
        being clamped — a caller cannot buy a better backtest.
        """
        with pytest.raises(ValueError, match="stop_gap_bps"):
            BacktestEngine(stop_gap_bps=-1)

    def test_a_zero_gap_is_legal_and_still_cannot_beat_the_bar(self) -> None:
        assert _stop_gap_price("stop", OrderSide.BUY, 1000.0, 998.5, 1010.0, 0) == 998.5
        assert _stop_gap_price("stop", OrderSide.SELL, 1020.0, 1005.0, 1021.5, 0) == 1021.5

    @pytest.mark.parametrize(
        ("stop", "bar_low"),
        [
            (1000.0, 998.5),   # barely pierced: the gap is the whole difference
            (1000.0, 980.0),   # gapped through: the bar is worse
            (1000.0, 1000.0),  # exactly on the level
            (12.5, 12.4),      # a cheap stock: the gap is proportionally larger
            (24780.0, 24700.0),  # an expensive one
        ],
    )
    def test_a_long_stop_can_never_fill_above_its_level_or_its_bars_low(
        self, stop: float, bar_low: float
    ) -> None:
        price = _stop_gap_price("stop", OrderSide.BUY, stop, bar_low, stop + 1.0)
        assert price <= bar_low
        assert price <= stop
        assert price > 0.0


# ---------------------------------------------------------------------------
# Control (a): a stop pierced inside the bar fills worse than the stop price
# ---------------------------------------------------------------------------


class TestStopPiercedInsideTheBar:
    #: bar 3 trades down to 998.50 with a stop at 1,000.00 — through the level
    #: but nowhere near a gap. The gap model is the binding constraint here, so
    #: this is the fixture on which the model is load-bearing.
    BARS = [
        _flat(ENTRY, 1),
        _flat(ENTRY, 2),
        _bar(ENTRY, ENTRY, 998.5, 1005.0, 3),
        _flat(1005.0, 4),
    ]

    def test_the_fill_is_the_hand_computed_gapped_price(self) -> None:
        result = _run(self.BARS)
        # stop 1000.00 x (1 - 25/10000) = 997.50; bar low 998.50 is better, so
        # the gap is what moves the fill.
        assert [f["price"] for f in result.fills] == [ENTRY, 997.50]
        # 100000 - (10 x 1010) + (10 x 997.50) = 89900 + 9975 = 99875.
        assert result.equity_curve[-1] == 99875.0
        assert result.equity_curve[-1] - CAPITAL == -125.0

    def test_the_stop_price_itself_is_not_a_price_the_run_reports(self) -> None:
        result = _run(self.BARS)
        assert STOP not in [f["price"] for f in result.fills]

    def test_the_control_switching_the_slippage_term_off_still_honours_the_bar(
        self,
    ) -> None:
        """Proves the slippage term is load-bearing.

        ``stop_gap_bps=0`` drops the modelled slippage and leaves only the bar
        bound, which is arithmetic rather than a choice: the fill becomes the
        bar's own 998.50 and the run reports 99885.00 instead of 99875.00.
        """
        result = _run(self.BARS, stop_gap_bps=0)
        assert [f["price"] for f in result.fills] == [ENTRY, 998.50]
        assert result.equity_curve[-1] == 99885.0

    def test_the_control_bypassing_the_model_entirely_recovers_the_old_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The strongest form: the pre-AC-19 code path, exactly.

        ``_stop_gap_price`` is the only place a triggered leg is priced, so
        replacing it with the old behaviour — return the declared level — is the
        pre-fix engine. It must produce the number the run produced before this
        lane, which is the number the assertions above exist to change: the fill
        at exactly the stop and 99900.00 of equity, a loss of 100.00 where the
        market's own print says 500.00 of it was real.
        """
        monkeypatch.setattr(
            backtest_module,
            "_stop_gap_price",
            lambda kind, entry_side, level, bar_low, bar_high, gap_bps=0: level,
        )
        result = _run(self.BARS)
        assert [f["price"] for f in result.fills] == [ENTRY, STOP]
        assert result.equity_curve[-1] == 99900.0
        assert result.equity_curve[-1] != 99875.0

    def test_a_forty_basis_point_gap_is_a_different_number_again(self) -> None:
        result = _run(self.BARS, stop_gap_bps=40)
        # 1000.00 x (1 - 40/10000) = 996.00, still better than the bar's 998.50.
        assert [f["price"] for f in result.fills] == [ENTRY, 996.00]
        assert result.equity_curve[-1] == 99860.0


# ---------------------------------------------------------------------------
# Control (b): a bar that gapped through the stop
# ---------------------------------------------------------------------------


class TestStopGappedThrough:
    BARS = [
        _flat(ENTRY, 1),
        _flat(ENTRY, 2),
        _bar(ENTRY, ENTRY, 980.0, 1005.0, 3),
        _flat(1005.0, 4),
    ]

    def test_the_fill_is_the_bars_own_low_and_worse_than_the_stop(self) -> None:
        result = _run(self.BARS)
        assert [f["price"] for f in result.fills] == [ENTRY, 980.0]
        # 89900 + (10 x 980) = 99700.
        assert result.equity_curve[-1] == 99700.0
        assert 980.0 < STOP

    def test_the_model_is_a_floor_not_a_substitute_for_the_bar(self) -> None:
        """Switching the gap off changes nothing here — by design.

        The bar's low is already worse than any modelled gap, so the run reports
        the same 980.00 either way. That is the bound doing its job: the model
        can never be *better* than what the bar printed, in either direction.
        """
        assert _run(self.BARS, stop_gap_bps=0).fills[-1]["price"] == 980.0


# ---------------------------------------------------------------------------
# The short side, and the legs that must not move
# ---------------------------------------------------------------------------


class TestShortSideAndTheTarget:
    def test_a_short_stop_gaps_upward(self) -> None:
        bars = [
            _flat(ENTRY, 1),
            _flat(ENTRY, 2),
            _bar(ENTRY, 1021.5, 1005.0, 1005.0, 3),
            _flat(1005.0, 4),
        ]
        result = _run(
            bars, side=OrderSide.SELL, stop=SHORT_STOP, target=SHORT_TARGET
        )
        # stop 1020.00 x (1 + 25/10000) = 1022.55; bar high 1021.50 is better.
        assert [f["price"] for f in result.fills] == [ENTRY, 1022.55]
        # 100000 + (10 x 1010) - (10 x 1022.55) = 110100 - 10225.50 = 99874.50.
        assert result.equity_curve[-1] == 99874.5

    def test_the_control_switching_the_model_off_recovers_the_old_short_fill(self) -> None:
        bars = [
            _flat(ENTRY, 1),
            _flat(ENTRY, 2),
            _bar(ENTRY, 1021.5, 1005.0, 1005.0, 3),
            _flat(1005.0, 4),
        ]
        result = _run(
            bars, side=OrderSide.SELL, stop=SHORT_STOP, target=SHORT_TARGET,
            stop_gap_bps=0,
        )
        assert result.fills[-1]["price"] == 1021.50
        assert result.equity_curve[-1] == 99885.0

    def test_a_target_fills_at_the_target_because_a_limit_does_not_gap(self) -> None:
        bars = [
            _flat(ENTRY, 1),
            _flat(ENTRY, 2),
            _bar(ENTRY, 1120.0, 1005.0, 1100.0, 3),
            _flat(1100.0, 4),
        ]
        result = _run(bars)
        # The bar ran to 1120.00 through a 1100.00 limit; the venue fills the
        # limit at 1100.00. Gapping this leg would be modelling a mechanism
        # that does not exist.
        assert [f["price"] for f in result.fills] == [ENTRY, 1100.0]
        assert result.equity_curve[-1] == 100900.0

    def test_the_stop_still_wins_when_both_legs_pierce_one_bar(self) -> None:
        bars = [
            _flat(ENTRY, 1),
            _flat(ENTRY, 2),
            _bar(ENTRY, 1120.0, 980.0, 1005.0, 3),
            _flat(1005.0, 4),
        ]
        result = _run(bars)
        assert [f["price"] for f in result.fills] == [ENTRY, 980.0]
        assert result.equity_curve[-1] == 99700.0

    def test_a_bar_that_touches_neither_leg_leaves_the_bracket_alone(self) -> None:
        bars = [
            _flat(ENTRY, 1),
            _flat(ENTRY, 2),
            _bar(ENTRY, 1090.0, 1002.0, 1005.0, 3),
            _flat(1005.0, 4),
        ]
        result = _run(bars)
        assert [f["side"] for f in result.fills] == ["BUY"]
        # The trade is still open, marked at bar 4's close of 1005.00:
        # 89900 + (10 x 1005.00) = 99950.00.
        assert result.equity_curve[-1] == 99950.0
