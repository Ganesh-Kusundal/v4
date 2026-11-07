"""Tests for trading environment factory."""

from tradex_runtime.environments import EnvironmentFactory, EnvironmentType
from tradex_runtime.clocks import SimClock, LiveClock, ReplayClock


class FakeBus:
    pass


def test_create_backtest_env():
    factory = EnvironmentFactory(config=None, event_bus=FakeBus())
    env = factory.create(EnvironmentType.BACKTEST, start=None)
    assert env.env_type == EnvironmentType.BACKTEST
    assert isinstance(env.clock, SimClock)
    assert env.clock.is_live() is False


def test_create_live_env():
    factory = EnvironmentFactory(config=None, event_bus=FakeBus())
    env = factory.create(EnvironmentType.LIVE)
    assert env.env_type == EnvironmentType.LIVE
    assert isinstance(env.clock, LiveClock)
    assert env.clock.is_live() is True


def test_create_replay_env():
    factory = EnvironmentFactory(config=None, event_bus=FakeBus())
    ts = __import__("datetime").datetime(2024, 6, 1, tzinfo=__import__("datetime").timezone.utc)
    env = factory.create(EnvironmentType.REPLAY, timestamps=[ts])
    assert env.env_type == EnvironmentType.REPLAY
    assert isinstance(env.clock, ReplayClock)


def test_unknown_env_type_raises():
    factory = EnvironmentFactory(config=None, event_bus=FakeBus())
    import pytest
    with pytest.raises(ValueError):
        factory.create("INVALID")  # type: ignore[arg-type]
