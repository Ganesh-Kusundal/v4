"""Tests for enhanced CorporateActionStore — typed actions and adjust_series."""

from __future__ import annotations

import pytest

from tradex_trading.datalake.corporate_actions import CorporateAction, CorporateActionStore


class MockCandle:
    def __init__(self, o: float, h: float, lo: float, c: float) -> None:
        self.open = o
        self.high = h
        self.low = lo
        self.close = c


class TestAddTypedAndGet:
    def test_add_typed_and_get(self):
        store = CorporateActionStore()
        action = CorporateAction(
            instrument="NSE_RELIANCE",
            action_type="DIVIDEND",
            ex_date="2024-06-15",
            amount=10.0,
        )
        store.add_typed(action)
        result = store.get_typed("NSE_RELIANCE")
        assert len(result) == 1
        assert result[0].instrument == "NSE_RELIANCE"
        assert result[0].action_type == "DIVIDEND"
        assert result[0].amount == 10.0


class TestGetTypedFilterByType:
    def test_get_typed_filter_by_type(self):
        store = CorporateActionStore()
        store.add_typed(CorporateAction("NSE_TCS", "DIVIDEND", "2024-06-01", amount=5.0))
        store.add_typed(CorporateAction("NSE_TCS", "SPLIT", "2024-07-01", ratio=2.0))
        store.add_typed(CorporateAction("NSE_TCS", "DIVIDEND", "2024-09-01", amount=3.0))

        dividends = store.get_typed("NSE_TCS", action_type="DIVIDEND")
        assert len(dividends) == 2
        assert all(a.action_type == "DIVIDEND" for a in dividends)

        splits = store.get_typed("NSE_TCS", action_type="SPLIT")
        assert len(splits) == 1
        assert splits[0].ratio == 2.0


class TestAdjustSeriesSingleSplit:
    def test_adjust_series_single_split(self):
        store = CorporateActionStore()
        store.add_typed(CorporateAction("NSE_REL", "SPLIT", "2024-06-01", ratio=2.0))
        candles = [MockCandle(100.0, 110.0, 90.0, 105.0)]
        adjusted = store.adjust_series(candles, "NSE_REL")
        assert len(adjusted) == 1
        assert adjusted[0]["open"] == pytest.approx(50.0)
        assert adjusted[0]["high"] == pytest.approx(55.0)
        assert adjusted[0]["low"] == pytest.approx(45.0)
        assert adjusted[0]["close"] == pytest.approx(52.5)


class TestAdjustSeriesMultipleSplits:
    def test_adjust_series_multiple_splits(self):
        store = CorporateActionStore()
        store.add_typed(CorporateAction("NSE_REL", "SPLIT", "2024-03-01", ratio=2.0))
        store.add_typed(CorporateAction("NSE_REL", "SPLIT", "2024-09-01", ratio=5.0))
        # cumulative ratio = 2.0 * 5.0 = 10.0
        candles = [MockCandle(1000.0, 1100.0, 900.0, 1050.0)]
        adjusted = store.adjust_series(candles, "NSE_REL")
        assert adjusted[0]["open"] == pytest.approx(100.0)
        assert adjusted[0]["high"] == pytest.approx(110.0)
        assert adjusted[0]["low"] == pytest.approx(90.0)
        assert adjusted[0]["close"] == pytest.approx(105.0)


class TestAdjustSeriesNoSplits:
    def test_adjust_series_no_splits(self):
        store = CorporateActionStore()
        candles = [MockCandle(100.0, 110.0, 90.0, 105.0)]
        adjusted = store.adjust_series(candles, "NSE_UNKNOWN")
        assert adjusted[0]["open"] == 100.0
        assert adjusted[0]["close"] == 105.0


class TestCorporateActionFrozen:
    def test_corporate_action_frozen(self):
        action = CorporateAction("NSE_TCS", "DIVIDEND", "2024-06-01")
        with pytest.raises(AttributeError):
            action.instrument = "NSE_REL"  # type: ignore[misc]
