"""Tests for protective."""
from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain.enums import OrderSide, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import OHLC, Candle
from tradex_domain.protective import stop_hit, target_hit
from tradex_domain.value_objects import Price, Quantity


def _candle(high: str, low: str) -> Candle:
    return Candle(
        instrument=Equity.of("NSE", "RELIANCE"),
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=Decimal("100")),
            high=Price(value=Decimal(high)),
            low=Price(value=Decimal(low)),
            close=Price(value=Decimal("100")),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 9, 29, 10, 0, tzinfo=UTC),
    )


def test_stop_hit_long():
    candle = _candle(high="106", low="95")
    assert stop_hit(candle, Price(value=Decimal("95.00")), OrderSide.BUY) is True


def test_stop_hit_long_not_hit():
    candle = _candle(high="106", low="96")
    assert stop_hit(candle, Price(value=Decimal("95.00")), OrderSide.BUY) is False


def test_stop_hit_short():
    candle = _candle(high="106", low="95")
    assert stop_hit(candle, Price(value=Decimal("105.00")), OrderSide.SELL) is True


def test_stop_hit_short_not_hit():
    candle = _candle(high="104", low="95")
    assert stop_hit(candle, Price(value=Decimal("105.00")), OrderSide.SELL) is False


def test_target_hit_long():
    candle = _candle(high="106", low="99")
    assert target_hit(candle, Price(value=Decimal("105.00")), OrderSide.BUY) is True


def test_target_hit_long_not_hit():
    candle = _candle(high="104", low="99")
    assert target_hit(candle, Price(value=Decimal("105.00")), OrderSide.BUY) is False


def test_target_hit_short():
    candle = _candle(high="101", low="94")
    assert target_hit(candle, Price(value=Decimal("95.00")), OrderSide.SELL) is True


def test_target_hit_short_not_hit():
    candle = _candle(high="101", low="96")
    assert target_hit(candle, Price(value=Decimal("95.00")), OrderSide.SELL) is False
