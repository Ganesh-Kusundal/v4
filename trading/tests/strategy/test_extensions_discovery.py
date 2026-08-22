"""Tests for strategy/extensions auto-discovery."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tradex_domain import OHLC, Candle, OrderSide
from tradex_domain.enums import ExchangeId, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.strategy import ScannerDefinition, StrategyContext
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.strategy.core.protocols import Strategy
from tradex_trading.strategy.extensions import all_scanners, all_strategies

INSTRUMENT = Equity.of(ExchangeId.NSE, "RELIANCE")


class TestExtensionsDiscovery:
    """The documented discovery contract: isinstance-validated object lists."""

    def test_all_strategies_are_protocol_instances(self) -> None:
        assert len(all_strategies) >= 1
        for strategy in all_strategies:
            assert isinstance(strategy, Strategy)
            assert strategy.strategy_id

    def test_all_scanners_are_definitions(self) -> None:
        assert len(all_scanners) >= 1
        for definition in all_scanners:
            assert isinstance(definition, ScannerDefinition)
            assert definition.conditions

    def test_package_init_reexports_discovery(self) -> None:
        from tradex_trading.strategy import all_scanners as pkg_scanners
        from tradex_trading.strategy import all_strategies as pkg_strategies

        assert pkg_strategies == all_strategies
        assert pkg_scanners == all_scanners


class TestExampleSmaCross:
    """The reference extension strategy behaves like a Strategy."""

    def test_emits_buy_on_up_cross(self) -> None:
        from tradex_trading.strategy.extensions.strategies.sma_cross import (
            SmaCrossStrategy,
        )

        strat = SmaCrossStrategy("cross_test", INSTRUMENT, fast=2, slow=3)
        ctx = StrategyContext()
        # Down then sharp up: slow(SMA3) above fast(SMA2) → BUY on the up-cross.
        signals = [
            sig
            for i, close in enumerate([10, 11, 10, 9, 15], start=1)
            if (sig := strat.on_bar(ctx, _candle(close, i))) is not None
        ]
        assert signals
        assert signals[0].direction == OrderSide.SELL
        assert signals[0].reason == "sma_cross_down"
        assert signals[-1].direction == OrderSide.BUY
        assert signals[-1].reason == "sma_cross_up"

    def test_public_strategy_surface_from_package(self) -> None:
        from tradex_trading.strategy import ReactiveStrategyEngine, ScannerEngine

        assert ReactiveStrategyEngine is not None
        assert ScannerEngine is not None


class TestExampleMeanReversion:
    """The second extension strategy: SELL on overbought, BUY on oversold."""

    def test_discovered_and_protocol_conformant(self) -> None:
        from tradex_trading.strategy.extensions.strategies.mean_reversion import (
            MeanReversionStrategy,
            mean_reversion_strategy,
        )

        assert isinstance(mean_reversion_strategy, Strategy)
        assert mean_reversion_strategy.strategy_id == "mean_reversion_example"
        assert any(
            s.strategy_id == "mean_reversion_example" for s in all_strategies
        )
        assert MeanReversionStrategy is not None

    def test_emits_sell_on_overbought(self) -> None:
        from tradex_trading.strategy.extensions.strategies.mean_reversion import (
            MeanReversionStrategy,
        )

        strat = MeanReversionStrategy(
            "mr_sell_test", INSTRUMENT, period=2, overbought=70.0, oversold=30.0
        )
        ctx = StrategyContext()
        # Strictly rising closes → RSI pins at 100 (> overbought) → SELL.
        signals = [
            sig
            for i, close in enumerate([10, 11, 12, 13, 14, 15], start=1)
            if (sig := strat.on_bar(ctx, _candle(close, i))) is not None
        ]
        assert signals
        assert signals[0].direction == OrderSide.SELL
        assert signals[0].reason == "rsi_overbought"
        assert signals[0].instrument == INSTRUMENT

    def test_emits_buy_on_oversold(self) -> None:
        from tradex_trading.strategy.extensions.strategies.mean_reversion import (
            MeanReversionStrategy,
        )

        strat = MeanReversionStrategy(
            "mr_buy_test", INSTRUMENT, period=2, overbought=70.0, oversold=30.0
        )
        ctx = StrategyContext()
        # Strictly falling closes → RSI pins at 0 (< oversold) → BUY.
        signals = [
            sig
            for i, close in enumerate([15, 14, 13, 12, 11, 10], start=1)
            if (sig := strat.on_bar(ctx, _candle(close, i))) is not None
        ]
        assert signals
        assert signals[0].direction == OrderSide.BUY
        assert signals[0].reason == "rsi_oversold"

    def test_no_signal_spam_inside_extreme_zone(self) -> None:
        from tradex_trading.strategy.extensions.strategies.mean_reversion import (
            MeanReversionStrategy,
        )

        strat = MeanReversionStrategy(
            "mr_no_spam", INSTRUMENT, period=2, overbought=70.0, oversold=30.0
        )
        ctx = StrategyContext()
        # RSI stays overbought for 4 consecutive bars — only 1 SELL, no repeats.
        signals = [
            sig
            for i, close in enumerate([10, 11, 12, 13, 14, 15], start=1)
            if (sig := strat.on_bar(ctx, _candle(close, i))) is not None
        ]
        assert len(signals) == 1
        assert signals[0].direction == OrderSide.SELL

    def test_flat_prices_emit_no_signal(self) -> None:
        from tradex_trading.strategy.extensions.strategies.mean_reversion import (
            MeanReversionStrategy,
        )

        strat = MeanReversionStrategy(
            "mr_flat", INSTRUMENT, period=2, overbought=70.0, oversold=30.0
        )
        ctx = StrategyContext()
        # All deltas are zero → RSI is neutral (50) → nothing emitted.
        signals = [
            sig
            for i, close in enumerate([10, 10, 10, 10, 10, 10], start=1)
            if (sig := strat.on_bar(ctx, _candle(close, i))) is not None
        ]
        assert signals == []

    def test_invalid_levels_rejected(self) -> None:
        from tradex_trading.strategy.extensions.strategies.mean_reversion import (
            MeanReversionStrategy,
        )

        with pytest.raises(ValueError, match="oversold"):
            MeanReversionStrategy("bad_levels", INSTRUMENT, oversold=80.0)


class TestExampleMultiSymbolSmaCross:
    """The portfolio-style strategy: one instance trades every instrument."""

    def test_discovered_and_protocol_conformant(self) -> None:
        from tradex_trading.strategy.extensions.strategies.multi_symbol_sma_cross import (
            MultiSymbolSmaCross,
            multi_symbol_sma_cross,
        )

        assert isinstance(multi_symbol_sma_cross, Strategy)
        assert multi_symbol_sma_cross.strategy_id == "multi_symbol_sma_cross"
        assert any(
            s.strategy_id == "multi_symbol_sma_cross" for s in all_strategies
        )
        assert MultiSymbolSmaCross is not None

    def test_invalid_periods_rejected(self) -> None:
        from tradex_trading.strategy.extensions.strategies.multi_symbol_sma_cross import (
            MultiSymbolSmaCross,
        )

        with pytest.raises(ValueError, match="fast SMA"):
            MultiSymbolSmaCross(fast=20, slow=10)

    def test_signals_attributed_per_instrument(self) -> None:
        """Interleaved candles from two symbols produce the same per-symbol
        signal sequence as running each symbol standalone — the property the
        multi-symbol ``BacktestEngine`` relies on (signals matched per
        instrument id)."""
        from tradex_trading.strategy.extensions.strategies.multi_symbol_sma_cross import (
            MultiSymbolSmaCross,
        )

        other = Equity.of(ExchangeId.NSE, "TCS")
        rel_closes = [10, 11, 10, 9, 15]
        tcs_closes = [10, 12, 10, 8, 16]
        ctx = StrategyContext()
        strat = MultiSymbolSmaCross(fast=2, slow=3)

        candles = []
        for i in range(len(rel_closes)):
            candles.append(_candle(rel_closes[i], i + 1))
            candles.append(_candle(tcs_closes[i], i + 1, other))
        signals = [
            sig for candle in candles
            if (sig := strat.on_bar(ctx, candle)) is not None
        ]

        rel_signals = [s for s in signals if s.instrument == INSTRUMENT]
        tcs_signals = [s for s in signals if s.instrument == other]
        assert rel_signals and tcs_signals
        # Every signal references exactly one of the two traded instruments.
        assert all(s.instrument in (INSTRUMENT, other) for s in signals)

        def _standalone_directions(closes: list[float], instrument) -> list:
            standalone = MultiSymbolSmaCross(fast=2, slow=3)
            return [
                sig.direction
                for i, close in enumerate(closes, start=1)
                if (sig := standalone.on_bar(ctx, _candle(close, i, instrument))) is not None
            ]

        # Interleaved feeding must match each symbol run standalone — the
        # property the multi-symbol BacktestEngine relies on.
        assert [s.direction for s in rel_signals] == _standalone_directions(
            rel_closes, INSTRUMENT
        )
        assert [s.direction for s in tcs_signals] == _standalone_directions(
            tcs_closes, other
        )


class TestExamplePullbackScanner:
    """The second extension scanner combines two conditions."""

    def test_discovered_with_two_conditions(self) -> None:
        from tradex_trading.strategy.extensions.scanners.pullback import (
            pullback_scanner,
        )

        assert any(s is pullback_scanner for s in all_scanners)
        assert isinstance(pullback_scanner, ScannerDefinition)
        assert len(pullback_scanner.conditions) == 2
        assert {c.name for c in pullback_scanner.conditions} == {"close", "rsi"}
        assert pullback_scanner.limit == 20


class TestNifty500TechnicalScanner:
    """The Nifty 500 technical screener extension."""

    def test_discovered_with_technical_conditions(self) -> None:
        from tradex_trading.strategy.extensions.scanners.nifty500_technical import (
            nifty500_technical_scanner,
        )

        assert any(s is nifty500_technical_scanner for s in all_scanners)
        assert isinstance(nifty500_technical_scanner, ScannerDefinition)
        assert {c.name for c in nifty500_technical_scanner.conditions} == {"close", "roc"}
        assert nifty500_technical_scanner.limit == 20

    def test_universe_nonempty(self) -> None:
        from tradex_trading.strategy.extensions.scanners.nifty500_technical import (
            _UNIVERSE,
        )

        # Loaded from the nifty500 CSV when present; falls back to examples.
        assert len(_UNIVERSE) >= 1

    def test_boot_can_run_it(self, monkeypatch) -> None:
        """The nifty500 scanner runs through a booted session's ScannerService."""
        from unittest.mock import MagicMock

        from tradex_domain.market import HistoricalSeries

        from tradex_trading.config.schema import AppConfig
        from tradex_trading.runtime.startup import boot
        from tradex_trading.strategy.extensions.scanners.nifty500_technical import (
            nifty500_technical_scanner,
        )

        def fake_broker() -> MagicMock:
            broker = MagicMock()

            def history(instrument, timeframe, start, end) -> HistoricalSeries:
                return HistoricalSeries(
                    instrument=instrument, timeframe=timeframe, candles=[],
                    start=start, end=end,
                )

            broker.history = history
            return broker

        session = boot(AppConfig(mode="paper"), broker=fake_broker())
        try:
            results = session.scanner.run(nifty500_technical_scanner)
            assert isinstance(results, list)
        finally:
            session.stop()


class TestTypoInAllDegradesGracefully:
    """A typo in a user's ``__all__`` must not break discovery.

    ``_collect`` skips names missing from the package namespace instead of
    raising — the documented degradation is "not discovered", not "import
    explodes".
    """

    def test_missing_name_is_skipped_for_strategies(self) -> None:
        from tradex_trading.strategy.extensions import _collect
        from tradex_trading.strategy.extensions.strategies.sma_cross import (
            sma_cross_strategy,
        )

        class FakeStrategies:
            __all__ = ["sma_cross_strategy", "typo_strategy"]

        FakeStrategies.sma_cross_strategy = sma_cross_strategy
        # typo_strategy is intentionally NOT set on the namespace.

        collected = _collect(FakeStrategies, Strategy)
        assert collected == (sma_cross_strategy,)

    def test_missing_name_is_skipped_for_scanners(self) -> None:
        from tradex_domain.strategy import ScannerDefinition as ScannerDef

        from tradex_trading.strategy.extensions import _collect
        from tradex_trading.strategy.extensions.scanners.momentum import (
            momentum_scanner,
        )

        class FakeScanners:
            __all__ = ["momentum_scanner", "typo_scanner"]

        FakeScanners.momentum_scanner = momentum_scanner

        collected = _collect(FakeScanners, ScannerDef)
        assert collected == (momentum_scanner,)

    def test_all_typos_yield_empty_result(self) -> None:
        from tradex_trading.strategy.extensions import _collect

        class AllTypos:
            __all__ = ["nope_strategy", "missing_strategy"]

        collected = _collect(AllTypos, Strategy)
        assert collected == ()


def _candle(close_value: float, day: int, instrument=INSTRUMENT) -> Candle:
    price = Price(value=Decimal(str(close_value)))
    return Candle(
        instrument=instrument,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=price, high=price, low=price, close=price),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 1, day, tzinfo=UTC),
    )
