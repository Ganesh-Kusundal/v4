"""Live parity test — the paper session path end-to-end via public API.

Proves the documented live-mode flow with no mocks and no internals:

    TradingSession.paper() → ReactiveStrategyEngine.register(discovered strategy)
        → bus.publish(Candle) → Signal → PlaceOrderCommand
        → ExecutionEngine (PaperFillSource) → OrderPlaced + OrderFilled
        → OMS cache + PositionManager

Everything is driven and asserted through the documented public surface:
``TradingSession.paper()``, ``session.bus``, ``session.stream.subscribe_fills()``,
``session.engine.cache``, ``session.trade.get_order()`` / ``get_orderbook()``,
``session.portfolio.positions()``, and ``session.stop()``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import OHLC, Candle, OrderFilled, OrderSide, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.sdk.session import TradingSession
from tradex_trading.strategy import ReactiveStrategyEngine
from tradex_trading.strategy.extensions import all_strategies

# The shipped SMA-crossover example: BUY on the fast/SMA crossing above the
# slow/SMA. Discovered via the public ``all_strategies`` registry.
_DISCOVERED_ID = "sma_cross_example"


def _candle(instrument, close: float, day: int) -> Candle:
    price = Price(value=Decimal(str(close)))
    return Candle(
        instrument=instrument,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=price, high=price, low=price, close=price),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 8, day, tzinfo=UTC),
    )


class TestPaperSessionLiveParity:
    """The documented paper path, driven end-to-end through the session."""

    def test_discovered_strategy_order_lands_in_oms_and_positions(self) -> None:
        bus = ReactiveBus()
        session = TradingSession.paper(bus=bus)
        strategy_engine = ReactiveStrategyEngine(session.bus)
        # NOTE: this registers the module-level discovered singleton, whose
        # close history accumulates across on_bar calls. The 1-order/1-fill
        # assertions below assume no other test drives the singleton with
        # candles before this one — deterministic today, keep it that way.
        strategy = next(
            s for s in all_strategies if s.strategy_id == _DISCOVERED_ID
        )
        strategy_engine.register(strategy)
        fills: list[OrderFilled] = []
        sub = session.stream.subscribe_fills(fills.append)
        try:
            # 20 flat closes then a rising leg → fast SMA5 crosses above SMA20 → BUY.
            closes = [10.0] * 20 + [11.0, 12.0, 13.0]
            for day, close in enumerate(closes, start=1):
                session.bus.publish(_candle(strategy.instrument, close, day))

            # The Signal was bridged into an order that filled on the paper engine.
            orders = session.engine.cache.all_orders()
            assert len(orders) == 1
            order = orders[0]
            assert order.side == OrderSide.BUY
            assert order.instrument.instrument_id == strategy.instrument.instrument_id
            assert len(fills) == 1

            # The order is visible through the documented trade service.
            fetched = session.trade.get_order(order.order_id)
            assert fetched.order_id == order.order_id
            assert session.trade.get_orderbook() == [order]

            # PositionManager recorded the fill — public portfolio surface.
            positions = session.portfolio.positions()
            assert any(
                p.instrument.instrument_id == strategy.instrument.instrument_id
                for p in positions
            )
        finally:
            strategy_engine.dispose_all()
            sub.cancel()
            session.stop()

    def test_strategy_signal_reaches_stream_fills_public_api(self) -> None:
        """The OrderFilled event is delivered to stream subscribers — the
        documented reactive fill path, not just the OMS cache.

        Uses a fresh instance of the discovered strategy's class (the
        module-level singleton is stateful across runs, so behavior tests
        construct their own copy — same class the discovery validates).
        """
        from tradex_trading.strategy.extensions.strategies.sma_cross import (
            SmaCrossStrategy,
        )

        bus = ReactiveBus()
        session = TradingSession.paper(bus=bus)
        strategy_engine = ReactiveStrategyEngine(session.bus)
        strategy = SmaCrossStrategy("stream_fill_test", Equity.of("NSE", "RELIANCE"))
        strategy_engine.register(strategy)
        fills: list[OrderFilled] = []
        sub = session.stream.subscribe_fills(fills.append)
        try:
            closes = [10.0] * 20 + [11.0, 12.0, 13.0]
            for day, close in enumerate(closes, start=1):
                session.bus.publish(_candle(strategy.instrument, close, day))
            assert len(fills) == 1
            assert fills[0].fill.instrument.instrument_id == strategy.instrument.instrument_id
        finally:
            strategy_engine.dispose_all()
            sub.cancel()
            session.stop()

    def test_paper_session_is_ready_and_stoppable(self) -> None:
        """The factory returns a READY session; stop() tears everything down."""
        session = TradingSession.paper()
        try:
            assert session.state.value == "READY"
            assert session.mode == "paper"
        finally:
            session.stop()
        assert session.state.value == "STOPPED"
