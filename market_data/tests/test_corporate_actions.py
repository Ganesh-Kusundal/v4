"""Tests for CorporateActionStore.adjust_series on real domain Candles."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import Candle, OHLC
from tradex_domain.value_objects import Price, Quantity
from tradex_market_data.corporate_actions import CorporateAction, CorporateActionStore


def _candle(o: float, h: float, lo: float, c: float) -> Candle:
    return Candle(
        instrument=Equity.of("NSE", "RELIANCE"),
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(Decimal(str(o))),
            high=Price(Decimal(str(h))),
            low=Price(Decimal(str(lo))),
            close=Price(Decimal(str(c))),
        ),
        volume=Quantity(Decimal(1000)),
        timestamp=datetime(2026, 7, 1, 9, 15),
    )


class TestAdjustSeriesRealCandle:
    def test_adjust_series_single_split(self):
        store = CorporateActionStore()
        store.add_typed(CorporateAction("NSE_REL", "SPLIT", "2024-06-01", ratio=2.0))
        candles = [_candle(100.0, 110.0, 90.0, 105.0)]
        adjusted = store.adjust_series(candles, "NSE_REL")
        assert len(adjusted) == 1
        assert adjusted[0]["open"] == pytest.approx(50.0)
        assert adjusted[0]["high"] == pytest.approx(55.0)
        assert adjusted[0]["low"] == pytest.approx(45.0)
        assert adjusted[0]["close"] == pytest.approx(52.5)

    def test_adjust_series_no_splits(self):
        store = CorporateActionStore()
        candles = [_candle(100.0, 110.0, 90.0, 105.0)]
        adjusted = store.adjust_series(candles, "NSE_UNKNOWN")
        assert adjusted[0]["open"] == 100.0
        assert adjusted[0]["close"] == 105.0