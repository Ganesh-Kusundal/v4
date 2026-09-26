"""Live parity test — the paper and backtest session paths end-to-end.

Proves the documented execution-mode flow with no mocks and no internals.
Paper path:

    TradingSession.paper() → ReactiveStrategyEngine.register(discovered strategy)
        → bus.publish(Candle) → Signal → PlaceOrderCommand
        → ExecutionEngine (PaperFillSource) → OrderPlaced + OrderFilled
        → OMS cache + PositionManager

Backtest path (mode parity through the composition root):

    boot(AppConfig(mode="backtest")) → same flow via SimulatedFillSource
        → OMS cache + PositionManager

Every order-producing path asserts the exact documented CQRS event spine
on the bus — PlaceOrderCommand → OrderPlaced → OrderFilled. ReactiveBus
publishes causally (nested publishes are drained before publish returns), so
the raw stream order matches the message log; boot's shared bus interleaves
collateral orders from both discovered strategies on the same instrument, so
its assertions stay scoped by tag/instrument.

Everything is driven and asserted through the documented public surface:
``TradingSession.paper()``, ``session.bus``, ``session.bus.of_type(OrderFilled).subscribe()``,
``session.engine.cache``, ``session.engine.get_order()`` / ``all_orders()``,
``session.engine.cache.all_positions()``, and ``session.stop()``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from tradex_domain import (
    OHLC,
    Candle,
    OrderFilled,
    OrderPlaced,
    OrderSide,
    PlaceOrderCommand,
    Timeframe,
)
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.config.schema import AppConfig
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.runtime.startup import boot
from tradex_trading.sdk.session import TradingSession
from tradex_trading.strategy import ReactiveStrategyEngine
from tradex_trading.strategy.extensions import all_strategies

# The shipped SMA-crossover example: BUY on the fast/SMA crossing above the
# slow/SMA. Discovered via the public ``all_strategies`` registry.
_DISCOVERED_ID = "sma_cross_example"
# The shipped RSI mean-reversion example: SELL on overbought / BUY on oversold.
_MEAN_REVERSION_ID = "mean_reversion_example"


def _candle(instrument, close: float, day: int) -> Candle:
    price = Price(value=Decimal(str(close)))
    return Candle(
        instrument=instrument,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=price, high=price, low=price, close=price),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 8, day, tzinfo=UTC),
    )


def _record_bus_events(bus) -> tuple[list[object], Any]:
    """Record every bus message in emission order (raw stream)."""
    events: list[object] = []
    sub = bus.stream().subscribe(events.append)
    return events, sub


def _event_instrument(event) -> object | None:
    """The instrument a CQRS event carries (request/order/fill payload)."""
    payload = (
        getattr(event, "request", None)
        or getattr(event, "order", None)
        or getattr(event, "fill", None)
    )
    return getattr(payload, "instrument", None)


def _events_of(
    events: list[object], event_type: type, instrument,
) -> list[object]:
    """Every *event_type* bus message for *instrument*."""
    return [
        e for e in events
        if isinstance(e, event_type)
        and _event_instrument(e).instrument_id == instrument.instrument_id
    ]


def _cqrs_sequence(events: list[object], instrument) -> list[type]:
    """The exact documented CQRS event spine for *instrument*:

        PlaceOrderCommand → OrderPlaced → OrderFilled

    (Strategy Signal bridge → engine OMS update → fill publication.)
    Events for other instruments — e.g. collateral orders from boot()'s
    other registered singletons on the shared bus — are ignored, so the
    assertion is deterministic in isolation and in the full file.
    """
    wanted = (PlaceOrderCommand, OrderPlaced, OrderFilled)
    return [
        type(e) for e in events
        if isinstance(e, wanted)
        and _event_instrument(e).instrument_id == instrument.instrument_id
    ]


class TestPaperSessionLiveParity:
    """The documented paper path, driven end-to-end through the session."""

    def test_discovered_strategy_order_lands_in_oms_and_positions(self) -> None:
        # The bus message log records every publish in true causal order
        # (the engine processes PlaceOrderCommand synchronously inside the
        # command's delivery, so a stream recorder would see its effects
        # first). The exact documented CQRS spine is asserted below.
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
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
        sub = session.bus.of_type(OrderFilled).subscribe(fills.append)
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

            # The exact documented CQRS event spine, in true publish order.
            assert _cqrs_sequence(events, strategy.instrument) == [
                PlaceOrderCommand, OrderPlaced, OrderFilled,
            ]

            # The order is visible through the execution engine cache.
            fetched = session.engine.get_order(order.order_id)
            assert fetched.order_id == order.order_id
            assert session.engine.all_orders() == [order]

            # PositionManager recorded the fill — public portfolio surface.
            positions = session.engine.cache.all_positions()
            assert any(
                p.instrument.instrument_id == strategy.instrument.instrument_id
                for p in positions
            )
        finally:
            strategy_engine.dispose_all()
            sub.dispose()
            session.stop()

    def test_mean_reversion_sell_lands_as_short_position(self) -> None:
        """The discovered mean-reversion example emits SELL on overbought;
        the paper fill opens a short position (negative quantity) in the
        PositionManager — the SELL mirror of the BUY case above.

        NOTE: like test 1, this registers the module-level discovered
        singleton, whose close history accumulates across on_bar calls. Only
        this test drives it with candles, so the 1-order/1-SELL assertion is
        deterministic today — keep it that way.
        """
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        session = TradingSession.paper(bus=bus)
        strategy_engine = ReactiveStrategyEngine(session.bus)
        strategy = next(
            s for s in all_strategies if s.strategy_id == _MEAN_REVERSION_ID
        )
        strategy_engine.register(strategy)
        fills: list[OrderFilled] = []
        sub = session.bus.of_type(OrderFilled).subscribe(fills.append)
        try:
            # 15 strictly rising closes → RSI = 100 → transition into
            # overbought fires exactly one SELL on the 15th bar. A 16th
            # neutral bar lets the deferred order fill at its open (next_open
            # model — same fill timing as BacktestEngine).
            closes = [10.0 + i for i in range(15)] + [25.0]
            for day, close in enumerate(closes, start=1):
                session.bus.publish(_candle(strategy.instrument, close, day))

            orders = session.engine.cache.all_orders()
            assert len(orders) == 1
            order = orders[0]
            assert order.side == OrderSide.SELL
            assert order.instrument.instrument_id == strategy.instrument.instrument_id
            assert len(fills) == 1
            assert fills[0].fill.side == OrderSide.SELL

            # The exact CQRS event spine — a SELL flows through the same
            # contract as the BUY case above.
            assert _cqrs_sequence(events, strategy.instrument) == [
                PlaceOrderCommand, OrderPlaced, OrderFilled,
            ]

            # The SELL fill opened a short: negative quantity in the manager.
            positions = session.engine.cache.all_positions()
            short = next(
                (
                    p for p in positions
                    if p.instrument.instrument_id == strategy.instrument.instrument_id
                ),
                None,
            )
            assert short is not None
            assert short.quantity.value < 0
            assert short.is_short
        finally:
            strategy_engine.dispose_all()
            sub.dispose()
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
        sub = session.bus.of_type(OrderFilled).subscribe(fills.append)
        try:
            closes = [10.0] * 20 + [11.0, 12.0, 13.0]
            for day, close in enumerate(closes, start=1):
                session.bus.publish(_candle(strategy.instrument, close, day))
            assert len(fills) == 1
            assert fills[0].fill.instrument.instrument_id == strategy.instrument.instrument_id
        finally:
            strategy_engine.dispose_all()
            sub.dispose()
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

    def test_backtest_boot_path_lands_order_via_simulated_fill(self) -> None:
        """boot(AppConfig(mode="backtest")) + discovered strategy — the same
        end-to-end flow as paper, through the boot-composed engine and
        SimulatedFillSource. Mode parity: order → fill → position, and the
        Signal→Order bridge stamps a reference price from the triggering
        candle, so the simulated fill lands at a real price (no more
        zero-priced fills that corrupted P&L).

        NOTE: boot() registers BOTH discovered singletons on the shared bus
        (candles broadcast to every strategy), and the sma_cross singleton
        accumulates history across tests in this process — so a collateral
        sma_cross order can appear depending on that accumulated state.
        Assertions below are scoped by tag/instrument to the strategy under
        test, making the outcome deterministic in isolation and in the full
        file. The falling series is also order-independent: it transitions
        the mean-reversion singleton to oversold (BUY) whether it enters
        neutral or overbought.
        """
        from dataclasses import replace

        config = AppConfig(mode="backtest")
        # Examples are opt-in; this test drives a discovered one deliberately.
        config = replace(
            config,
            execution=replace(config.execution, auto_register_examples=True),
        )
        session = boot(config)
        strategy = next(
            s for s in all_strategies if s.strategy_id == _MEAN_REVERSION_ID
        )
        fills: list[OrderFilled] = []
        sub = session.bus.of_type(OrderFilled).subscribe(fills.append)
        events, rec_sub = _record_bus_events(session.bus)
        try:
            assert session.mode == "backtest"
            assert session.state.value == "READY"

            # 15 strictly falling closes → RSI falls below the oversold
            # level (30) on a transition → exactly one BUY.
            closes = [24.0 - i for i in range(15)]
            for day, close in enumerate(closes, start=1):
                session.bus.publish(_candle(strategy.instrument, close, day))

            # Scope to the discovered strategy's own orders (its tag is
            # strategy_id@version stamped by the Signal→Order bridge at boot,
            # so the versioned audit trail is preserved — review area #5).
            orders = session.engine.cache.all_orders()
            mr_orders = [
                o for o in orders
                if o.tag is not None and o.tag.startswith(f"{_MEAN_REVERSION_ID}@")
            ]
            assert len(mr_orders) == 1
            order = mr_orders[0]
            assert order.side == OrderSide.BUY
            assert order.instrument.instrument_id == strategy.instrument.instrument_id
            # The order is visible through the execution engine, as
            # in the paper path (membership — collateral orders may exist).
            assert session.engine.get_order(order.order_id).order_id == order.order_id
            assert order in session.engine.all_orders()

            mr_fills = [
                f for f in fills
                if f.fill.instrument.instrument_id == strategy.instrument.instrument_id
            ]
            assert len(mr_fills) == 1
            assert mr_fills[0].fill.side == OrderSide.BUY
            # The bridge stamped the triggering candle's close as the order
            # price, so the simulated fill is at a real, non-zero price.
            assert mr_fills[0].fill.price.value > 0

            # ReactiveBus drains nested publishes causally, so the stream
            # order matches the message log. boot()'s shared bus interleaves
            # collateral orders from its other registered singletons (same
            # instrument), so assertions stay scoped by tag/instrument rather
            # than asserting a whole-stream sequence.
            pocs = _events_of(events, PlaceOrderCommand, strategy.instrument)
            placed = _events_of(events, OrderPlaced, strategy.instrument)
            filled = _events_of(events, OrderFilled, strategy.instrument)
            assert len(pocs) == 1 and len(placed) == 1 and len(filled) == 1
            # Causal chain, not just counts: the fill references the placed
            # order, and the order matches the strategy's command.
            assert placed[0].order.order_id == filled[0].fill.order_id
            assert pocs[0].request.side == placed[0].order.side == OrderSide.BUY
            # Tag is strategy_id@version (versioned audit trail, area #5).
            assert pocs[0].request.tag == f"{_MEAN_REVERSION_ID}@1.0.0"
            # The bridge now stamps a reference price + deterministic
            # correlation id derived from the triggering event (parity fix:
            # no more price-less MARKET orders filling at zero).
            assert pocs[0].request.price is not None
            assert pocs[0].request.price.value > 0
            assert pocs[0].request.correlation_id is not None
            assert placed[0].order.correlation_id == pocs[0].request.correlation_id

            positions = session.engine.cache.all_positions()
            long = next(
                (
                    p for p in positions
                    if p.instrument.instrument_id == strategy.instrument.instrument_id
                ),
                None,
            )
            assert long is not None
            assert long.quantity.value > 0
            assert long.is_long
        finally:
            rec_sub.dispose()
            sub.dispose()
            session.stop()
