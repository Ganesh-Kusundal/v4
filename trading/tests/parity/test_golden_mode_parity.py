"""Golden mode-parity gate (P5).

The same market-data event stream must produce identical *signals* in every
mode, and every mode must fill at a real reference price (never zero /
nominal 1.0). This is the acceptance test for the parity review's critical
findings:

* CRITICAL-2 — strategy orders are stamped with a reference price + a
  deterministic correlation id (no price-less MARKET orders filling at
  zero / nominal 1.0).
* CRITICAL-3 / HIGH-7 — BacktestEngine matches signals to candles by
  timestamp and fills at the NEXT bar's open (no same-close look-ahead, no
  sequential misalignment, no final-close MTM); point-in-time MTM.
* Mode parity — paper/reactive and BacktestEngine agree on signal count and
  signal timestamps.

Price-reference model (unified): every mode fills a signal at the *next*
bar's open of the same instrument — BacktestEngine by timestamp matching,
the reactive bridge by deferring orders until the next candle. The same
event stream therefore fills at identical prices in backtest, replay,
paper, and live, and this gate asserts **exact** fill-price + P&L equality
across modes (parity review CRITICAL-1).

A 23-bar stream is intentionally identical to the review's empirical probe:
20 flat closes then a rising leg — SMA5 crosses SMA20 on bar 21.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import (
    OHLC,
    Candle,
    OrderRequest,
    OrderSide,
    OrderType,
    Signal,
    Timeframe,
    TimeInForce,
)
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import (
    PaperFillSource,
    SimulatedFillSource,
)
from tradex_trading.execution.slippage import FixedSlippageModel
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.replay.backtest import BacktestEngine
from tradex_trading.sdk.session import TradingSession
from tradex_trading.strategy import ReactiveStrategyEngine
from tradex_trading.strategy.extensions.strategies.sma_cross import SmaCrossStrategy

INSTRUMENT = Equity.of("NSE", "RELIANCE")
CLOSES = [10.0] * 20 + [11.0, 12.0, 13.0]


def _candle(close: float, day: int) -> Candle:
    price = Price(value=Decimal(str(close)))
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=price, high=price, low=price, close=price),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 8, day, tzinfo=UTC),
    )


def _candles() -> list[Candle]:
    return [_candle(close, day) for day, close in enumerate(CLOSES, start=1)]


class TestGoldenBacktestAccounting:
    """BacktestEngine accounting: timestamp-matched, next-bar-open, PIT-MTM."""

    def test_signal_fills_at_next_bar_open_not_signal_close(self) -> None:
        """The SMA cross fires on bar 21 (close 11.0). The fill must be the
        OPEN of bar 22 (12.0) — the earliest tradeable price — not bar 21's
        close (11.0) and never the final close (13.0)."""
        strategy = SmaCrossStrategy("golden-bt", INSTRUMENT)
        result = BacktestEngine().run(strategy, _candles())

        assert result.num_trades == 1
        assert result.trades[0].direction == OrderSide.BUY
        # Signal stamped bar-21 timestamp; fill = bar-22 open = 12.0.
        assert result.trades[0].timestamp == datetime(2026, 8, 21, tzinfo=UTC)
        # Fill point: buy 1 @12 -> cash 99988 + MTM(closes <= bar 22 = 12.0)
        # = 100000. No look-ahead to 13.0 at the fill.
        assert result.equity_curve[-2] == 100000.0
        # Curve is closed at the final close: +1 position @13 -> 100001.
        assert result.equity_curve[-1] == 100001.0

    def test_no_final_close_lookahead_in_mtm(self) -> None:
        """Mark-to-market at a fill must never use a close from the future.
        The fill point (index -2) uses closes <= bar 22 only; if the final
        close were used at the fill, it would read 100001 instead of 100000.
        """
        strategy = SmaCrossStrategy("golden-mtm", INSTRUMENT)
        result = BacktestEngine().run(strategy, _candles())
        assert result.equity_curve[-2] == 100000.0
        # The final point legitimately marks the open position at 13.0.
        assert result.equity_curve[-1] == 100001.0


class TestGoldenReactiveParity:
    """Reactive paper/backtest paths agree with BacktestEngine's accounting
    and carry non-zero reference prices on every strategy order."""

    def test_paper_path_fills_at_reference_price_not_nominal(self) -> None:
        session = TradingSession.paper()
        strategy = SmaCrossStrategy("golden-paper", INSTRUMENT)
        strategy_engine = ReactiveStrategyEngine(session.bus)
        strategy_engine.register(strategy)
        fills = []
        sub = session.stream.subscribe_fills(fills.append)
        try:
            for candle in _candles():
                session.bus.publish(candle)
            assert len(fills) == 1
            fill = fills[0].fill
            assert fill.side == OrderSide.BUY
            # Unified next-open model: the bar-21 signal fills at bar 22's
            # OPEN = 12.0 — never the signal close (11.0), final close (13.0),
            # or a nominal 1.0.
            assert fill.price.value == Decimal("12")
            # The order request carries a deterministic correlation id.
            order = session.engine.cache.all_orders()[0]
            assert order.correlation_id is not None
            assert order.correlation_id.value.startswith("strat-")
            # PositionManager recorded the fill at the fill price.
            pos = session.portfolio.positions()[0]
            assert pos.quantity.value == 1
            assert pos.avg_price.value == fill.price.value
        finally:
            strategy_engine.dispose_all()
            sub.cancel()
            session.stop()

    def test_backtest_boot_path_fills_at_reference_price_not_zero(self) -> None:
        """The boot-composed backtest engine (SimulatedFillSource) fills the
        bridge-stamped reference price, not zero. Uses a clean composition
        (own bus + engine) so the module-level discovered singletons that
        ``boot()`` auto-registers are never polluted by this test's candles.
        """
        from tradex_domain.events import OrderFilled

        bus = ReactiveBus()
        strategy = SmaCrossStrategy("golden-boot", INSTRUMENT)
        strategy_engine = ReactiveStrategyEngine(bus)
        strategy_engine.register(strategy)
        engine = ExecutionEngine(bus, SimulatedFillSource())
        fills = []
        bus.of_type(OrderFilled).subscribe(fills.append)
        try:
            for candle in _candles():
                bus.publish(candle)
            assert len(fills) == 1
            # Same unified next-open model: bar-22 open = 12.0.
            assert fills[0].fill.price.value == Decimal("12")
        finally:
            strategy_engine.dispose_all()
            engine.shutdown()

    def test_signal_count_matches_across_modes(self) -> None:
        """The same strategy + events emits the same number of signals in the
        paper path and in BacktestEngine.run — the parity discriminator."""
        bt_strategy = SmaCrossStrategy("golden-count-bt", INSTRUMENT)
        result = BacktestEngine().run(bt_strategy, _candles())

        session = TradingSession.paper()
        paper_strategy = SmaCrossStrategy("golden-count-paper", INSTRUMENT)
        strategy_engine = ReactiveStrategyEngine(session.bus)
        strategy_engine.register(paper_strategy)
        try:
            for candle in _candles():
                session.bus.publish(candle)
            assert result.num_trades == 1
            assert len(paper_strategy.signals) == 1
            # Both signal timestamps align to bar 21.
            assert result.trades[0].timestamp == paper_strategy.signals[0].timestamp
        finally:
            strategy_engine.dispose_all()
            session.stop()


class _TimedBuySell:
    """Emits BUY on bar 1 and SELL on bar 2, both timestamped — used to prove
    accounting convergence (CRITICAL-1)."""

    def __init__(self, instrument) -> None:
        self._instrument = instrument
        self._signals: list = []
        self._bar = 0

    @property
    def strategy_id(self) -> str:
        return "timed-buy-sell"

    @property
    def signals(self) -> list:
        return list(self._signals)

    def on_bar(self, context, candle) -> object:
        self._bar += 1
        if self._bar == 1:
            signal = Signal(
                instrument=candle.instrument, direction=OrderSide.BUY,
                strength=1.0, reason="buy", timestamp=candle.timestamp,
            )
        elif self._bar == 2:
            signal = Signal(
                instrument=candle.instrument, direction=OrderSide.SELL,
                strength=1.0, reason="sell", timestamp=candle.timestamp,
            )
        else:
            return None
        self._signals.append(signal)
        return signal

    def on_quote(self, context, quote) -> None:
        return None

    def on_depth(self, context, depth) -> None:
        return None

    def on_fill(self, context, fill) -> None:
        pass


class _TimedPartial:
    """Multi-fill buy/sell ladder: BUY 2, BUY 1, SELL 1, SELL 2 — partial
    closes that exercise the weighted-average + realized-PnL math."""

    def __init__(self, instrument) -> None:
        self._instrument = instrument
        self._signals: list = []
        self._bar = 0

    @property
    def strategy_id(self) -> str:
        return "timed-partial"

    @property
    def signals(self) -> list:
        return list(self._signals)

    def on_bar(self, context, candle) -> object:
        self._bar += 1
        plan = {
            1: (OrderSide.BUY, 2.0),
            2: (OrderSide.BUY, 1.0),
            3: (OrderSide.SELL, 1.0),
            4: (OrderSide.SELL, 2.0),
        }.get(self._bar)
        if plan is None:
            return None
        side, strength = plan
        signal = Signal(
            instrument=candle.instrument, direction=side,
            strength=strength, reason=f"bar{self._bar}",
            timestamp=candle.timestamp,
        )
        self._signals.append(signal)
        return signal

    def on_quote(self, context, quote) -> None:
        return None

    def on_depth(self, context, depth) -> None:
        return None

    def on_fill(self, context, fill) -> None:
        pass

    def reset(self) -> None:
        """Reset internal state for reuse across multiple runs."""
        self._bar = 0
        self._signals.clear()


class TestAccountingConvergence:
    """CRITICAL-1: both execution paths compute identical P&L on the same
    fills. BacktestEngine and the reactive pipeline now share one accounting
    model (``execution.position_math.apply_fill``) AND one price reference
    (next bar's open), so realized P&L — and the fill prices themselves —
    must match exactly across backtest, replay, and paper modes.

    The ladder series is fully closed: buys and sells shift by the same
    step, so realized P&L is identical in both engines whatever the prices,
    and the unified next-open reference makes the fill prices identical too.
    """

    @staticmethod
    def _chained_candle(price: Decimal, day: int) -> Candle:
        p = Price(value=price)
        return Candle(
            instrument=INSTRUMENT,
            timeframe=Timeframe.D1,
            ohlc=OHLC(open=p, high=p, low=p, close=p),
            volume=Quantity(value=Decimal("1000")),
            timestamp=datetime(2026, 9, day, tzinfo=UTC),
        )

    def test_backtest_pnl_equals_position_manager_pnl(self) -> None:
        from tradex_domain.events import OrderFilled

        from tradex_trading.strategy import ReactiveStrategyEngine

        candles = [
            self._chained_candle(Decimal("100"), 1),
            self._chained_candle(Decimal("110"), 2),
            self._chained_candle(Decimal("120"), 3),
        ]

        # --- Path A: BacktestEngine (fills at next-bar open: 110 then 120) ---
        bt = BacktestEngine().run(_TimedBuySell(INSTRUMENT), list(candles))
        assert bt.num_trades == 2
        # BUY@110 SELL@120 -> +10; equity closes at 100010.
        assert bt.equity_curve[-1] == 100010.0

        # --- Path B: reactive path — unified next-open model fills at the
        # SAME prices as BacktestEngine (bar-2 open 110, bar-3 open 120). ---
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource())
        strategy_engine = ReactiveStrategyEngine(bus)
        fills: list = []
        bus.of_type(OrderFilled).subscribe(fills.append)
        strategy_engine.register(_TimedBuySell(INSTRUMENT))
        try:
            for c in candles:
                bus.publish(c)
            assert [f.fill.price.value for f in fills] == [
                Decimal("110"), Decimal("120"),
            ]
            position = engine.cache.all_positions()[0]
            # Realized P&L from the shared PositionManager accounting: +10.
            assert position.realized_pnl.amount == Decimal("10")
        finally:
            strategy_engine.dispose_all()
            engine.shutdown()

        # Convergence: backtest equity gain == PositionManager realized P&L.
        assert bt.equity_curve[-1] - 100000.0 == float(position.realized_pnl.amount)

    def test_exact_fill_price_equals_backtest_next_open(self) -> None:
        """Unified price reference: paper and backtest fill the bar-21 signal
        at bar 22's OPEN (12.0) — identical prices, not just identical P&L."""
        session = TradingSession.paper()
        strategy = SmaCrossStrategy("golden-exact", INSTRUMENT)
        strategy_engine = ReactiveStrategyEngine(session.bus)
        strategy_engine.register(strategy)
        fills = []
        sub = session.stream.subscribe_fills(fills.append)
        try:
            for candle in _candles():
                session.bus.publish(candle)
            assert len(fills) == 1
            assert fills[0].fill.price.value == Decimal("12")
            assert session.portfolio.positions()[0].avg_price.value == Decimal("12")
        finally:
            strategy_engine.dispose_all()
            sub.cancel()
            session.stop()

        bt_strategy = SmaCrossStrategy("golden-exact-bt", INSTRUMENT)
        bt = BacktestEngine().run(bt_strategy, _candles())
        assert bt.num_trades == 1
        # Backtest: buy 1 @ 12.0 → cash 99988 + PIT MTM 12 = 100000; final 100001.
        assert bt.equity_curve[-2] == 100000.0
        assert bt.equity_curve[-1] == 100001.0

    @staticmethod
    def _ladder_candles() -> list:
        return [
            TestAccountingConvergence._chained_candle(Decimal(str(p)), day)
            for day, p in enumerate([100, 110, 120, 130, 140], start=1)
        ]

    def test_partial_sell_realized_pnl_matches_across_modes(self) -> None:
        """A BUY 2 / BUY 1 / SELL 1 / SELL 2 ladder books identical realized
        P&L in BacktestEngine (fills 110/120/130/140) and the reactive paper
        session (fills 100/110/120/130): both close fully at +70."""
        from tradex_trading.strategy import ReactiveStrategyEngine

        candles = self._ladder_candles()

        # --- Path A: BacktestEngine ---
        bt = BacktestEngine().run(_TimedPartial(INSTRUMENT), list(candles))
        assert bt.num_trades == 4
        # BUY 2@110 + 1@120 = 340 spent; SELL 1@130 + 2@140 = 410 received.
        assert bt.equity_curve[-1] == 100070.0

        # --- Path B: reactive paper session (end-to-end boot composition) ---
        session = TradingSession.paper()
        strategy_engine = ReactiveStrategyEngine(session.bus)
        strategy_engine.register(_TimedPartial(INSTRUMENT))
        fills: list = []
        sub = session.stream.subscribe_fills(fills.append)
        try:
            for c in candles:
                session.bus.publish(c)
            # Unified next-open model: reactive fills land at the SAME prices
            # as BacktestEngine — 110/120/130/140 — not signal closes.
            assert [f.fill.price.value for f in fills] == [
                Decimal("110"), Decimal("120"), Decimal("130"), Decimal("140"),
            ]
            closed = next(
                p for p in session.portfolio.positions() if p.quantity.value == 0
            )
            assert closed.realized_pnl.amount == Decimal("70")
        finally:
            strategy_engine.dispose_all()
            sub.cancel()
            session.stop()

        # Convergence: identical realized P&L across both accounting engines.
        assert bt.equity_curve[-1] - 100000.0 == float(closed.realized_pnl.amount)


class TestFillSourceParity:
    """HIGH-6b: every price-resolving fill source shares ONE price-resolution
    path (``FillModel``), so the same request + market reference resolves to
    the identical fill price in backtest, replay, paper, and live."""

    @staticmethod
    def _request() -> OrderRequest:
        return OrderRequest(
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
            time_in_force=TimeInForce.DAY,
            reference_timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        )

    def test_all_fill_sources_agree_on_same_input(self) -> None:
        """The same request + LTP resolves the identical fill price in
        Simulated (backtest) and Paper — the shared FillModel must return one
        price for every mode (LTP wins over the limit price)."""
        req = self._request()
        ltp = Price(value=Decimal("101"))
        simulated = SimulatedFillSource().resolve_fill_price(req, market_price=ltp)
        paper = PaperFillSource().resolve_fill_price(req, market_price=ltp)
        assert simulated == paper == Price(value=Decimal("101"))

    def test_fill_source_parity_with_slippage(self) -> None:
        """The same slippage model moves the fill price identically in every
        mode — identical net P&L across backtest/paper (HIGH-6b)."""
        slippage = FixedSlippageModel(constant=Decimal("0.25"))
        req = self._request()
        ltp = Price(value=Decimal("100"))
        simulated = SimulatedFillSource(slippage_model=slippage).resolve_fill_price(
            req, market_price=ltp
        )
        paper = PaperFillSource(slippage_model=slippage).resolve_fill_price(
            req, market_price=ltp
        )
        assert simulated == paper
        # 100 + 0.25 — both modes applied the identical adjustment.
        assert simulated.value == Decimal("100.25")
