"""Cross-environment determinism smoke test."""

from datetime import UTC, datetime

from tradex_runtime.clocks import SimClock
from tradex_runtime.environments import EnvironmentFactory, EnvironmentType


def test_backtest_env_clock_deterministic():
    factory = EnvironmentFactory(config=None, event_bus=object())
    env = factory.create(EnvironmentType.BACKTEST, start=datetime(2024, 6, 1, tzinfo=UTC))
    assert isinstance(env.clock, SimClock)
    # Advance clock
    env.clock.advance(datetime(2024, 6, 1, 9, 15, tzinfo=UTC))
    assert env.clock.now() == datetime(2024, 6, 1, 9, 15, tzinfo=UTC)
    # Second advance is monotonic
    env.clock.advance(datetime(2024, 6, 1, 9, 16, tzinfo=UTC))
    assert env.clock.now() == datetime(2024, 6, 1, 9, 16, tzinfo=UTC)


def test_replay_env_clock():
    ts = datetime(2024, 6, 1, 9, 15, tzinfo=UTC)
    factory = EnvironmentFactory(config=None, event_bus=object())
    env = factory.create(EnvironmentType.REPLAY, timestamps=[ts])
    assert env.clock.now() == ts


def test_live_env_clock_is_live():
    factory = EnvironmentFactory(config=None, event_bus=object())
    env = factory.create(EnvironmentType.LIVE)
    assert env.clock.is_live() is True


def test_create_with_strategy():
    from tradex_strategy.base import BaseStrategy
    from tradex_strategy.registry import clear, register

    clear()

    @register("test_strat")
    class TS(BaseStrategy):
        def on_bar(self, bar, ctx):
            return []

    factory = EnvironmentFactory(config=None, event_bus=object())
    env, strategy = factory.create_with_strategy(EnvironmentType.BACKTEST, "test_strat", {"x": 1})
    assert env.env_type == EnvironmentType.BACKTEST
    assert strategy.params["x"] == 1
    clear()
