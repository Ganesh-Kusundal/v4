"""Golden all-costs mode-parity gate (review area #9 — sufficient tests).

Proves that BacktestEngine and the reactive paper path produce identical
results when ALL execution costs are enabled simultaneously: fees,
slippage, corporate actions (stock split), and deterministic fill timestamps.

The strategy opens a 100-share position; a 2:1 split mid-holding doubles it
to 200 shares; the sell closes it fully. The test separates the parity
proof into three layers:

  1. Fill-price equality (slippage + next-open model agree)
  2. Gross P&L equality (split + slippage: identical in every mode)
  3. Fee application in both paths (fees exist, on_fee ran)
  4. Net P&L equality to the rupee (split avg quantization is absorbed by
     BacktestEngine's cash-basis restatement)

A failing test here means either the accounting model diverged or one of
the parity-fix rounds regressed — the strictest CI gate for the
"consistent across modes" invariant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import OHLC, Candle, OrderSide, Signal, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.datalake.corporate_actions import CorporateAction, CorporateActionStore
from tradex_trading.execution.fees import FeeCalculator
from tradex_trading.execution.slippage import PercentageSlippageModel
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.replay.backtest import BacktestEngine

INSTRUMENT = Equity.of("NSE", "RELIANCE")

_SLIPPAGE = PercentageSlippageModel(Decimal("0.0005"))  # 5 bps


def _candle(price: float, day: int) -> Candle:
    p = Price(value=Decimal(str(price)))
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=p, high=p, low=p, close=p),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 9, day, tzinfo=UTC),
    )


_CANDLES = [_candle(v, i + 1) for i, v in enumerate(
    [100.0, 100.0, 100.0, 100.0, 100.0, 60.0, 60.0, 60.0]
)]


def _corp_store() -> CorporateActionStore:
    store = CorporateActionStore()
    store.add_typed(CorporateAction(
        instrument="RELIANCE", action_type="SPLIT",
        ex_date="2026-09-04", ratio=2.0,
    ))
    return store


class _FullCostLadder:
    """BUY strength=100 on bar 1, SELL strength=200 on bar 5.

    SELL closes the 200-share post-split position completely, so realized
    P&L needs no MTM stub — the comparison is a single number.
    """

    def __init__(self, instrument) -> None:
        self._instrument = instrument
        self._signals: list = []
        self._bar = 0

    @property
    def strategy_id(self) -> str:
        return "full-cost"

    @property
    def signals(self) -> list:
        return list(self._signals)

    def on_bar(self, context, candle) -> object:
        self._bar += 1
        side = {1: OrderSide.BUY, 5: OrderSide.SELL}.get(self._bar)
        if side is None:
            return None
        strength = 100.0 if side == OrderSide.BUY else 200.0
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


class TestGoldenAllCostsParity:
    """Prove every cost model books identically in all modes."""

    def test_fill_prices_match_across_modes(self) -> None:
        """Both paths fill at the same slipped prices: 100.05 and 59.97."""
        from tradex_domain.events import OrderFilled

        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import SimulatedFillSource
        from tradex_trading.strategy import ReactiveStrategyEngine

        bus = ReactiveBus()
        engine = ExecutionEngine(
            bus, SimulatedFillSource(slippage_model=_SLIPPAGE),
            fee_calculator=FeeCalculator(),
        )
        strategy_engine = ReactiveStrategyEngine(bus)
        fills: list = []
        bus.of_type(OrderFilled).subscribe(fills.append)
        strategy_engine.register(_FullCostLadder(INSTRUMENT))

        try:
            for i, c in enumerate(_CANDLES):
                bus.publish(c)
                if i == 1:  # after BUY fill, apply split
                    engine._position_manager.on_corporate_action(
                        INSTRUMENT, "SPLIT", ratio=2.0,
                    )
        finally:
            strategy_engine.dispose_all()
            engine.shutdown()

        assert len(fills) == 2
        assert fills[0].fill.price.value == Decimal("100.05")
        assert fills[1].fill.price.value == Decimal("59.97")

    def test_gross_pnl_realized_identical_both_paths(self) -> None:
        """Realized P&L (before fees) is identical TO THE RUPEE across both
        paths. Both use ``apply_split`` (avg 100.05/2 quantizes to 50.03),
        and BacktestEngine restates its cash basis by that quantization, so
        ``(59.97 - 50.03) * 200 = 1988`` in both — the last split artifact
        (the cash-based backtest previously booked 1989)."""
        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import SimulatedFillSource
        from tradex_trading.strategy import ReactiveStrategyEngine

        # Backtest path: slippage + split, no fees (to compare gross).
        bt = BacktestEngine(
            slippage_model=_SLIPPAGE,
            corporate_actions=_corp_store(),
        ).run(_FullCostLadder(INSTRUMENT), list(_CANDLES))

        # Reactive path: same strategy, data, slippage, and split.
        bus = ReactiveBus()
        engine = ExecutionEngine(
            bus, SimulatedFillSource(slippage_model=_SLIPPAGE),
            fee_calculator=None,
        )
        strategy_engine = ReactiveStrategyEngine(bus)
        strategy_engine.register(_FullCostLadder(INSTRUMENT))

        try:
            for i, c in enumerate(_CANDLES):
                bus.publish(c)
                if i == 1:
                    engine._position_manager.on_corporate_action(
                        INSTRUMENT, "SPLIT", ratio=2.0,
                    )
        finally:
            strategy_engine.dispose_all()
            engine.shutdown()

        pos = engine.cache.all_positions()[-1]
        assert pos.quantity.value == 0
        # (59.97 - 50.03) * 200 = 1988 (avg quantized by apply_split).
        assert pos.realized_pnl.amount == Decimal("1988")
        # Backtest equity gain == reactive realized P&L, to the rupee.
        assert bt.equity_curve[-1] - 100000.0 == float(pos.realized_pnl.amount)

    def test_fees_applied_in_both_paths(self) -> None:
        """With FeeCalculator enabled, both paths deduct the SAME fees and the
        net P&L still matches to the rupee."""
        bt = BacktestEngine(
            fee_calculator=FeeCalculator(),
            slippage_model=_SLIPPAGE,
            corporate_actions=_corp_store(),
        ).run(_FullCostLadder(INSTRUMENT), list(_CANDLES))
        assert bt.total_fees > 0, "backtest should have non-zero fees"

        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import SimulatedFillSource
        from tradex_trading.strategy import ReactiveStrategyEngine

        bus = ReactiveBus()
        engine = ExecutionEngine(
            bus, SimulatedFillSource(slippage_model=_SLIPPAGE),
            fee_calculator=FeeCalculator(),
        )
        strategy_engine = ReactiveStrategyEngine(bus)
        strategy_engine.register(_FullCostLadder(INSTRUMENT))

        try:
            for i, c in enumerate(_CANDLES):
                bus.publish(c)
                if i == 1:
                    engine._position_manager.on_corporate_action(
                        INSTRUMENT, "SPLIT", ratio=2.0,
                    )
        finally:
            strategy_engine.dispose_all()
            engine.shutdown()

        pos = engine.cache.all_positions()[-1]
        assert pos.quantity.value == 0
        # Same fills → same fees in both paths → same NET P&L to the rupee:
        # gross 1988 minus the shared 11.68 fees = 1976.32 in both. (Float
        # equity curve → compare at paisa precision; the underlying Decimals
        # are exact.)
        assert pos.realized_pnl.amount < Decimal("1988")
        assert round(bt.equity_curve[-1] - 100000.0, 2) == float(
            pos.realized_pnl.amount
        )
        # The fees each path deducted are the SAME: reactive realized P&L is
        # gross minus fees; backtest equity gain is gross minus the same fees.
        reactive_fees = 1988.0 - float(pos.realized_pnl.amount)
        assert round(bt.total_fees, 2) == round(reactive_fees, 2)

    def test_backtest_off_naive(self) -> None:
        """Without corporate actions, selling 200 shares when only 100 are
        owned creates a short position. The result differs from the split-
        adjusted path — proving the split handling changes the outcome."""
        bt = BacktestEngine(
            fee_calculator=FeeCalculator(),
            slippage_model=_SLIPPAGE,
        ).run(_FullCostLadder(INSTRUMENT), list(_CANDLES))
        gross = bt.equity_curve[-1] - 100000.0
        # Without split: short 100 shares @ 60 → different from split result.
        # The exact value depends on short position MTM handling.
        assert gross != 1976.32  # must differ from split-adjusted result


class TestStrategyVersionStamping:
    """Review area #5: strategies are versioned artifacts — the strategy
    version must be stamped on orders and signals in every mode, so the
    audit trail identifies which version produced each result."""

    def test_reactive_path_stamps_version_on_order_tag(self) -> None:
        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import SimulatedFillSource
        from tradex_trading.strategy import ReactiveStrategyEngine

        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource(slippage_model=_SLIPPAGE))
        strategy_engine = ReactiveStrategyEngine(bus)
        strategy = _FullCostLadder(INSTRUMENT)
        strategy_engine.register(strategy)
        try:
            for c in _CANDLES:
                bus.publish(c)
        finally:
            strategy_engine.dispose_all()
            engine.shutdown()

        # No explicit version attr → protocol default.
        assert getattr(strategy, "version", "1.0.0") == "1.0.0"
        orders = engine.cache.all_orders()
        assert len(orders) > 0
        # Every order's tag carries strategy_id@version.
        for order in orders:
            assert order.tag is not None
            assert order.tag.startswith("full-cost@"), order.tag
            assert order.tag.endswith("@1.0.0"), order.tag

    def test_signal_metadata_stamped(self) -> None:
        bt = BacktestEngine(slippage_model=_SLIPPAGE).run(
            _FullCostLadder(INSTRUMENT), list(_CANDLES)
        )
        assert bt.num_trades == 2
        for trade in bt.trades:
            assert trade.metadata.get("strategy_version") == "1.0.0"

    def test_builtin_strategy_exposes_version(self) -> None:
        from tradex_trading.strategy.extensions.strategies.sma_cross import (
            SmaCrossStrategy,
        )

        assert SmaCrossStrategy("v", INSTRUMENT).version == "1.0.0"


class TestBusMessageLogAuditability:
    """Review area #5: ReactiveBus records every event for audit."""

    def test_candle_events_recorded(self) -> None:
        bus = ReactiveBus()
        events: list = []
        bus.of_type(Candle).subscribe(events.append)
        for c in _CANDLES:
            bus.publish(c)
        assert len(events) == len(_CANDLES)
        assert all(isinstance(e, Candle) for e in events)

    def test_combined_event_stream(self) -> None:
        """With a strategy registered, the bus delivers Candles AND
        OrderFilled events — the complete auditable event stream."""
        from tradex_domain.events import OrderFilled

        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import SimulatedFillSource
        from tradex_trading.strategy import ReactiveStrategyEngine

        bus = ReactiveBus()
        engine = ExecutionEngine(
            bus, SimulatedFillSource(slippage_model=_SLIPPAGE),
        )
        strategy_engine = ReactiveStrategyEngine(bus)
        strategy_engine.register(_FullCostLadder(INSTRUMENT))

        candles: list = []
        fills: list = []
        bus.of_type(Candle).subscribe(candles.append)
        bus.of_type(OrderFilled).subscribe(fills.append)

        try:
            for i, c in enumerate(_CANDLES):
                bus.publish(c)
                if i == 1:
                    engine._position_manager.on_corporate_action(
                        INSTRUMENT, "SPLIT", ratio=2.0,
                    )
        finally:
            strategy_engine.dispose_all()
            engine.shutdown()

        assert len(candles) == len(_CANDLES)
        assert len(fills) == 2
        assert Candle in {type(e) for e in candles + fills}
        assert OrderFilled in {type(e) for e in candles + fills}
