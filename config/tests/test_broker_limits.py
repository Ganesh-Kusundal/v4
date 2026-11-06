"""Tests for broker-specific limits centralization."""

from tradex_config.broker_limits import BROKER_LIMITS, BrokerIntradayLimits


def test_broker_limits_defaults() -> None:
    assert BROKER_LIMITS.dhan_max_days == 90
    assert BROKER_LIMITS.upstox_max_days == 30


def test_broker_limits_frozen() -> None:
    import pytest

    with pytest.raises(Exception):
        BROKER_LIMITS.dhan_max_days = 10  # type: ignore[misc]


def test_broker_limits_type() -> None:
    assert isinstance(BROKER_LIMITS, BrokerIntradayLimits)
