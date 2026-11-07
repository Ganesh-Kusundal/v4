"""Tests for BaseStrategy."""

from tradex_strategy.base import BaseStrategy, StrategyContext


class SimpleStrategy(BaseStrategy):
    def on_bar(self, bar, ctx):
        if bar.get("close", 0) > 100:
            return [{"symbol": bar["symbol"], "side": "BUY", "quantity": 1, "order_type": "MARKET"}]
        return []


def test_strategy_lifecycle():
    s = SimpleStrategy(params={"x": 1})
    ctx = StrategyContext(portfolio=None, clock=None)
    assert s.is_started is False
    s.start(ctx)
    assert s.is_started is True
    s.stop()
    assert s.is_stopped is True


def test_strategy_on_bar():
    s = SimpleStrategy()
    ctx = StrategyContext(portfolio=None, clock=None)
    s.start(ctx)
    orders = s.on_bar({"symbol": "A", "close": 105}, ctx)
    assert len(orders) == 1
    assert orders[0]["side"] == "BUY"


def test_strategy_no_signal():
    s = SimpleStrategy()
    ctx = StrategyContext(portfolio=None, clock=None)
    s.start(ctx)
    orders = s.on_bar({"symbol": "A", "close": 95}, ctx)
    assert orders == []


def test_strategy_params():
    s = SimpleStrategy(params={"threshold": 50})
    assert s.params["threshold"] == 50


def test_strategy_name():
    s = SimpleStrategy()
    assert s.name == "SimpleStrategy"
