"""Tests for simple_sync — the one datalake fill path.

Candidate 2 of the 2026-09-17 architecture review asked for the dual sync
paths to be unified. These tests pin the contracts that unification had to
preserve, and the one it improved (failover).
"""

from __future__ import annotations

import pandas as pd
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from tradex_domain import OHLC, Candle, Equity, Timeframe
from tradex_domain.market import HistoricalSeries
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.datalake.simple_sync import series_to_frame, simple_sync

INSTRUMENTS = [Equity.of("NSE", f"SYM{i}") for i in range(3)]
BASE = datetime(2026, 8, 1, 9, 15, tzinfo=UTC)
START, END = datetime(2026, 8, 1), datetime(2026, 8, 2)


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
            timestamp=BASE + timedelta(minutes=i),
        )
        for i in range(n)
    ]
    return HistoricalSeries(
        instrument=inst, timeframe=Timeframe.M1, candles=candles,
        start=BASE, end=BASE + timedelta(minutes=n - 1),
    )


def _patched_fetcher(results: dict, errors: list) -> MagicMock:
    """A ParallelHistoryFetcher stand-in returning fixed (results, errors)."""
    fetcher = MagicMock()
    fetcher.fetch.return_value = (results, errors)
    return fetcher


def _store(rows_per_call: int = 10) -> MagicMock:
    store = MagicMock()
    store.upsert.return_value = rows_per_call
    return store


class TestSimpleSync:
    """simple_sync — fetch, convert, store."""

    def test_empty_universe_requests_nothing(self) -> None:
        """No instruments is a trivial success, not an error."""
        store = _store()
        result = simple_sync(MagicMock(), store, [], "1m", START, END)
        assert result.requested == 0
        assert result.fetched == 0
        assert result.written == 0
        assert result.failed == []
        store.upsert.assert_not_called()

    def test_syncs_all_symbols(self) -> None:
        """Every instrument with a series is fetched and written."""
        results = {str(i.instrument_id): _series(instrument=i) for i in INSTRUMENTS}
        with patch(
            "tradex_trading.datalake.simple_sync.ParallelHistoryFetcher"
        ) as phf:
            phf.return_value = _patched_fetcher(results, [])
            result = simple_sync(MagicMock(), _store(10), INSTRUMENTS, "1m", START, END)
        assert result.requested == 3
        assert result.fetched == 3
        assert result.failed == []

    def test_uses_parallel_history_fetcher(self) -> None:
        """The fetch must go through ParallelHistoryFetcher, not a duplicate."""
        results = {str(i.instrument_id): _series(instrument=i) for i in INSTRUMENTS}
        with patch(
            "tradex_trading.datalake.simple_sync.ParallelHistoryFetcher"
        ) as phf:
            phf.return_value = _patched_fetcher(results, [])
            simple_sync(MagicMock(), _store(10), INSTRUMENTS, "1m", START, END)
        phf.assert_called_once()
        # max_workers reaches the fetcher constructor.
        assert phf.call_args.kwargs["max_workers"] == 5

    def test_failover_brokers_reach_the_fetcher(self) -> None:
        """The other brokers are handed to the fetcher, not discarded."""
        results = {str(i.instrument_id): _series(instrument=i) for i in INSTRUMENTS}
        extra = {"upstox": MagicMock()}
        with patch(
            "tradex_trading.datalake.simple_sync.ParallelHistoryFetcher"
        ) as phf:
            phf.return_value = _patched_fetcher(results, [])
            simple_sync(
                MagicMock(), _store(10), INSTRUMENTS, "1m", START, END,
                failover_brokers=extra,
            )
        brokers = phf.call_args.args[0]
        assert "primary" in brokers
        assert brokers["upstox"] is extra["upstox"]

    def test_no_data_is_skipped_not_failed(self) -> None:
        """An IPO with no bars is skipped, not counted as a failure.

        This is the contract the unification had to keep: the fetcher reports
        an empty series as an *error*, but this path downgrades it to a skip.
        """
        # The fetcher returns no entry for an IPO, and reports it as an error.
        empty = {}
        empty_err = [
            f"{INSTRUMENTS[0].instrument_id}: all brokers failed "
            f"(primary: empty stitched series for {INSTRUMENTS[0].instrument_id})"
        ]
        with patch(
            "tradex_trading.datalake.simple_sync.ParallelHistoryFetcher"
        ) as phf:
            phf.return_value = _patched_fetcher(empty, empty_err)
            result = simple_sync(MagicMock(), _store(10), INSTRUMENTS, "1m", START, END)
        assert result.requested == 3
        assert result.failed == []
        assert result.fetched == 0

    def test_transient_failure_lands_in_failed(self) -> None:
        """A real 429-style failure is not downgraded to a skip."""
        err = [f"{INSTRUMENTS[0].instrument_id}: all brokers failed (429 rate limit)"]
        with patch(
            "tradex_trading.datalake.simple_sync.ParallelHistoryFetcher"
        ) as phf:
            phf.return_value = _patched_fetcher({}, err)
            result = simple_sync(MagicMock(), _store(10), INSTRUMENTS, "1m", START, END)
        assert INSTRUMENTS[0].symbol in result.failed

    def test_gaps_short_circuit_before_fetching(self) -> None:
        """A complete lake (detector returns []) short-circuits before fetching."""
        gaps = MagicMock()
        gaps.detect.return_value = []
        store = _store()
        with patch(
            "tradex_trading.datalake.simple_sync.ParallelHistoryFetcher"
        ) as phf:
            result = simple_sync(
                MagicMock(), store, INSTRUMENTS, "1m", START, END, gaps=gaps,
            )
        assert result.requested == 3
        assert result.fetched == 0
        phf.assert_not_called()
        store.upsert.assert_not_called()

    def test_no_gaps_detector_fetches_everything(self) -> None:
        """gaps=None means a full fetch — no detector, no skip logic."""
        results = {str(i.instrument_id): _series(instrument=i) for i in INSTRUMENTS}
        with patch(
            "tradex_trading.datalake.simple_sync.ParallelHistoryFetcher"
        ) as phf:
            phf.return_value = _patched_fetcher(results, [])
            result = simple_sync(MagicMock(), _store(10), INSTRUMENTS, "1m", START, END)
        assert result.fetched == 3

    def test_present_but_empty_series_is_skipped(self) -> None:
        """A present-but-empty series is an IPO skip, never a failure."""
        results = {
            str(INSTRUMENTS[0].instrument_id): _series(n=0, instrument=INSTRUMENTS[0])
        }
        with patch(
            "tradex_trading.datalake.simple_sync.ParallelHistoryFetcher"
        ) as phf:
            phf.return_value = _patched_fetcher(results, [])
            result = simple_sync(MagicMock(), _store(10), INSTRUMENTS, "1m", START, END)
        assert result.failed == []

    def test_partial_gaps_fetch_only_ranged_symbols(self) -> None:
        """Detector hits pass both the symbol filter and the ranges through."""
        results = {str(INSTRUMENTS[0].instrument_id): _series(instrument=INSTRUMENTS[0])}
        gaps = MagicMock()
        gaps.detect.return_value = [(INSTRUMENTS[0], [(START, END)])]
        with patch(
            "tradex_trading.datalake.simple_sync.ParallelHistoryFetcher"
        ) as phf:
            phf.return_value = _patched_fetcher(results, [])
            result = simple_sync(
                MagicMock(), _store(10), INSTRUMENTS, "1m", START, END, gaps=gaps,
            )
        assert result.fetched == 1
        _, kw = phf.return_value.fetch.call_args
        assert kw.get("ranges") == {str(INSTRUMENTS[0].instrument_id): [(START, END)]}


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
