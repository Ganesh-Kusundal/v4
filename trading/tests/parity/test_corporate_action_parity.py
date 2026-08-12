"""Corporate-action parity gate (review area #4 — "corporate actions ... modeled
consistently").

A mid-holding SPLIT/BONUS re-bases quantity and average price, and a DIVIDEND
credits cash + realized P&L. Both BacktestEngine (point-in-time, at the first
fill bar on/after the ex-date) and the reactive PositionManager must apply the
same action through the *same* shared math (``position_math.apply_split`` /
``apply_dividend``), so a stock split mid-backtest cannot silently corrupt
P&L and live cannot diverge from backtest.

The ladder strategy below BUYs on bar 1 and SELLs on bar 5; fills land at the
next bar's open (bar 2 and bar 6) under the unified next-open model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import OHLC, Candle, OrderSide, Signal, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.datalake.corporate_actions import CorporateAction, CorporateActionStore
from tradex_trading.execution.position_manager import PositionManager
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.replay.backtest import BacktestEngine

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _candle(price: float, day: int) -> Candle:
    p = Price(value=Decimal(str(price)))
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=p, high=p, low=p, close=p),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 9, day, tzinfo=UTC),
    )


def _split_ladder(post_split: float = 60.0) -> list[Candle]:
    """BUY bar1 (fills bar2 @100), SELL bar5 (fills bar6 @*post_split*)."""
    return [
        _candle(100.0, 1), _candle(100.0, 2), _candle(100.0, 3),
        _candle(100.0, 4), _candle(100.0, 5), _candle(post_split, 6),
        _candle(post_split, 7), _candle(post_split, 8),
    ]


class _SplitLadderStrategy:
    """BUY strength=100 on bar 1, SELL strength=``sell_strength`` on bar 5.

    ``sell_strength=100`` leaves a stub position after the split (unrealized
    MTM at the final close); ``sell_strength=200`` fully closes the post-split
    position so backtest realized P&L equals the equity gain exactly.
    """

    def __init__(self, instrument, sell_strength: float = 100.0) -> None:
        self._instrument = instrument
        self._sell_strength = sell_strength
        self._signals: list = []
        self._bar = 0

    @property
    def strategy_id(self) -> str:
        return "split-ladder"

    @property
    def signals(self) -> list:
        return list(self._signals)

    def _emit(self, direction: OrderSide, bar: int, candle, strength: float) -> object:
        signal = Signal(
            instrument=candle.instrument, direction=direction,
            strength=strength, reason=f"bar{bar}", timestamp=candle.timestamp,
        )
        self._signals.append(signal)
        return signal

    def on_bar(self, context, candle) -> object:
        self._bar += 1
        if self._bar == 1:
            return self._emit(OrderSide.BUY, 1, candle, 100.0)
        if self._bar == 5:
            return self._emit(OrderSide.SELL, 5, candle, self._sell_strength)
        return None

    def on_quote(self, context, quote) -> None:
        return None

    def on_depth(self, context, depth) -> None:
        return None

    def on_fill(self, context, fill) -> None:
        pass


def _store(*actions: CorporateAction) -> CorporateActionStore:
    store = CorporateActionStore()
    for action in actions:
        store.add_typed(action)
    return store


def _split_action(ex_day: int, ratio: float = 2.0) -> CorporateAction:
    return CorporateAction(
        instrument="RELIANCE", action_type="SPLIT",
        ex_date=f"2026-09-{ex_day:02d}", ratio=ratio,
    )


def _dividend_action(ex_day: int, per_share: float = 2.0) -> CorporateAction:
    return CorporateAction(
        instrument="RELIANCE", action_type="DIVIDEND",
        ex_date=f"2026-09-{ex_day:02d}", amount=per_share,
    )


class TestSplitParity:
    def test_backtest_split_rebases_quantity_and_avg(self) -> None:
        """BUY 100@100, 2:1 split before the SELL, SELL 100@60 post-split.

        Without split handling the SELL books -4000 ((60-100)*100). With the
        split the position is 200@50, so the SELL realizes +1000 and the
        remaining 100 shares mark at 60 (unrealized +1000) — equity 102000.
        """
        result = BacktestEngine(
            corporate_actions=_store(_split_action(ex_day=4))
        ).run(_SplitLadderStrategy(INSTRUMENT), _split_ladder())

        assert result.num_trades == 2
        # Gain: +1000 realized on the SELL + 100 @ (60-50) = 1000 unrealized.
        assert result.equity_curve[-1] == 102000.0

    def test_backtest_split_off_returns_naive_accounting(self) -> None:
        """No store configured → historical behavior (split not applied)."""
        result = BacktestEngine().run(_SplitLadderStrategy(INSTRUMENT), _split_ladder())
        # BUY 100@100, SELL 100@60 → -4000 realized; stub gone, no MTM.
        assert result.equity_curve[-1] == 96000.0

    def test_reactive_split_matches_backtest_realized_pnl(self) -> None:
        """With a fully-closing SELL, PositionManager's realized P&L equals
        the backtest's equity gain exactly — both through the same split math."""
        cache = TradingCache()
        pm = PositionManager(cache)
        pm.on_fill(_fill(OrderSide.BUY, Decimal("100"), Decimal("100")))
        pm.on_corporate_action(INSTRUMENT, "SPLIT", ratio=2.0)
        pos = pm.on_fill(_fill(OrderSide.SELL, Decimal("200"), Decimal("60")))
        assert pos.realized_pnl.amount == Decimal("2000")

        result = BacktestEngine(
            corporate_actions=_store(_split_action(ex_day=4))
        ).run(_SplitLadderStrategy(INSTRUMENT, sell_strength=200.0), _split_ladder())
        # BUY 100@100 → 200@50 → SELL 200@60 → +2000, fully closed.
        assert result.equity_curve[-1] == 102000.0
        assert result.equity_curve[-1] - 100000.0 == float(pos.realized_pnl.amount)

    def test_bonus_uses_same_path(self) -> None:
        """BONUS 1:1 is a 2.0 ratio — same re-base math."""
        result = BacktestEngine(
            corporate_actions=_store(
                CorporateAction(
                    instrument="RELIANCE", action_type="BONUS",
                    ex_date="2026-09-04", ratio=2.0,
                )
            )
        ).run(_SplitLadderStrategy(INSTRUMENT), _split_ladder())
        assert result.equity_curve[-1] == 102000.0


class TestDividendParity:
    def test_backtest_dividend_credits_cash_and_pnl(self) -> None:
        """BUY 100@100, 2.0/share dividend while holding, SELL 100@110.

        Cash: -10000 buy + 200 dividend + 11000 sell → 101200. Realized via
        the shared math: (110-100)*100 + 2*100 = 1200.
        """
        candles = _split_ladder(post_split=110.0)
        result = BacktestEngine(
            corporate_actions=_store(_dividend_action(ex_day=3))
        ).run(_SplitLadderStrategy(INSTRUMENT), candles)

        assert result.equity_curve[-1] == 101200.0

    def test_reactive_dividend_matches_backtest(self) -> None:
        cache = TradingCache()
        pm = PositionManager(cache)
        pm.on_fill(_fill(OrderSide.BUY, Decimal("100"), Decimal("100")))
        pm.on_corporate_action(INSTRUMENT, "DIVIDEND", per_share=2.0)
        pos = pm.on_fill(_fill(OrderSide.SELL, Decimal("100"), Decimal("110")))
        assert pos.realized_pnl.amount == Decimal("1200")

        result = BacktestEngine(
            corporate_actions=_store(_dividend_action(ex_day=3))
        ).run(_SplitLadderStrategy(INSTRUMENT), _split_ladder(post_split=110.0))
        assert result.equity_curve[-1] - 100000.0 == float(pos.realized_pnl.amount)

    def test_split_then_dividend_apply_in_ex_date_order(self) -> None:
        """Dividend on day 3 (pre-split qty), split on day 4: dividend credits
        100 shares, then the position becomes 200@50."""
        store = _store(
            _dividend_action(ex_day=3, per_share=2.0),
            _split_action(ex_day=4),
        )
        result = BacktestEngine(corporate_actions=store).run(
            _SplitLadderStrategy(INSTRUMENT), _split_ladder()
        )
        # Cash: -10000 buy + 200 dividend + 6000 sell = 96200; MTM 100@60=6000.
        assert result.equity_curve[-1] == 102200.0

    def test_distinct_actions_share_one_ex_date_both_apply(self) -> None:
        """A dividend AND a split on the SAME ex-date must both apply — the
        once-per-run dedup key is action identity, never the bare date."""
        store = _store(
            _dividend_action(ex_day=4, per_share=2.0),
            _split_action(ex_day=4, ratio=2.0),
        )
        result = BacktestEngine(corporate_actions=store).run(
            _SplitLadderStrategy(INSTRUMENT), _split_ladder()
        )
        # Cash: -10000 buy + 200 dividend + 6000 sell = 96200; MTM 100@60=6000.
        assert result.equity_curve[-1] == 102200.0

    def test_dividend_on_short_debits_realized_pnl(self) -> None:
        from tradex_domain.execution import Position
        from tradex_domain.value_objects import Money

        pos = Position(
            instrument=INSTRUMENT,
            quantity=Quantity(value=Decimal("-100")),
            avg_price=Price(value=Decimal("50")),
            realized_pnl=Money(amount=Decimal("0")),
            unrealized_pnl=Money(amount=Decimal("0")),
        )
        cache = TradingCache()
        cache.update_position(pos)
        pm = PositionManager(cache)
        out = pm.on_corporate_action(INSTRUMENT, "DIVIDEND", per_share=2.0)
        assert out is not None
        assert out.realized_pnl.amount == Decimal("-200")


def _fill(side: OrderSide, qty: str, price: str):
    from tradex_domain.execution import Fill
    from tradex_domain.value_objects import OrderId

    return Fill(
        order_id=OrderId("t-1"),
        instrument=INSTRUMENT,
        side=side,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
    )
