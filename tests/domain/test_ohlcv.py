"""Tests for ohlcv."""
from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import OHLC, Candle
from tradex_domain.ohlcv import aggregate_candles
from tradex_domain.value_objects import Price, Quantity


def _candle(
    *,
    open: str,
    high: str,
    low: str,
    close: str,
    volume: str,
    minute: int,
) -> Candle:
    return Candle(
        instrument=Equity.of("NSE", "RELIANCE"),
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=Decimal(open)),
            high=Price(value=Decimal(high)),
            low=Price(value=Decimal(low)),
            close=Price(value=Decimal(close)),
        ),
        volume=Quantity(value=Decimal(volume)),
        timestamp=datetime(2026, 9, 29, 9, minute, tzinfo=UTC),
    )


def test_aggregate_candles_open_first_close_last():
    candles = [
        _candle(open="100", high="105", low="99", close="103", volume="1000", minute=15),
        _candle(open="103", high="108", low="101", close="107", volume="2000", minute=16),
        _candle(open="107", high="110", low="104", close="106", volume="1500", minute=17),
    ]
    agg = aggregate_candles(candles)
    assert agg.ohlc.open.value == Decimal("100")
    assert agg.ohlc.close.value == Decimal("106")


def test_aggregate_candles_high_max_low_min():
    candles = [
        _candle(open="100", high="105", low="99", close="103", volume="1000", minute=15),
        _candle(open="103", high="108", low="101", close="107", volume="2000", minute=16),
        _candle(open="107", high="110", low="95", close="106", volume="1500", minute=17),
    ]
    agg = aggregate_candles(candles)
    assert agg.ohlc.high.value == Decimal("110")
    assert agg.ohlc.low.value == Decimal("95")


def test_aggregate_candles_volume_sum():
    candles = [
        _candle(open="100", high="105", low="99", close="103", volume="1000", minute=15),
        _candle(open="103", high="108", low="101", close="107", volume="2000", minute=16),
        _candle(open="107", high="110", low="104", close="106", volume="1500", minute=17),
    ]
    agg = aggregate_candles(candles)
    assert agg.volume.value == Decimal("4500")


def test_aggregate_candles_single_candle_identity():
    candle = _candle(open="100", high="105", low="99", close="103", volume="1000", minute=15)
    agg = aggregate_candles([candle])
    assert agg.ohlc.open.value == Decimal("100")
    assert agg.ohlc.high.value == Decimal("105")
    assert agg.ohlc.low.value == Decimal("99")
    assert agg.ohlc.close.value == Decimal("103")
    assert agg.volume.value == Decimal("1000")


def test_aggregate_candles_preserves_instrument_timeframe_timestamp():
    candles = [
        _candle(open="100", high="105", low="99", close="103", volume="1000", minute=15),
        _candle(open="103", high="108", low="101", close="107", volume="2000", minute=16),
    ]
    agg = aggregate_candles(candles)
    assert agg.instrument == candles[0].instrument
    assert agg.timeframe == Timeframe.M1
    assert agg.timestamp == datetime(2026, 9, 29, 9, 16, tzinfo=UTC)


def test_aggregate_candles_empty_raises():
    import pytest
    with pytest.raises(ValueError):
        aggregate_candles([])
