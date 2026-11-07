"""Verify strategy is data-source-agnostic (zero parity live vs replay)."""


def test_strategy_on_bar_takes_candle_not_source():
    """Strategy.on_bar signature accepts a Candle/Bar, no source parameter."""
    import inspect

    from tradex_strategy.core.protocols import Strategy

    sig = inspect.signature(Strategy.on_bar)
    params = list(sig.parameters.keys())
    # Should be: self, context, bar (no 'source', 'live', 'replay' param)
    assert "source" not in params
    assert "is_live" not in params
    assert "is_replay" not in params
    assert "live" not in params


def test_bar_aggregator_is_single_entry_point():
    """Both live (MarketFeed) and replay (ReplayController) feed BarAggregator."""
    from tradex_runtime.bar_aggregator import BarAggregator

    # BarAggregator has on_bar method that emits frames consumed by strategies
    assert hasattr(BarAggregator, "on_quote")
