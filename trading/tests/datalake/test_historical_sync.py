"""Tests for SyncOrchestrator facade — detect → fetch → upsert."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pandas as pd
from tradex_domain import OHLC, Candle, Equity, Timeframe
from tradex_domain.market import HistoricalSeries
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.datalake.historical_sync import (
    SyncOrchestrator,
    series_to_frame,
)

INSTRUMENTS = [Equity.of("NSE", f"SYM{i}") for i in range(5)]
BASE = datetime(2026, 8, 1, 9, 15, tzinfo=UTC)


def _series(n=10, instrument=None) -> HistoricalSeries:
    inst = instrument or INSTRUMENTS[0]
    candles = [
        Candle(
            instrument=inst,
            timeframe=Timeframe.M1,
            ohlc=OHLC(open=Price(Decimal("100")), high=Price(Decimal("101")),
                    low=Price(Decimal("99")), close=Price(Decimal("100"))),
            volume=Quantity(Decimal("1000")),
            timestamp=BASE + timedelta(minutes=i),
        )
        for i in range(n)
    ]
    return HistoricalSeries(
        instrument=inst, timeframe=Timeframe.M1,
        candles=candles, start=BASE, end=BASE + timedelta(minutes=n - 1),
    )


def _mock_fetcher(series_fn=None) -> MagicMock:
    f = MagicMock()
    def _fetch(instruments, tf, start, end, ranges=None):
        results = {}
        for inst in instruments:
            s = series_fn(inst) if series_fn else _series(instrument=inst)
            if s is not None:
                results[str(inst.instrument_id)] = s
        return results, []
    f.fetch.side_effect = _fetch
    return f


def _mock_gaps(hits=None) -> MagicMock:
    g = MagicMock()
    # hits=None → all instruments gapped (full fetch); [] → all complete
    g.detect.side_effect = lambda instruments, **kw: (
        [(i, [(kw.get("start"), kw.get("end"))]) for i in instruments]
        if hits is None else hits
    )
    return g


def _mock_store(total_rows: int = 10) -> MagicMock:
    store = MagicMock()
    store.upsert = MagicMock(return_value=total_rows)
    return store


class TestSeriesToFrame:
    def test_converts_candles_to_storage_columns(self):
        df = series_to_frame(_series(n=3), "SYM0")
        assert list(df.columns) == [
            "symbol", "exchange", "kind", "timeframe",
            "timestamp", "open", "high", "low", "close", "volume",
        ]
        assert len(df) == 3
        assert df["symbol"].eq("SYM0").all()
        assert df["timestamp"].dt.tz is None

    def test_empty_series_gives_empty_frame(self):
        empty = HistoricalSeries(
            instrument=INSTRUMENTS[0], timeframe=Timeframe.M1,
            candles=[], start=BASE, end=BASE,
        )
        assert series_to_frame(empty, "SYM0").empty

    def test_aware_non_utc_converts_not_relabels(self):
        # 14:45 IST aware == 09:15 UTC; old replace() bug shifted it +5:30
        from datetime import timezone, timedelta as td
        ist = timezone(td(hours=5, minutes=30))
        src = _series(n=1).candles[0]
        candle = Candle(
            instrument=src.instrument, timeframe=src.timeframe,
            ohlc=src.ohlc, volume=src.volume,
            timestamp=datetime(2026, 8, 3, 14, 45, tzinfo=ist),
        )
        df = series_to_frame(
            HistoricalSeries(instrument=INSTRUMENTS[0], timeframe=Timeframe.M1,
                             candles=[candle], start=BASE, end=BASE), "SYM0")
        assert df["timestamp"].iloc[0] == pd.Timestamp("2026-08-03 14:45:00")


class TestSyncFacade:
    def test_syncs_all_symbols(self):
        svc = SyncOrchestrator(_mock_store(10), _mock_fetcher(), _mock_gaps())
        r = svc.sync(INSTRUMENTS, "1m", BASE, BASE + timedelta(days=1), backoff_base=0)
        assert (r.requested, r.fetched, r.written, r.failed) == (5, 5, 10, [])

    def test_skip_complete_fetches_nothing(self):
        fetcher = _mock_fetcher()
        svc = SyncOrchestrator(_mock_store(), fetcher, _mock_gaps(hits=[]))
        r = svc.sync(INSTRUMENTS, "1m", BASE, BASE + timedelta(days=1), backoff_base=0)
        assert r.fetched == 0 and r.written == 0
        fetcher.fetch.assert_not_called()

    def test_partial_gaps_pass_ranges(self):
        fetcher = _mock_fetcher()
        gaps = MagicMock()
        gaps.detect.return_value = [(INSTRUMENTS[0], [(BASE, BASE + timedelta(hours=1))])]
        svc = SyncOrchestrator(_mock_store(10), fetcher, gaps)
        r = svc.sync(INSTRUMENTS[:2], "1m", BASE, BASE + timedelta(days=1), backoff_base=0)
        assert r.fetched == 1
        _, kw = fetcher.fetch.call_args
        assert list(kw.get("ranges") or {}) == [str(INSTRUMENTS[0].instrument_id)]

    def test_missing_results_land_in_failed(self):
        svc = SyncOrchestrator(_mock_store(), _mock_fetcher(series_fn=lambda i: None), _mock_gaps())
        r = svc.sync(INSTRUMENTS[:2], "1m", BASE, BASE + timedelta(days=1), backoff_base=0)
        assert r.fetched == 0 and len(r.failed) == 2

    def test_empty_universe(self):
        svc = SyncOrchestrator(_mock_store(), _mock_fetcher(), _mock_gaps())
        r = svc.sync([], "1m", BASE, BASE + timedelta(days=1))
        assert r.requested == 0 and r.fetched == 0

    def test_no_gaps_detector_full_fetch(self):
        fetcher = _mock_fetcher()
        svc = SyncOrchestrator(_mock_store(10), fetcher, None)
        r = svc.sync(INSTRUMENTS[:2], "1m", BASE, BASE + timedelta(days=1), backoff_base=0)
        assert r.fetched == 2
