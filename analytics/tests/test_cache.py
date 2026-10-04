"""BarStore + IndicatorCache: canonical series, versioned cache dedup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_analytics.cache import BarStore, IndicatorCache
from tradex_domain import OHLC, Candle, Equity, Price, Quantity, Timeframe


def _candle(symbol: str, i: int, tf: Timeframe = Timeframe.M1) -> Candle:
    ts = datetime(2026, 8, 5, 15, 0, tzinfo=UTC) + timedelta(minutes=i)
    return Candle(
        instrument=Equity.of("NSE", symbol),
        timeframe=tf,
        ohlc=OHLC(
            open=Price(value=Decimal("100")),
            high=Price(value=Decimal("101")),
            low=Price(value=Decimal("99")),
            close=Price(value=Decimal(str(100 + i))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


def test_store_ingest_appends_and_bumps_version() -> None:
    store = BarStore()
    inst = Equity.of("NSE", "RELIANCE").instrument_id
    assert store.version(inst, Timeframe.M1) == 0
    store.ingest(_candle("RELIANCE", 0))
    store.ingest(_candle("RELIANCE", 1))
    assert store.version(inst, Timeframe.M1) == 2
    series = store.series(inst, Timeframe.M1)
    assert series is not None and len(series.candles) == 2


def test_store_separates_instruments_and_timeframes() -> None:
    store = BarStore()
    store.ingest(_candle("RELIANCE", 0))
    store.ingest(_candle("TCS", 0))
    store.ingest(_candle("RELIANCE", 0, Timeframe.M5))
    r = Equity.of("NSE", "RELIANCE").instrument_id
    t = Equity.of("NSE", "TCS").instrument_id
    assert len(store.series(r, Timeframe.M1).candles) == 1
    assert len(store.series(t, Timeframe.M1).candles) == 1
    assert len(store.series(r, Timeframe.M5).candles) == 1


def test_cache_returns_same_object_for_same_version() -> None:
    cache = IndicatorCache()
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"value": [1.0, 2.0]}

    key = ("k",)
    assert cache.get_or_compute(key, 1, compute) == {"value": [1.0, 2.0]}
    assert cache.get_or_compute(key, 1, compute) == {"value": [1.0, 2.0]}
    assert calls["n"] == 1  # computed once, served twice


def test_cache_recomputes_on_version_bump() -> None:
    cache = IndicatorCache()
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"value": [float(calls["n"])]}

    key = ("k",)
    assert cache.get_or_compute(key, 1, compute) == {"value": [1.0]}
    assert cache.get_or_compute(key, 2, compute) == {"value": [2.0]}
    assert calls["n"] == 2
