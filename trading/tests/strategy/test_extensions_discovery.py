"""Tests for strategy/extensions auto-discovery."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

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


def _candle(close_value: float, day: int) -> Candle:
    price = Price(value=Decimal(str(close_value)))
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=price, high=price, low=price, close=price),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 1, day, tzinfo=UTC),
    )
