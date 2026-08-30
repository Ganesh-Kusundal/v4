"""Execution-cost parity (HIGH-6b).

BacktestEngine deducts fees and applies slippage when configured; the reactive
paper/live path must book the SAME costs so net P&L is identical across modes.
ExecutionEngine now takes an optional ``fee_calculator`` (fees deducted from
each position's realized P&L via PositionManager.on_fee) and the fill sources
apply the same slippage models — both wired from ``AppConfig.execution`` by
``runtime.startup.boot``.

Also pins deterministic fill timestamps (reference_timestamp on OrderRequest)
so event logs are reproducible across runs (parity review #5/#8).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tradex_domain import OHLC, Candle, OrderSide, PlaceOrderCommand, Signal, Timeframe
from tradex_domain.enums import OrderType
from tradex_domain.events import OrderFilled
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.config.schema import AppConfig, ExecutionConfig
from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fees import FeeCalculator
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.execution.slippage import PercentageSlippageModel
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.replay.backtest import BacktestEngine
from tradex_trading.runtime.startup import boot
from tradex_trading.strategy import ReactiveStrategyEngine

INSTRUMENT = Equity.of("NSE", "RELIANCE")

_SLIPPAGE = PercentageSlippageModel(pct=Decimal("0.001"))
_FEES = FeeCalculator()


class _Ladder:
    """BUY 2, BUY 1, SELL 1, SELL 2 over 5 chained bars (next-open fills)."""

    def __init__(self, instrument) -> None:
        self._instrument = instrument
        self._signals: list = []
        self._bar = 0

    @property
    def strategy_id(self) -> str:
        return "cost-ladder"

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


def _candle(price: Decimal, day: int) -> Candle:
    p = Price(value=price)
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=p, high=p, low=p, close=p),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 10, day, tzinfo=UTC),
    )


def _ladder_candles() -> list[Candle]:
    return [
        _candle(Decimal(str(p)), day)
        for day, p in enumerate([100, 110, 120, 130, 140], start=1)
    ]


class TestExecutionCostParity:
    """Same fees + slippage → identical net P&L in backtest and reactive paths."""

    def test_fee_parity_net_pnl_matches_exactly(self) -> None:
        """With fees enabled (no slippage, whole prices) both engines book the
        same net realized P&L — every delta and fee quantizes identically."""
        candles = _ladder_candles()

        # Backtest: fees deducted from cash.
        bt = BacktestEngine(fee_calculator=_FEES).run(_Ladder(INSTRUMENT), list(candles))
        assert bt.total_fees > 0

        # Reactive: fees deducted from the position's realized P&L.
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource(), fee_calculator=_FEES)
        strategy_engine = ReactiveStrategyEngine(bus)
        strategy_engine.register(_Ladder(INSTRUMENT))
        try:
            for c in candles:
                bus.publish(c)
            closed = next(
                p for p in engine.cache.all_positions() if p.quantity.value == 0
            )
            net = closed.realized_pnl.amount
        finally:
            strategy_engine.dispose_all()
            engine.shutdown()

        # Equity curve is float-converted; quantize to paisa for exact compare.
        net_backtest = (Decimal(str(bt.equity_curve[-1])) - Decimal("100000"))
        assert net == net_backtest.quantize(Decimal("0.01"))
        assert net < Decimal("70")  # fees shave the gross +70

    def test_slippage_parity_fill_prices_match(self) -> None:
        """With slippage enabled both engines fill at identical slipped prices
        (BUY pays more, SELL receives less)."""
        candles = _ladder_candles()

        bt = BacktestEngine(slippage_model=_SLIPPAGE).run(_Ladder(INSTRUMENT), list(candles))

        bus = ReactiveBus()
        engine = ExecutionEngine(
            bus, SimulatedFillSource(slippage_model=_SLIPPAGE),
        )
        strategy_engine = ReactiveStrategyEngine(bus)
        fills = []
        bus.of_type(OrderFilled).subscribe(fills.append)
        strategy_engine.register(_Ladder(INSTRUMENT))
        try:
            for c in candles:
                bus.publish(c)
            assert len(fills) == 4
            reactive_prices = [f.fill.price.value for f in fills]
        finally:
            strategy_engine.dispose_all()
            engine.shutdown()

        # BUY @110→110.11, @120→120.12; SELL @130→129.87, @140→139.86.
        assert reactive_prices == [
            Decimal("110.11"), Decimal("120.12"),
            Decimal("129.87"), Decimal("139.86"),
        ]
        # Backtest equity gain (gross, no fees) — the same slipped round trip.
        assert bt.equity_curve[-1] - 100000.0 == Decimal("69.25")


class TestExecutionConfigWiring:
    """AppConfig.execution drives boot's fee/slippage/fill-reference wiring."""

    def test_execution_config_from_dict(self) -> None:
        cfg = AppConfig.from_dict({
            "execution": {
                "fees_enabled": True,
                "slippage_bps": "5",
                "fill_reference": "signal_close",
            },
        })
        assert cfg.execution.fees_enabled is True
        assert cfg.execution.slippage_bps == Decimal("5")
        assert cfg.execution.fill_reference == "signal_close"

    def test_execution_config_rejects_unknown_fill_reference(self) -> None:
        with pytest.raises(ValueError, match="fill_reference"):
            AppConfig.from_dict({"execution": {"fill_reference": "bogus"}})

    def test_boot_wires_fees_and_slippage_from_config(self) -> None:
        """boot(execution=...) fills with slippage and deducts fees — end to
        end through the composition root, no strategy needed."""
        session = boot(AppConfig(
            mode="paper",
            execution=ExecutionConfig(fees_enabled=True, slippage_bps=10),
        ))
        try:
            session.bus.publish(PlaceOrderCommand(request=OrderRequest(
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Quantity(value=Decimal("10")),
                price=Price(value=Decimal("100")),
            )))
            pos = session.engine.cache.all_positions()[0]
            # Slippage: BUY pays more (100 * 1.001 = 100.10).
            assert pos.avg_price.value == Decimal("100.10")
            # Fees: deducted from realized P&L (negative).
            assert pos.realized_pnl.amount < 0
        finally:
            session.stop()


    def test_paper_factory_honors_execution_config(self) -> None:
        """TradingSession.paper(config=...) wires fees + slippage too, so the
        SDK factory and boot() book identical execution costs."""
        from tradex_trading.sdk.session import TradingSession

        session = TradingSession.paper(config=AppConfig(
            execution=ExecutionConfig(fees_enabled=True, slippage_bps=10),
        ))
        try:
            session.bus.publish(PlaceOrderCommand(request=OrderRequest(
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Quantity(value=Decimal("10")),
                price=Price(value=Decimal("100")),
            )))
            pos = session.engine.cache.all_positions()[0]
            assert pos.avg_price.value == Decimal("100.10")  # slipped
            assert pos.realized_pnl.amount < 0  # fee deducted
        finally:
            session.stop()


class TestDeterministicFillTimestamps:
    """reference_timestamp makes reactive fills reproducible across runs."""

    def test_reactive_fill_stamps_the_fill_candle_timestamp(self) -> None:
        candles = _ladder_candles()  # bar-1 signal → fill at bar-2 open
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource())
        strategy_engine = ReactiveStrategyEngine(bus)
        fills = []
        bus.of_type(OrderFilled).subscribe(fills.append)
        strategy_engine.register(_Ladder(INSTRUMENT))
        try:
            for c in candles:
                bus.publish(c)
            assert len(fills) == 4
            # Fill timestamps are the fill candles' timestamps, not now().
            assert [f.fill.timestamp for f in fills] == [c.timestamp for c in candles[1:]]
        finally:
            strategy_engine.dispose_all()
            engine.shutdown()
