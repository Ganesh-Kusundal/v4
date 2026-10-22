"""Tests for the datalake sync write path — SyncService + series_to_frame."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
from tradex_domain import OHLC, Candle, Equity, Timeframe
from tradex_domain.market import HistoricalSeries
from tradex_domain.market_calendar import IST
from tradex_domain.value_objects import Price, Quantity
from tradex_market_data.sync_service import (
    SyncConfig,
    SyncReport,
    SyncService,
    series_to_frame,
)

INSTRUMENTS = [Equity.of("NSE", f"SYM{i}") for i in range(3)]
#: IST-naive wall clock (the datalake contract). ``_series`` tags it IST and
#: converts to UTC, matching real broker output. Tagging 09:15 as UTC — the
#: pre-560d8a77 fixture — shifts every session 5.5h and fails the IST-naive
#: coverage check (commit 560d8a77 fixed the validator, not this file).
#: A Monday: a Saturday session does not exist, and sync never requests one.
BASE = datetime(2026, 8, 3, 9, 15)
START, END = BASE, BASE + timedelta(minutes=9)


def _series(n: int = 10, instrument=None) -> HistoricalSeries:
    inst = instrument or INSTRUMENTS[0]
    candles = [
        Candle(
            instrument=inst,
            timeframe=Timeframe.M1,
            ohlc=OHLC(
                open=Price(Decimal("100")), high=Price(Decimal("101")),
                low=Price(Decimal("99")), close=Price(Decimal("100")),
            ),
            volume=Quantity(Decimal("1000")),
            timestamp=(BASE + timedelta(minutes=i)).replace(tzinfo=IST).astimezone(UTC),
        )
        for i in range(n)
    ]
    return HistoricalSeries(
        instrument=inst, timeframe=Timeframe.M1, candles=candles,
        start=BASE, end=BASE + timedelta(minutes=n - 1),
    )


def _patched_fetcher(results: dict, errors: list) -> MagicMock:
    fetcher = MagicMock()
    fetcher.fetch.return_value = (results, errors)
    return fetcher


def _store(rows_per_call: int = 10) -> MagicMock:
    store = MagicMock()
    store.upsert.return_value = rows_per_call
    return store


class TestSyncService:
    """Sync path tests — SyncService is the one write path (simple_sync deleted)."""

    def _run(self, results: dict, errors: list, instruments=None, ranges=None):
        insts = INSTRUMENTS if instruments is None else instruments
        store = _store()
        with patch(
            "tradex_market_data.sync_service.ParallelHistoryFetcher"
        ) as phf, patch(
            "tradex_market_data.sync_service.fetch_with_backoff",
            return_value=results,
        ) as fwb:
            phf.return_value = _patched_fetcher(results, errors)
            svc = SyncService(MagicMock(), store, SyncConfig(timeframe="1m"))
            report = svc.sync(insts, START, END, ranges=ranges)
        return report, phf, fwb, store

    def test_empty_universe_requests_nothing(self) -> None:
        store = _store()
        svc = SyncService(MagicMock(), store, SyncConfig())
        report = svc.sync([], START, END)
        assert report == SyncReport(0, 0, 0)
        store.upsert.assert_not_called()

    def test_syncs_all_symbols(self) -> None:
        results = {str(i.instrument_id): _series(instrument=i) for i in INSTRUMENTS}
        report, phf, _, _ = self._run(results, [])
        assert report.fetched == 3
        assert report.failed == {}
        assert report.skipped == []
        brokers = phf.call_args.args[0]
        assert list(brokers.keys()) == ["dhan"]

    def test_uses_parallel_history_fetcher_max_workers(self) -> None:
        results = {str(i.instrument_id): _series(instrument=i) for i in INSTRUMENTS}
        _, phf, _, _ = self._run(results, [])
        assert phf.call_args.kwargs["max_workers"] == 2  # SyncConfig default

    def test_ranges_passed_to_fetcher(self) -> None:
        results = {str(INSTRUMENTS[0].instrument_id): _series(instrument=INSTRUMENTS[0])}
        ranges = {str(INSTRUMENTS[0].instrument_id): [(START, END)]}
        report, _, fwb, _ = self._run(results, [], instruments=[INSTRUMENTS[0]], ranges=ranges)
        assert report.fetched == 1
        _, kw = fwb.call_args
        assert kw.get("ranges") == ranges

    def test_empty_answer_is_a_failure_not_a_silent_skip(self) -> None:
        """An empty series usually means the symbol resolved to the wrong
        security. Skipping it would let the run exit 0 over a missing symbol."""
        report, _, _, _ = self._run({}, [], instruments=[INSTRUMENTS[0]])
        assert report.failed == {INSTRUMENTS[0].symbol: "no_data"}
        assert report.skipped == []

    def test_present_but_empty_series_is_a_failure(self) -> None:
        results = {str(INSTRUMENTS[0].instrument_id): _series(n=0, instrument=INSTRUMENTS[0])}
        report, _, _, _ = self._run(results, [], instruments=[INSTRUMENTS[0]])
        assert report.failed == {INSTRUMENTS[0].symbol: "no_data"}
        assert report.skipped == []


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
        ist = timezone(timedelta(hours=5, minutes=30))
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
