"""Tests for StrategyEnsemble."""

from __future__ import annotations

from unittest.mock import MagicMock

from tradex_domain import Candle, Quote
from tradex_domain.enums import OrderSide
from tradex_domain.strategy import StrategyContext

from tradex_trading.strategy.ensemble import StrategyEnsemble, StrategyEntry


def _ctx(**overrides):
    return StrategyContext(**overrides)


def _make_strategy(name: str = "strat", signal_side=None, signal_strength=1.0):
    """Create a mock strategy that returns a fixed signal."""
    strat = MagicMock()
    strat.__class__.__name__ = name
    if signal_side is not None:
        sig = MagicMock()
        sig.direction = signal_side
        sig.side = signal_side
        sig.strength = signal_strength
        sig.instrument = MagicMock()
        strat.on_bar.return_value = sig
        strat.on_quote.return_value = sig
    else:
        strat.on_bar.return_value = None
        strat.on_quote.return_value = None
    return strat


class TestStrategyEntry:
    def test_default_name_from_class(self):
        strat = MagicMock()
        strat.__class__.__name__ = "MyStrat"
        entry = StrategyEntry(strategy=strat)
        assert entry.name == "MyStrat"

    def test_custom_name(self):
        entry = StrategyEntry(strategy=MagicMock(), name="custom")
        assert entry.name == "custom"

    def test_default_weight(self):
        entry = StrategyEntry(strategy=MagicMock())
        assert entry.weight == 1.0


class TestStrategyEnsemble:
    def test_empty_ensemble(self):
        ens = StrategyEnsemble()
        assert len(ens) == 0

    def test_add_strategy(self):
        ens = StrategyEnsemble()
        ens.add(MagicMock(), weight=0.5, name="s1")
        assert len(ens) == 1

    def test_add_chaining(self):
        ens = StrategyEnsemble()
        result = ens.add(MagicMock(), name="s1").add(MagicMock(), name="s2")
        assert result is ens
        assert len(ens) == 2

    def test_remove_strategy(self):
        ens = StrategyEnsemble()
        ens.add(MagicMock(), name="s1")
        ens.remove("s1")
        assert len(ens) == 0

    def test_strategies_property(self):
        ens = StrategyEnsemble()
        ens.add(MagicMock(), name="s1")
        assert len(ens.strategies) == 1

    def test_on_bar_calls_all_strategies(self):
        s1 = _make_strategy("s1", OrderSide.BUY)
        s2 = _make_strategy("s2", OrderSide.SELL)
        ens = StrategyEnsemble([
            StrategyEntry(strategy=s1, name="s1"),
            StrategyEntry(strategy=s2, name="s2"),
        ])
        ctx = _ctx()
        bar = MagicMock(spec=Candle)
        results = ens.on_bar(ctx, bar)
        assert len(results) == 2
        s1.on_bar.assert_called_once()
        s2.on_bar.assert_called_once()

    def test_on_quote_calls_all_strategies(self):
        s1 = _make_strategy("s1", OrderSide.BUY)
        ens = StrategyEnsemble([StrategyEntry(strategy=s1, name="s1")])
        ctx = _ctx()
        quote = MagicMock(spec=Quote)
        results = ens.on_quote(ctx, quote)
        assert len(results) == 1

    def test_on_start_propagates(self):
        s1 = MagicMock()
        s2 = MagicMock()
        ens = StrategyEnsemble([
            StrategyEntry(strategy=s1, name="s1"),
            StrategyEntry(strategy=s2, name="s2"),
        ])
        ctx = _ctx()
        ens.on_start(ctx)
        s1.on_start.assert_called_once()
        s2.on_start.assert_called_once()

    def test_on_stop_propagates(self):
        s1 = MagicMock()
        ens = StrategyEnsemble([StrategyEntry(strategy=s1, name="s1")])
        ctx = _ctx()
        ens.on_stop(ctx)
        s1.on_stop.assert_called_once()

    def test_aggregate_empty_returns_none(self):
        ens = StrategyEnsemble()
        assert ens.aggregate([]) is None

    def test_aggregate_all_none_returns_none(self):
        ens = StrategyEnsemble()
        assert ens.aggregate([("s1", None), ("s2", None)]) is None

    def test_weighted_aggregation_buy(self):
        s1 = _make_strategy("s1", OrderSide.BUY, 0.8)
        ens = StrategyEnsemble(
            [StrategyEntry(strategy=s1, weight=1.0, name="s1")],
            aggregation="weighted",
        )
        results = [("s1", s1.on_bar.return_value)]
        signal = ens.aggregate(results)
        assert signal is not None
        assert signal.direction == OrderSide.BUY

    def test_majority_aggregation(self):
        s1 = _make_strategy("s1", OrderSide.BUY)
        s2 = _make_strategy("s2", OrderSide.BUY)
        s3 = _make_strategy("s3", OrderSide.SELL)
        ens = StrategyEnsemble(
            [
                StrategyEntry(strategy=s1, name="s1"),
                StrategyEntry(strategy=s2, name="s2"),
                StrategyEntry(strategy=s3, name="s3"),
            ],
            aggregation="majority",
            min_votes=2,
        )
        results = [
            ("s1", s1.on_bar.return_value),
            ("s2", s2.on_bar.return_value),
            ("s3", s3.on_bar.return_value),
        ]
        signal = ens.aggregate(results)
        assert signal is not None
        assert signal.direction == OrderSide.BUY

    def test_priority_aggregation(self):
        s1 = _make_strategy("s1", OrderSide.SELL)
        s2 = _make_strategy("s2", OrderSide.BUY)
        ens = StrategyEnsemble(
            [
                StrategyEntry(strategy=s1, name="s1"),
                StrategyEntry(strategy=s2, name="s2"),
            ],
            aggregation="priority",
        )
        results = [
            ("s1", s1.on_bar.return_value),
            ("s2", s2.on_bar.return_value),
        ]
        signal = ens.aggregate(results)
        assert signal is not None
        # Priority returns first strategy's signal
        assert signal.direction == OrderSide.SELL

    def test_strategy_exception_handled(self):
        s1 = MagicMock()
        s1.__class__.__name__ = "Bad"
        s1.on_bar.side_effect = RuntimeError("boom")
        ens = StrategyEnsemble([StrategyEntry(strategy=s1, name="bad")])
        ctx = _ctx()
        bar = MagicMock()
        results = ens.on_bar(ctx, bar)
        assert len(results) == 1
        assert results[0][1] is None  # error → None signal
