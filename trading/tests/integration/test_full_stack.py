"""End-to-end integration tests for the full v4 trading stack.

Tests the complete flow: session → market data → orders → positions → strategy → P&L.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain import (
    OHLC,
    Candle,
    Equity,
    OrderRequest,
    OrderSide,
    OrderType,
    Quote,
    StrategyContext,
    Timeframe,
)
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.replay.backtest import BacktestEngine
from tradex_trading.sdk.session import SessionState, TradingSession
from tradex_trading.strategy.core.buy_and_hold import BuyAndHoldStrategy


def _now() -> datetime:
    return datetime(2026, 7, 31, 10, 30, tzinfo=UTC)


def _eq(symbol: str = "RELIANCE") -> Equity:
    return Equity.of("NSE", symbol)


def _candle(instrument: Equity, price: float, ts: datetime) -> Candle:
    return Candle(
        instrument=instrument,
        timeframe=Timeframe.D1,
        timestamp=ts,
        ohlc=OHLC(
            open=Price(value=Decimal(str(price))),
            high=Price(value=Decimal(str(price * 1.01))),
            low=Price(value=Decimal(str(price * 0.99))),
            close=Price(value=Decimal(str(price))),
        ),
        volume=Quantity(value=Decimal("1000")),
    )


# ---------------------------------------------------------------------------
# Test 1: Full session lifecycle
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    """Test complete session lifecycle: create → start → services → stop."""

    def test_paper_session_lifecycle(self):
        """Create paper session, verify lifecycle states, stop."""
        session = TradingSession.paper()
        # Factories return READY so properties are usable immediately.
        assert session.state == SessionState.READY

        # Access core properties
        assert session.broker is not None
        assert session.engine is not None
        assert session.bus is not None

        session.stop()
        assert session.state == SessionState.STOPPED

    def test_context_manager(self):
        """Session works as context manager."""
        with TradingSession.paper() as session:
            session.start()
            assert session.state == SessionState.READY
        # After exit, session should be stopped (close() calls stop())
        assert session.state == SessionState.STOPPED


# ---------------------------------------------------------------------------
# Test 2: Market data flow
# ---------------------------------------------------------------------------


class TestMarketDataFlow:
    """Test market data: quote → ltp → history → search."""

    def test_quote_and_ltp(self):
        """Get quote and LTP from paper session."""
        session = TradingSession.paper()
        session.start()
        try:
            reliance = _eq()
            quote = session.broker.get_quote(reliance)
            # Paper broker returns a default synthetic quote
            assert quote is not None
            assert quote.ltp is not None
            assert quote.ltp.value > Decimal("0")

            # LTP should also work
            ltp = session.broker.ltp(reliance)
            assert ltp is not None
            assert ltp.value > Decimal("0")
        finally:
            session.stop()

    def test_history(self):
        """Get historical data."""
        session = TradingSession.paper()
        session.start()
        try:
            reliance = _eq()
            now = _now()
            history = session.broker.history(
                reliance, Timeframe.D1, now - timedelta(days=30), now
            )
            # Paper broker returns empty but valid HistoricalSeries
            assert history is not None
            assert hasattr(history, "candles")
        finally:
            session.stop()

    def test_search(self):
        """Search instruments."""
        session = TradingSession.paper()
        session.start()
        try:
            # Paper broker returns empty list for unknown query
            results = session.broker.search("REL")
            assert isinstance(results, list)
        finally:
            session.stop()


# ---------------------------------------------------------------------------
# Test 3: Order lifecycle
# ---------------------------------------------------------------------------


class TestOrderLifecycle:
    """Test order submission → fill → position."""

    def test_submit_and_check(self):
        """Submit an order and verify receipt."""
        session = TradingSession.paper()
        session.start()
        try:
            reliance = _eq()
            request = OrderRequest(
                instrument=reliance,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Quantity(value=Decimal("10")),
            )
            receipt = session.engine.submit(request)
            assert receipt is not None
            assert receipt.order_id is not None
        finally:
            session.stop()

    def test_positions_after_order(self):
        """Check positions after submitting an order."""
        session = TradingSession.paper()
        session.start()
        try:
            reliance = _eq()
            request = OrderRequest(
                instrument=reliance,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Quantity(value=Decimal("10")),
            )
            session.engine.submit(request)
            positions = session.engine.cache.all_positions()
            assert isinstance(positions, list)
        finally:
            session.stop()

    def test_account_info(self):
        """Get account info from paper session."""
        session = TradingSession.paper()
        session.start()
        try:
            account = session.broker.get_account()
            assert account is not None
            assert account.balance is not None
            assert account.balance.amount > Decimal("0")
        finally:
            session.stop()


# ---------------------------------------------------------------------------
# Test 4: Strategy execution
# ---------------------------------------------------------------------------


class TestStrategyExecution:
    """Test strategy with context injection and signal generation."""

    def test_buy_and_hold_strategy(self):
        """BuyAndHoldStrategy generates signal on quote."""
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        ctx = StrategyContext()
        quote = Quote(
            instrument=_eq(),
            ltp=Price(value=Decimal("2500")),
            timestamp=_now(),
        )
        signal = strategy.on_quote(ctx, quote)
        assert signal is not None
        assert signal.direction == OrderSide.BUY
        assert len(strategy.signals) == 1

    def test_buy_and_hold_on_bar_returns_none(self):
        """BuyAndHoldStrategy.on_bar returns None (no action on bars)."""
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        ctx = StrategyContext()
        candle = _candle(_eq(), 100.0, _now())
        result = strategy.on_bar(ctx, candle)
        assert result is None
        assert len(strategy.signals) == 0

    def test_strategy_context_injection(self):
        """Strategy receives populated context via ReactiveStrategyEngine."""
        from tradex_trading.strategy.core.engine import ReactiveStrategyEngine

        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)

        seen_contexts: list[StrategyContext] = []

        class RecordingStrategy:
            strategy_id = "recorder-1"

            def on_bar(self, ctx, candle):
                seen_contexts.append(ctx)

            def on_quote(self, ctx, quote):
                seen_contexts.append(ctx)

            def on_fill(self, ctx, fill):
                pass

        strat = RecordingStrategy()
        engine.register(strat)

        candle = _candle(_eq(), 100.0, _now())
        bus.publish(candle)

        assert len(seen_contexts) == 1
        assert seen_contexts[0].bar_count == 1

        engine.dispose_all()


# ---------------------------------------------------------------------------
# Test 5: Backtest engine
# ---------------------------------------------------------------------------


class TestBacktestEngine:
    """Test backtest with real data."""

    def test_backtest_with_candles(self):
        """Run backtest with candle data."""
        reliance = _eq()
        candles = [
            _candle(
                reliance,
                100.0 + i * 0.5,
                datetime(2026, 7, 31, 10, 30 + i, tzinfo=UTC),
            )
            for i in range(10)
        ]

        strategy = BuyAndHoldStrategy("bh-1", reliance)
        engine = BacktestEngine()
        result = engine.run(strategy, candles)
        assert result is not None
        assert hasattr(result, "total_return")
        assert hasattr(result, "sharpe")
        assert hasattr(result, "max_drawdown")
        assert hasattr(result, "num_trades")

    def test_backtest_empty_data(self):
        """Backtest with no data returns zeroed metrics."""
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        engine = BacktestEngine()
        result = engine.run(strategy, [])
        assert result.num_trades == 0
        assert result.total_return == 0.0


# ---------------------------------------------------------------------------
# Test 7: Strategy ensemble
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test 8: Full pipeline — session → strategy → backtest → analytics
# ---------------------------------------------------------------------------


class TestFullPipeline:
    """End-to-end: create session, run backtest, check analytics."""

    def test_full_pipeline(self):
        """Complete flow from session to analytics."""
        # 1. Create session
        session = TradingSession.paper()
        session.start()

        try:
            # 2. Prepare data
            reliance = _eq()
            candles = [
                _candle(
                    reliance,
                    100.0 + i,
                    datetime(2026, 7, 31, 10, 30 + i, tzinfo=UTC),
                )
                for i in range(20)
            ]

            # 3. Run backtest
            strategy = BuyAndHoldStrategy("bh-1", reliance)
            engine = BacktestEngine()
            result = engine.run(strategy, candles)
            assert result is not None

            # 4. Check analytics
            from tradex_trading.analytics.engine import AnalyticsEngine

            analytics = AnalyticsEngine()
            # Use trade returns
            returns = [0.01, -0.005, 0.02, 0.01, -0.01]
            sheet = analytics.tearsheet(returns)
            assert "total_return" in sheet
            assert "sharpe_ratio" in sheet
            assert sheet["num_trades"] == 5

            summary = analytics.summary(returns)
            assert "PERFORMANCE SUMMARY" in summary
        finally:
            session.stop()

    def test_optimization_pipeline(self):
        """Grid search over strategy parameters."""
        from tradex_trading.replay.optimization import grid_search

        reliance = _eq()
        candles = [
            _candle(
                reliance,
                100.0 + i * 0.5,
                datetime(2026, 7, 31, 10, 30 + i, tzinfo=UTC),
            )
            for i in range(20)
        ]

        def run_backtest(params):
            strategy = BuyAndHoldStrategy(f"bh-{params.get('id', 0)}", reliance)
            engine = BacktestEngine()
            return engine.run(strategy, candles)

        result = grid_search(
            {"id": [1, 2, 3]},
            run_backtest,
        )
        assert result.total_combinations == 3
        assert result.best is not None


# ---------------------------------------------------------------------------
# Test 9: Reactive bus integration
# ---------------------------------------------------------------------------


class TestReactiveBusIntegration:
    """Test the reactive bus with real subscribers."""

    def test_bus_publish_subscribe(self):
        """Publish and subscribe to typed messages."""
        bus = ReactiveBus()
        received: list[Quote] = []

        bus.of_type(Quote).subscribe(received.append)

        quote = Quote(
            instrument=_eq(),
            ltp=Price(value=Decimal("2500")),
            timestamp=_now(),
        )
        bus.publish(quote)

        assert len(received) == 1
        assert received[0].ltp.value == Decimal("2500")

        bus.dispose()

    def test_bus_typed_filtering(self):
        """Bus filters messages by type."""
        bus = ReactiveBus()
        quotes: list[Quote] = []
        candles: list[Candle] = []

        bus.of_type(Quote).subscribe(quotes.append)
        bus.of_type(Candle).subscribe(candles.append)

        # Publish a quote
        bus.publish(
            Quote(
                instrument=_eq(),
                ltp=Price(value=Decimal("100")),
                timestamp=_now(),
            )
        )
        # Publish a candle
        bus.publish(_candle(_eq(), 100.0, _now()))

        assert len(quotes) == 1
        assert len(candles) == 1

        bus.dispose()


# ---------------------------------------------------------------------------
# Test 10: Order flow through execution engine
# ---------------------------------------------------------------------------


class TestExecutionEngineIntegration:
    """Test the execution engine with paper fill source."""

    def test_order_fill_publishes_events(self):
        """Submit order through engine, verify events are published."""
        from tradex_domain.events import OrderFilled, OrderPlaced
        from tradex_domain.market import Quote

        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import PaperFillSource

        bus = ReactiveBus()
        cache = TradingCache()
        # Seed a quote so PaperFillSource can resolve a MARKET fill price.
        cache.set_quote(Quote(
            instrument=_eq(),
            ltp=Price(value=Decimal("100")),
        ))
        engine = ExecutionEngine(bus=bus, fill_source=PaperFillSource(cache=cache), cache=cache)

        placed: list = []
        filled: list = []

        bus.of_type(OrderPlaced).subscribe(placed.append)
        bus.of_type(OrderFilled).subscribe(filled.append)

        request = OrderRequest(
            instrument=_eq(),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
        )
        receipt = engine.submit(request)

        assert receipt is not None
        assert len(placed) == 1
        assert len(filled) == 1

        engine.shutdown()
        bus.dispose()
