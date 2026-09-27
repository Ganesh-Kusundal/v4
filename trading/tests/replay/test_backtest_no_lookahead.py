"""AC-1 — a backtest may not fill a signal at a price the strategy could not
have known when it decided.

**The rule, and it is the only one.** A signal decided on bar N fills at bar
N+1's *open*, whichever of the two paths carries it: the ``next_open`` queue in
``ReactiveStrategyEngine`` (a strategy that RETURNS its signal) or the
recording-only bridge in ``BacktestEngine`` (a strategy that only appends to
``strategy.signals``). Before this, the two paths disagreed — the queue was
flushed against the *last* candle of the run and the bridge filled at the
*signal bar's own close* — so which path a signal took decided whether the
strategy looked good, and nothing said so.

A signal whose next bar never arrives — the final bar of the run, or a quote
after the last candle — cannot be filled at all inside the run. It is counted in
``BacktestResult.num_unfilled`` and logged, never filled at a price that did not
exist yet when the strategy decided.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tradex_domain import OHLC, Candle, OrderSide, Quote, Signal, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_replay.backtest import BacktestEngine

INSTRUMENT = Equity.of("NSE", "RELIANCE")

# Every bar's open, high, low and close are four different numbers, so each
# candidate fill price is distinguishable from the others by inspection:
#
#   bar 1: open 100.00  high 110.00  low  90.00  close 105.00
#   bar 2: open 200.00  high 210.00  low 190.00  close 205.00
#   bar 3: open 300.00  high 310.00  low 290.00  close 305.00
#
# A signal decided on bar 1 therefore has four wrong answers available to it —
# 100.00 (its own open, the final-bar flush on a one-bar run), 105.00 (its own
# close, the recording-only bridge), 300.00 (the last candle of the run, the
# final-bar flush) and 205.00 (the wrong bar's close) — and exactly one right
# one: 200.00, bar 2's open.
BARS = [(1, 100, 110, 90, 105), (2, 200, 210, 190, 205), (3, 300, 310, 290, 305)]


def _candles() -> list[Candle]:
    out: list[Candle] = []
    for day, o, h, low_, c in BARS:
        out.append(Candle(
            instrument=INSTRUMENT,
            timeframe=Timeframe.D1,
            ohlc=OHLC(
                open=Price(value=Decimal(o)),
                high=Price(value=Decimal(h)),
                low=Price(value=Decimal(low_)),
                close=Price(value=Decimal(c)),
            ),
            volume=Quantity(value=Decimal("1000")),
            timestamp=datetime(2026, 9, day, tzinfo=UTC),
        ))
    return out


def _quote(day: int, ltp: str) -> Quote:
    return Quote(
        instrument=INSTRUMENT,
        ltp=Price(value=Decimal(ltp)),
        timestamp=datetime(2026, 9, day, 10, 0, tzinfo=UTC),
    )


class _SignalsOnBar:
    """Signals on one bar, either by RETURNING it (engine path) or by only
    appending it to ``signals`` (recording-only bridge path).

    ``quantity`` is the signal strength, and the strength is the order quantity
    on both paths, so 1.0 means exactly one share.
    """

    def __init__(
        self, bar: int | tuple[int, ...], *, recording: bool, quantity: float = 1.0,
    ) -> None:
        self._bars = (bar,) if isinstance(bar, int) else tuple(bar)
        self._recording = recording
        self._quantity = quantity
        self._signals: list[Signal] = []
        self._bar = 0

    @property
    def strategy_id(self) -> str:
        return f"signal-on-bar-{'+'.join(str(b) for b in self._bars)}"

    @property
    def signals(self) -> list[Signal]:
        return list(self._signals)

    def on_bar(self, context, candle) -> object:
        self._bar += 1
        if self._bar not in self._bars:
            return None
        signal = Signal(
            instrument=candle.instrument,
            direction=OrderSide.BUY,
            strength=self._quantity,
            reason=f"bar{self._bar}",
            timestamp=candle.timestamp,
        )
        self._signals.append(signal)
        return None if self._recording else signal

    def on_quote(self, context, quote) -> object:
        return None

    def on_depth(self, context, depth) -> object:
        return None

    def on_fill(self, context, fill) -> None:
        """No-op fill hook."""


class _SignalOnQuote:
    """Signals on a quote, recording only — the bridge path for quote signals."""

    def __init__(self, quantity: float = 1.0) -> None:
        self._quantity = quantity
        self._signals: list[Signal] = []

    @property
    def strategy_id(self) -> str:
        return "signal-on-quote"

    @property
    def signals(self) -> list[Signal]:
        return list(self._signals)

    def on_bar(self, context, candle) -> object:
        return None

    def on_quote(self, context, quote) -> object:
        signal = Signal(
            instrument=quote.instrument,
            direction=OrderSide.BUY,
            strength=self._quantity,
            reason="quote",
            timestamp=quote.timestamp,
        )
        self._signals.append(signal)
        return None

    def on_depth(self, context, depth) -> object:
        return None

    def on_fill(self, context, fill) -> None:
        """No-op fill hook."""


class TestFillBarIsTheBarAfterTheSignal:
    """The hand-computed expectation: 200.00, bar 2's open."""

    def test_a_returned_signal_fills_at_the_next_bars_open(self) -> None:
        result = BacktestEngine().run(
            _SignalsOnBar(1, recording=False), _candles(),
        )

        assert [f["price"] for f in result.fills] == [200.0]
        assert result.num_trades == 1
        assert result.num_unfilled == 0

    def test_a_recorded_signal_fills_at_the_next_bars_open_too(self) -> None:
        """The recording-only bridge used to fill at the SIGNAL bar's close.
        Both paths now fill at the same bar, so which one a strategy emits can
        no longer decide whether it looks good."""
        result = BacktestEngine().run(
            _SignalsOnBar(1, recording=True), _candles(),
        )

        assert [f["price"] for f in result.fills] == [200.0]
        assert result.num_trades == 1
        assert result.num_unfilled == 0

    @pytest.mark.parametrize("recording", [False, True], ids=["returned", "recorded"])
    def test_no_unknowable_price_is_ever_a_fill_price(self, recording: bool) -> None:
        """The three wrong answers, named: the signal bar's own open (100.00),
        the signal bar's own close (105.00), and the last candle of the run
        (300.00, the final-bar flush)."""
        result = BacktestEngine().run(
            _SignalsOnBar(1, recording=recording), _candles(),
        )

        prices = [f["price"] for f in result.fills]
        assert 100.0 not in prices, "filled at the signal bar's own open"
        assert 105.0 not in prices, "filled at the signal bar's own close"
        assert 300.0 not in prices, "filled against the last candle of the run"
        assert prices == [200.0]

    def test_a_quote_signal_fills_at_the_next_candles_open(self) -> None:
        """A quote is a decision point like any other: the next bar of its
        instrument after it is the first price that could exist when the
        strategy decided. Here the quote lands between bar 1 and bar 2, so bar
        2's open (200.00) is the fill — not bar 1's close (105.00)."""
        result = BacktestEngine().run(
            _SignalOnQuote(),
            [_candles()[0], _quote(1, "150.00"), _candles()[1]],
        )

        assert [f["price"] for f in result.fills] == [200.0]
        assert result.num_unfilled == 0

    def test_a_quote_before_the_first_bar_fills_at_that_bars_open(self) -> None:
        """The same rule at the start of the run: a signal decided before bar 1
        fills at bar 1's open, the first bar that exists after the decision."""
        result = BacktestEngine().run(
            _SignalOnQuote(),
            [_quote(1, "150.00"), _candles()[0]],
        )

        assert [f["price"] for f in result.fills] == [100.0]
        assert result.num_unfilled == 0



class TestFinalBarSignalIsUnfilledNotOptimistic:
    """A signal on the last bar has no next bar, so the run cannot fill it."""

    @pytest.mark.parametrize("recording", [False, True], ids=["returned", "recorded"])
    def test_a_signal_on_the_final_bar_produces_no_fill(self, recording: bool) -> None:
        result = BacktestEngine().run(
            _SignalsOnBar(3, recording=recording), _candles(),
        )

        assert result.fills == []
        assert result.num_trades == 0
        # The signal is still in the audit trail — it was emitted, just not filled.
        assert [s.reason for s in result.trades] == ["bar3"]

    @pytest.mark.parametrize("recording", [False, True], ids=["returned", "recorded"])
    def test_the_run_summary_reports_it_as_unfilled(self, recording: bool) -> None:
        result = BacktestEngine().run(
            _SignalsOnBar(3, recording=recording), _candles(),
        )

        assert result.num_unfilled == 1
        # No phantom position either: the equity curve is flat, so nothing was
        # booked at a price the run never saw.
        assert result.equity_curve == [100000.0] * 4

    def test_the_unfilled_count_is_logged_not_just_counted(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="tradex_replay.backtest"):
            BacktestEngine().run(_SignalsOnBar(3, recording=True), _candles())

        assert "unfilled" in caplog.text.lower()

    def test_a_quote_after_the_last_candle_is_unfilled(self) -> None:
        """The old final-bar flush existed for exactly this case: a signal
        raised by an event that has no bar after it. It was filled at that last
        bar's own open — a price the strategy could not have known."""
        result = BacktestEngine().run(
            _SignalOnQuote(),
            [*_candles(), _quote(4, "150.00")],
        )

        assert result.fills == []
        assert result.num_trades == 0
        assert result.num_unfilled == 1
        assert result.equity_curve == [100000.0] * 4

    def test_an_earlier_signal_still_fills_while_the_final_bar_one_does_not(self) -> None:
        """Only the signal with no bar after it is dropped: bar 2's fills at
        bar 3's open (300.00) and bar 3's is reported unfilled."""
        result = BacktestEngine().run(
            _SignalsOnBar((2, 3), recording=True), _candles(),
        )

        assert [f["price"] for f in result.fills] == [300.0]
        assert result.num_unfilled == 1
