"""Corporate actions in the backtest: point in time, and re-basing the bracket
(AC-19).

**What was already here, and what was not.** The wiring the lane asks for is
*not* absent: ``_apply_actions_due`` has always delegated to
``PositionAccountant.on_corporate_action`` (the same ``position_math.apply_split``
/ ``apply_dividend`` kernel the reactive pipeline uses), and the on-a-bar
ex-date case was already covered. Three things were still wrong, and all three
make a run look better than the venue would allow:

1. **A split re-based the position but not the open bracket.** The stop and
   target live in the *pre-split* price scale. After a 2:1 split the position is
   20 shares at 500.00 while the bracket still says *stop 950.00, sell 10* — so
   on the very next bar, in a market trading at 500.00, the stop is "pierced" and
   the backtest sells at **950.00**, a fill 90% above the market, on half the
   position. That is a larger optimism than the one the brief describes, and
   applying the split without re-basing the bracket *creates* it.
2. **An action was applied to a lot that did not exist at its ex-date.** A
   position opened after the ex-date was still re-based, which doubled a lot
   that never held pre-split shares — a phantom gain.
3. **The end-of-run position was not reported**, so neither a split nor a
   dividend was observable in the run's output at all: the only evidence a
   corporate action had been applied was the equity curve moving.

**The control this file exists for.** A run containing a 2:1 split must produce
the same end-of-run position and the same profit and loss as an otherwise
identical run over the *same series with the pre-split history already divided
by the ratio and no action at all*. Every figure asserted below is computed by
hand from the fixture before the code is consulted; the raw run and the
hand-adjusted run are asserted to the same number, **100500.00**, and the run
with no corporate-action store is asserted to a different one, **95200.00**.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import OHLC, Candle, OrderSide, Signal, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_market_data.corporate_actions import CorporateAction, CorporateActionStore
from tradex_replay.backtest import BacktestEngine

INSTRUMENT = Equity.of("NSE", "RELIANCE")
CAPITAL = 100000


def _flat(price: float, day: int) -> Candle:
    def p(v: float) -> Price:
        return Price(value=Decimal(str(v)))

    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=p(price), high=p(price), low=p(price), close=p(price)),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 9, day, tzinfo=UTC),
    )


def _dip(low: float, day: int) -> Candle:
    """Day 6 opens at the post-split 500.00 and dips to *low*.

    The dip is placed just through the re-based stop of 475.00 but nowhere near
    the pre-split level of 950.00, so only a bracket that was re-based with the
    position can see it.
    """
    def p(v: float) -> Price:
        return Price(value=Decimal(str(v)))

    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=p(500.0), high=p(500.0), low=p(low), close=p(low)),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 9, day, tzinfo=UTC),
    )


class _Ladder:
    """BUY *buy_qty* on bar 1, SELL *sell_qty* on bar ``sell_bar``.

    ``sell_qty=None`` emits no exit at all, for the cases where the position is
    meant to still be open at the end of the run. Sizes are stated on the signal
    as ``quantity`` — ``strength`` is signal *quality*, not a size (AC-5).
    """

    def __init__(
        self,
        *,
        buy_qty: float,
        sell_qty: float | None,
        sell_bar: int,
        buy_bar: int = 1,
        metadata=None,
    ) -> None:
        self._buy_qty = buy_qty
        self._buy_bar = buy_bar
        self._sell_qty = sell_qty
        self._sell_bar = sell_bar
        self._metadata = dict(metadata or {})
        self._signals: list[Signal] = []
        self._bar = 0

    @property
    def strategy_id(self) -> str:
        return "ladder"

    @property
    def signals(self) -> list:
        return list(self._signals)

    def _emit(self, direction: OrderSide, qty: float, candle) -> Signal:
        signal = Signal(
            instrument=candle.instrument,
            direction=direction,
            strength=1.0,
            quantity=Quantity(value=Decimal(str(qty))),
            reason=f"bar{self._bar}",
            metadata=dict(self._metadata),
            timestamp=candle.timestamp,
        )
        self._signals.append(signal)
        return signal

    def on_bar(self, context, candle):
        self._bar += 1
        if self._bar == self._buy_bar:
            return self._emit(OrderSide.BUY, self._buy_qty, candle)
        if self._sell_qty is not None and self._bar == self._sell_bar:
            return self._emit(OrderSide.SELL, self._sell_qty, candle)
        return None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        """No-op fill hook."""


def _store(*actions: CorporateAction) -> CorporateActionStore:
    store = CorporateActionStore()
    for action in actions:
        store.add_typed(action)
    return store


def _split(day: int, ratio: float = 2.0) -> CorporateAction:
    return CorporateAction(
        instrument="RELIANCE",
        action_type="SPLIT",
        ex_date=f"2026-09-{day:02d}",
        ratio=ratio,
    )


def _dividend(day: int, per_share: float) -> CorporateAction:
    return CorporateAction(
        instrument="RELIANCE",
        action_type="DIVIDEND",
        ex_date=f"2026-09-{day:02d}",
        amount=per_share,
    )


def _only_position(result) -> dict:
    assert len(result.end_positions) == 1, result.end_positions
    return result.end_positions[0]


# ---------------------------------------------------------------------------
# The control: a 2:1 split must equal the same series already split by hand
# ---------------------------------------------------------------------------
#
# RAW RUN — the series as the market printed it, plus the action.
#   day 1..4  1000.00   pre-split
#   day 5      500.00   ex-date; the market is already trading ex-split all day
#   day 6      520.00
#   day 7      530.00
#   BUY 10 on bar 1 -> fills bar 2's open at 1000.00; cash 100000 - 10000 = 90000
#   split day 5    -> 20 @ 500.00; cost basis 20 x 500.00 = 10000, unchanged
#   SELL 10 on bar 5 -> fills bar 6's open at 520.00
#                    -> realised (520.00 - 500.00) x 10 = +200.00
#                    -> cash 90000 + 5200.00 = 95200.00; 10 @ 500.00 left
#   mark at day 7's close 530.00 -> 10 x 530.00 = 5300.00
#   FINAL EQUITY = 95200.00 + 5300.00 = 100500.00; net P&L = +500.00
#
# HAND-ADJUSTED RUN — the same series with the pre-split history divided by the
# ratio and no action, expressing the same economic exposure as 20 shares.
#   day 1..5   500.00   (1000.00 pre-split history / 2)
#   day 6      520.00
#   day 7      530.00
#   BUY 20 on bar 1 -> fills bar 2's open at 500.00; cash 100000 - 10000 = 90000
#   SELL 10 on bar 5 -> fills bar 6's open at 520.00; realised +200.00
#                     cash 95200.00; 10 @ 500.00 left
#   FINAL EQUITY = 100500.00; net P&L = +500.00
#
# NOT APPLIED — the same raw series with no store: 10 @ 1000.00 never re-based,
#   SELL 10 @ 520.00 -> realised (520.00 - 1000.00) x 10 = -4800.00
#   cash 95200.00, flat -> 95200.00; net P&L = -4800.00
RAW_SPLIT_RUN_EQUITY = 100500.0
NOT_APPLIED_EQUITY = 95200.0

_RAW_BARS = (
    [_flat(1000.0, d) for d in (1, 2, 3, 4)]
    + [_flat(500.0, 5), _flat(520.0, 6), _flat(530.0, 7)]
)
_ADJUSTED_BARS = (
    [_flat(500.0, d) for d in (1, 2, 3, 4, 5)] + [_flat(520.0, 6), _flat(530.0, 7)]
)


def _raw_split_run(bars: list[Candle], *, sell_bar: int = 5, store=None):
    return BacktestEngine(
        initial_capital=CAPITAL, corporate_actions=store
    ).run(
        _Ladder(buy_qty=10.0, sell_qty=10.0, sell_bar=sell_bar),
        bars,
    )


class TestSplitEqualsTheHandAdjustedSeries:
    def test_a_split_on_a_bar_boundary_matches_the_prices_already_divided(self) -> None:
        split_run = _raw_split_run(_RAW_BARS, store=_store(_split(5)))
        adjusted_run = BacktestEngine(initial_capital=CAPITAL).run(
            _Ladder(buy_qty=20.0, sell_qty=10.0, sell_bar=5),
            _ADJUSTED_BARS,
        )

        # The hand-computed number, asserted before consulting the code.
        assert split_run.equity_curve[-1] == RAW_SPLIT_RUN_EQUITY
        assert adjusted_run.equity_curve[-1] == RAW_SPLIT_RUN_EQUITY
        assert split_run.equity_curve[-1] == adjusted_run.equity_curve[-1]
        assert split_run.equity_curve[-1] - CAPITAL == 500.0
        assert adjusted_run.equity_curve[-1] - CAPITAL == 500.0

    def test_the_end_of_run_position_is_the_same_position(self) -> None:
        split_run = _raw_split_run(_RAW_BARS, store=_store(_split(5)))
        adjusted_run = BacktestEngine(initial_capital=CAPITAL).run(
            _Ladder(buy_qty=20.0, sell_qty=10.0, sell_bar=5),
            _ADJUSTED_BARS,
        )

        # 20 post-split shares minus the 10 sold = 10 left, at a 500.00 basis in
        # both representations, having realised (520.00 - 500.00) x 10.
        assert _only_position(split_run) == {
            "instrument_id": "NSE:RELIANCE",
            "symbol": "RELIANCE",
            "quantity": 10.0,
            "avg_price": 500.0,
            "realized_pnl": 200.0,
        }
        assert _only_position(split_run) == _only_position(adjusted_run)

    def test_the_whole_equity_curve_is_the_same_series_of_numbers(self) -> None:
        split_run = _raw_split_run(_RAW_BARS, store=_store(_split(5)))
        adjusted_run = BacktestEngine(initial_capital=CAPITAL).run(
            _Ladder(buy_qty=20.0, sell_qty=10.0, sell_bar=5),
            _ADJUSTED_BARS,
        )
        assert split_run.equity_curve == adjusted_run.equity_curve

    def test_a_split_landing_between_two_bars_gives_the_same_answer(self) -> None:
        """The ex-date falls on a day the market was shut: no bar for it.

        The bars run 1,2,3,4,6,7,8 — the ex-date 2026-09-05 has no bar, so the
        action is applied at the first bar on or after it (day 6). Where the
        ex-date lands relative to the bar grid must not change the economics:
        the same 100500.00 as the on-a-bar-boundary run.
        """
        between_bars = (
            [_flat(1000.0, d) for d in (1, 2, 3, 4)]
            + [_flat(500.0, 6), _flat(520.0, 7), _flat(530.0, 8)]
        )
        result = _raw_split_run(between_bars, sell_bar=5, store=_store(_split(5)))

        assert result.equity_curve[-1] == RAW_SPLIT_RUN_EQUITY
        assert _only_position(result)["quantity"] == 10.0
        assert _only_position(result)["avg_price"] == 500.0
        assert _only_position(result)["realized_pnl"] == 200.0

    def test_the_control_an_unapplied_split_is_a_different_and_worse_number(self) -> None:
        """Proves the two assertions above are load-bearing.

        Drop the store and the split is not applied: the lot stays 10 @ 1000.00,
        the exit realises -4800.00 instead of +200.00, and the run reports
        95200.00 — the phantom fifty percent loss AC-19 names. The lot is also
        fully closed, so the run reports no open position, where the split run
        reports 10 @ 500.00.
        """
        result = _raw_split_run(_RAW_BARS)

        assert result.equity_curve[-1] == NOT_APPLIED_EQUITY
        assert result.equity_curve[-1] != RAW_SPLIT_RUN_EQUITY
        assert result.equity_curve[-1] - CAPITAL == -4800.0
        assert result.end_positions == []


# ---------------------------------------------------------------------------
# A split re-bases the OPEN BRACKET, not just the position
# ---------------------------------------------------------------------------
#
# 20 shares at a 500.00 basis is 10000.00 either side of the ex-date, so a
# value-neutral 2:1 split must leave equity at exactly the opening capital.
_RAW_BARS_THROUGH_DAY6 = (
    [_flat(1000.0, d) for d in (1, 2, 3, 4)] + [_flat(500.0, d) for d in (5, 6)]
)


class TestSplitRebasesTheOpenBracket:
    def test_a_pre_split_stop_does_not_sell_at_twice_the_market_price(self) -> None:
        result = BacktestEngine(
            initial_capital=CAPITAL, corporate_actions=_store(_split(5))
        ).run(
            _Ladder(
                buy_qty=10.0,
                sell_qty=None,
                sell_bar=99,
                metadata={"stop_loss_price": 950.0, "target_price": 1200.0},
            ),
            _RAW_BARS_THROUGH_DAY6,
        )

        # The bracket re-bases to stop 475.00 / target 600.00 / 20 shares. The
        # market's worst print is 500.00, which does not reach 475.00, so the
        # trade is still open and equity is exactly the opening capital.
        assert [f["side"] for f in result.fills] == ["BUY"]
        assert result.equity_curve[-1] == 100000.0
        assert _only_position(result)["quantity"] == 20.0
        assert _only_position(result)["avg_price"] == 500.0

    def test_the_re_based_stop_still_fires_when_the_market_reaches_it(self) -> None:
        """A stop that survives the re-base has to remain a live stop.

        Bar 6 dips to 474.00 — through the re-based stop of 475.00 (950.00 / 2),
        and nowhere near the pre-split level, so a bracket left in the old scale
        would sit 475.00 above the market and never fire. Exit at
        475.00 x (1 - 25/10000) = 473.8125, which beats the bar's own 474.00, so
        the slippage term binds: 90000 + (20 x 473.8125) = 99476.25.
        """
        bars = (
            [_flat(1000.0, d) for d in (1, 2, 3, 4)]
            + [_flat(500.0, 5), _dip(474.0, 6), _flat(474.0, 7)]
        )
        result = BacktestEngine(
            initial_capital=CAPITAL, corporate_actions=_store(_split(5))
        ).run(
            _Ladder(
                buy_qty=10.0,
                sell_qty=None,
                sell_bar=99,
                metadata={"stop_loss_price": 950.0, "target_price": 1200.0},
            ),
            bars,
        )
        assert [f["price"] for f in result.fills] == [1000.0, 473.8125]
        assert result.equity_curve[-1] == 99476.25
        assert result.end_positions == []

    def test_the_re_based_stop_covers_the_whole_re_based_position(self) -> None:
        """A stop re-based in price must be re-based in size too.

        Entry 10 @ 1000.00, 2:1 split, so the position is 20 @ 500.00. The
        declared stop of 900.00 re-bases to 450.00 and the leg size to 20. Bar 6
        dips to 440.00 and the exit sells **all twenty** shares at
        min(450.00 x (1 - 25/10000), 440.00) = 440.00: 90000 + (20 x 440.00) =
        98800.00.

        Sized off the pre-split fill the leg would have been 10 and would have
        sold 10 of the 20 — half the position left holding with no stop at all,
        which is the other half of the same defect.
        """
        bars = (
            [_flat(1000.0, d) for d in (1, 2, 3, 4)]
            + [_flat(500.0, 5), _dip(440.0, 6), _flat(440.0, 7)]
        )
        result = BacktestEngine(
            initial_capital=CAPITAL, corporate_actions=_store(_split(5))
        ).run(
            _Ladder(
                buy_qty=10.0,
                sell_qty=None,
                sell_bar=99,
                metadata={"stop_loss_price": 900.0, "target_price": 1200.0},
            ),
            bars,
        )
        assert [(f["side"], f["price"], f["qty"]) for f in result.fills] == [
            ("BUY", 1000.0, 10.0),
            ("SELL", 440.0, 20.0),
        ]
        assert result.equity_curve[-1] == 98800.0
        assert result.end_positions == []

    def test_the_order_still_carries_the_levels_the_strategy_declared(self) -> None:
        """The re-basing is the *simulation's* job, not a rewrite of the order.

        The entry fill must report the strategy's own pre-split 900.00/1200.00
        so an audit trail still shows what was declared; only the bracket the
        backtest simulates against the post-split market is re-scaled.
        """
        bars = (
            [_flat(1000.0, d) for d in (1, 2, 3, 4)]
            + [_flat(500.0, d) for d in (5, 6)]
        )
        result = BacktestEngine(
            initial_capital=CAPITAL, corporate_actions=_store(_split(5))
        ).run(
            _Ladder(
                buy_qty=10.0,
                sell_qty=None,
                sell_bar=99,
                metadata={"stop_loss_price": 900.0, "target_price": 1200.0},
            ),
            bars,
        )
        assert result.fills[0]["stop"] == 900.0
        assert result.fills[0]["target"] == 1200.0
        assert result.equity_curve[-1] == 100000.0


# ---------------------------------------------------------------------------
# An action is owed only by the lot that existed at the ex-date
# ---------------------------------------------------------------------------
#
# The market is already at 500.00 from day 3; the entry fills on day 4 at
# 500.00 and never held a pre-split share. A split whose ex-date was day 2 is
# not owed on that lot. Before this was gated, the run re-based 10 @ 500.00
# into 20 @ 250.00 and then sold half at 500.00, reporting 105000.00.
_LATE_ENTRY_BARS = (
    [_flat(1000.0, d) for d in (1, 2)] + [_flat(500.0, d) for d in (3, 4, 5, 6)]
)


class TestActionsArePointInTime:
    def test_an_action_whose_ex_date_predates_the_lot_is_not_applied(self) -> None:
        """The entry fills on day 4, two days after the day-2 ex-date.

        The market is already ex-split at 500.00 from day 3, so the lot never
        held a pre-split share and the 2:1 is owed to nobody. BUY 10 @ 500.00
        and SELL 10 @ 500.00 realise 0.00 and return the cash to the opening
        capital — 100000.00.

        Applied anyway it would re-base 10 @ 500.00 into 20 @ 250.00, sell half
        at 500.00 for a 2500.00 gain, and report 105000.00.
        """
        result = BacktestEngine(
            initial_capital=CAPITAL, corporate_actions=_store(_split(2))
        ).run(
            _Ladder(buy_qty=10.0, sell_qty=10.0, buy_bar=3, sell_bar=5),
            _LATE_ENTRY_BARS,
        )
        assert [f["price"] for f in result.fills] == [500.0, 500.0]
        assert result.equity_curve[-1] == 100000.0
        assert result.end_positions == []

    def test_the_control_applying_it_anyway_is_the_optimism_being_removed(self) -> None:
        """Same fixture with the ex-date moved *after* the entry.

        A day-4 ex-date with a day-4 entry fill means the lot did hold the
        shares, so the 2:1 is owed: 10 @ 500.00 becomes 20 @ 250.00 and the
        exit at 500.00 realises (500.00 - 250.00) x 10 = 2500.00, cash 95000.00
        with 10 @ 250.00 marked at 500.00 = 5000.00, so 105000.00. The gate
        distinguishes these two by the lot's open date and nothing else.
        """
        result = BacktestEngine(
            initial_capital=CAPITAL, corporate_actions=_store(_split(4))
        ).run(
            _Ladder(buy_qty=10.0, sell_qty=10.0, buy_bar=3, sell_bar=5),
            _LATE_ENTRY_BARS,
        )
        assert result.equity_curve[-1] == 105000.0
        assert _only_position(result) == {
            "instrument_id": "NSE:RELIANCE",
            "symbol": "RELIANCE",
            "quantity": 10.0,
            "avg_price": 250.0,
            "realized_pnl": 2500.0,
        }

    def test_the_lot_that_did_exist_at_the_ex_date_is_still_re_based(self) -> None:
        """The gate is on WHEN the lot opened, not on the run's start."""
        result = BacktestEngine(
            initial_capital=CAPITAL, corporate_actions=_store(_split(2))
        ).run(
            _Ladder(buy_qty=10.0, sell_qty=10.0, sell_bar=5),
            _RAW_BARS,
        )
        assert result.equity_curve[-1] == RAW_SPLIT_RUN_EQUITY
        assert _only_position(result)["avg_price"] == 500.0

    def test_a_lot_closed_before_the_ex_date_and_reopened_after_owes_nothing(
        self,
    ) -> None:
        """The gate follows the *current* lot, not the run's first position.

        The ladder buys 10 on bar 1 (fills bar 2 @ 1000.00), sells them on bar 2
        (fills bar 3 @ 1000.00, flat from bar 3 on), and buys again on bar 5
        (fills bar 6 at the post-split 520.00). With a day-4 ex-date the first
        lot is closed before it and the second never held a pre-split share, so
        the split is owed to neither:
          cash 100000 - 10000 + 10000 - 5200 = 94800.00, position 10 @ 520.00
          marked at day 7's close of 530.00 -> 5300.00, so 100100.00.
        Applied to the second lot anyway it would be 20 @ 260.00 and 105400.00.
        """
        result = BacktestEngine(
            initial_capital=CAPITAL, corporate_actions=_store(_split(4))
        ).run(
            _ReFlatLadder(), _RAW_BARS
        )
        assert [f["price"] for f in result.fills] == [1000.0, 1000.0, 520.0]
        assert result.equity_curve[-1] == 100100.0
        assert _only_position(result) == {
            "instrument_id": "NSE:RELIANCE",
            "symbol": "RELIANCE",
            "quantity": 10.0,
            "avg_price": 520.0,
            "realized_pnl": 0.0,
        }


class _ReFlatLadder:
    """BUY 10 on bar 1, SELL 10 on bar 2, BUY 10 on bar 5."""

    def __init__(self) -> None:
        self._signals: list[Signal] = []
        self._bar = 0

    @property
    def strategy_id(self) -> str:
        return "re-flat-ladder"

    @property
    def signals(self) -> list:
        return list(self._signals)

    def on_bar(self, context, candle):
        self._bar += 1
        plan = {1: OrderSide.BUY, 2: OrderSide.SELL, 5: OrderSide.BUY}
        if self._bar not in plan:
            return None
        signal = Signal(
            instrument=candle.instrument,
            direction=plan[self._bar],
            strength=1.0,
            quantity=Quantity(value=Decimal("10")),
            reason=f"bar{self._bar}",
            timestamp=candle.timestamp,
        )
        self._signals.append(signal)
        return signal

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        """No-op fill hook."""


# ---------------------------------------------------------------------------
# A dividend inside the window
# ---------------------------------------------------------------------------
#
# 10 @ 1000.00 held across a 10.00/share dividend:
#   cash 100000 - 10000 = 90000, + (10 x 10.00) = 90100
#   SELL 10 @ 1020.00 -> realised (1020.00 - 1000.00) x 10 + 100.00 = 2100.00
#   cash 90100 + 10200.00 = 100300.00, flat
_DIVIDEND_BARS = [_flat(1000.0, d) for d in (1, 2, 3, 4, 5)] + [
    _flat(1020.0, 6), _flat(1030.0, 7)
]


class TestDividend:
    def test_a_dividend_inside_the_window_is_credited(self) -> None:
        result = BacktestEngine(
            initial_capital=CAPITAL, corporate_actions=_store(_dividend(3, 10.0))
        ).run(
            _Ladder(buy_qty=10.0, sell_qty=10.0, sell_bar=5),
            _DIVIDEND_BARS,
        )
        assert result.equity_curve[-1] == 100300.0
        assert result.equity_curve[-1] - CAPITAL == 300.0
        assert result.end_positions == []

    def test_the_control_without_the_dividend_credits_nothing(self) -> None:
        result = BacktestEngine(initial_capital=CAPITAL).run(
            _Ladder(buy_qty=10.0, sell_qty=10.0, sell_bar=5),
            _DIVIDEND_BARS,
        )
        assert result.equity_curve[-1] == 100200.0
        assert result.equity_curve[-1] != 100300.0

    def test_a_split_and_a_dividend_on_one_ex_date_both_apply(self) -> None:
        """The once-per-run key is the action's identity, never the bare date.

        Both actions carry ex-date 2026-09-05, so a dedup keyed on the date
        would apply one and silently drop the other. The store holds the
        dividend first and the run applies them in that order, which is also the
        economically right one — a dividend is paid on the shares held at the
        record date, so it is credited on the 10 pre-split shares, and the
        split then doubles them. (Within one ex-date the order is the store's
        insertion order; see concerns.)

        dividend 10.00 x 10 pre-split shares = 100.00 credited, then the split
        re-bases 10 @ 1000.00 to 20 @ 500.00 with the basis unchanged, then
        SELL 10 @ 520.00 realises (520.00 - 500.00) x 10 = +200.00:
        cash 90000 + 100.00 + 5200.00 = 95300.00, and 10 @ 500.00 marked at
        530.00 = 5300.00, so 100600.00.
        """
        result = BacktestEngine(
            initial_capital=CAPITAL,
            corporate_actions=_store(_dividend(5, 10.0), _split(5)),
        ).run(
            _Ladder(buy_qty=10.0, sell_qty=10.0, sell_bar=5),
            _RAW_BARS,
        )
        assert result.equity_curve[-1] == 100600.0
        assert _only_position(result)["quantity"] == 10.0
        assert _only_position(result)["avg_price"] == 500.0
        # The dividend is realised P&L by the shared kernel (apply_dividend adds
        # it to realized_pnl), so the position's 300.00 is the 200.00 of
        # trading plus the 100.00 of dividend.
        assert _only_position(result)["realized_pnl"] == 300.0
