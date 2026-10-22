"""SyncService — bounded, coverage-validated datalake sync."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
from tradex_domain import OHLC, Candle, Equity, HistoricalSeries, Price, Quantity, Timeframe
from tradex_domain.market_calendar import IST, to_ist_naive
from tradex_market_data.sync_service import (
    SyncConfig,
    SyncService,
    series_to_frame,
    servable_bounds,
)

START = datetime(2026, 8, 3, 9, 15)
END = datetime(2026, 8, 3, 15, 30)


def _eq(symbol: str) -> Equity:
    return Equity.of("NSE", symbol)


def _candle(inst, ts: datetime) -> Candle:
    # Real broker output: tz-aware UTC at the correct instant for the IST
    # wall-clock timestamp (provider_common.parse_timestamp pins naive strings
    # to UTC). Tagging the IST wall clock as UTC — the old fixture's bug —
    # shifts every session 5.5h and fails coverage validation.
    return Candle(
        instrument=inst,
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=Decimal("100")),
            high=Price(value=Decimal("101")),
            low=Price(value=Decimal("99")),
            close=Price(value=Decimal("100")),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts.replace(tzinfo=IST).astimezone(UTC),
    )


def _full_series(inst, start: datetime, end: datetime) -> HistoricalSeries:
    stamps, t = [], start
    while t <= end:
        stamps.append(t)
        t += timedelta(minutes=1)
    return HistoricalSeries(
        instrument=inst,
        timeframe=Timeframe.M1,
        candles=[_candle(inst, s) for s in stamps],
        start=start,
        end=end,
    )


class _Store:
    def __init__(self) -> None:
        self.rows = 0

    def upsert(self, df: pd.DataFrame) -> int:
        self.rows += len(df)
        return len(df)

    def read(self, **kw) -> pd.DataFrame:
        return pd.DataFrame()

    def date_range(self, symbol):
        return None


class _MemStore:
    """In-memory store: upsert records frames; read returns them filtered.

    ``_Store`` always reads empty, so converge could never observe the
    coverage its own sync pass wrote and would loop to the pass cap. This
    stand-in closes that loop: the second scan sees the written candles.
    """

    def __init__(self) -> None:
        self.frames: list[pd.DataFrame] = []

    def upsert(self, df: pd.DataFrame) -> int:
        self.frames.append(df)
        return len(df)

    def read(self, *, symbols, start, end, timeframe) -> pd.DataFrame:
        if not self.frames:
            return pd.DataFrame()
        df = pd.concat(self.frames, ignore_index=True)
        df = df[df["symbol"].isin(symbols)]
        ts = pd.to_datetime(df["timestamp"])
        return df[(ts >= pd.Timestamp(start)) & (ts <= pd.Timestamp(end))]

    def date_range(self, symbol):
        return None


class FakeBroker:
    """Programmable broker: rate-limit N times, truncate, sleep, or serve."""

    def __init__(self, *, fail_first=0, partial=False, delay_s=0.0) -> None:
        self._fail_first = fail_first
        self._partial = partial
        self._delay_s = delay_s
        self._calls: dict[str, int] = {}
        self.rate_limiter = None

    def history(self, inst, timeframe, start, end):
        sym = inst.symbol
        self._calls[sym] = self._calls.get(sym, 0) + 1
        if self._delay_s:
            time.sleep(self._delay_s)
        if self._calls[sym] <= self._fail_first:
            raise RuntimeError("429 rate limited")
        if self._partial:
            half = start + (end - start) / 2
            return _full_series(inst, start, half)
        return _full_series(inst, start, end)


def _service(broker, *, budget_s=30.0, backoff_s=0.0, validate=True) -> SyncService:
    return SyncService(
        broker,
        _Store(),
        SyncConfig(
            timeframe="1m",
            budget_seconds=budget_s,
            initial_backoff_s=backoff_s,
            validate_coverage=validate,
        ),
    )


def test_sync_fetches_and_writes_normal() -> None:
    svc = _service(FakeBroker())
    report = svc.sync([_eq("X")], START, END)
    assert report.fetched == 1
    assert report.written > 0
    assert report.failed == {}


def test_sync_retries_rate_limit_then_succeeds() -> None:
    svc = _service(FakeBroker(fail_first=1), backoff_s=0.0)
    report = svc.sync([_eq("X"), _eq("Y")], START, END)
    assert report.failed == {}
    assert report.fetched == 2


def test_sync_respects_budget() -> None:
    svc = _service(FakeBroker(delay_s=5.0), budget_s=1.0)
    t0 = time.monotonic()
    report = svc.sync([_eq("X"), _eq("Y")], START, END)
    assert time.monotonic() - t0 < 3.0  # never hangs
    assert report.resumable is True


def test_sync_detects_partial_response() -> None:
    svc = _service(FakeBroker(partial=True))
    report = svc.sync([_eq("X")], START, END)
    assert "X" in report.failed
    assert report.failed["X"] == "partial_response"
    assert report.written == 0


def test_converge_terminates_when_clean() -> None:
    svc = SyncService(FakeBroker(), _MemStore(), SyncConfig(timeframe="1m"))
    report = svc.converge([_eq("X")], START, END)
    assert report.converged is True


def test_converge_stops_at_budget() -> None:
    svc = _service(FakeBroker(delay_s=5.0), budget_s=1.0)
    t0 = time.monotonic()
    report = svc.converge([_eq("X"), _eq("Y")], START, END)
    assert time.monotonic() - t0 < 4.0
    assert report.converged is False
    assert report.resumable is True


def _truncated_series(inst, start, end, *, drop_minutes):
    """A full session's series with its tail cut off, as a venue serves it."""
    full = _full_series(inst, start, end)
    cutoff = end - timedelta(minutes=drop_minutes)
    return HistoricalSeries(
        instrument=inst,
        timeframe=Timeframe.M1,
        candles=[c for c in full.candles if to_ist_naive(c.timestamp) <= cutoff],
        start=start,
        end=end,
    )


class _FixedSeriesBroker:
    """Returns one canned series whatever it is asked for."""

    def __init__(self, series) -> None:
        self._series = series
        self.rate_limiter = None

    def history(self, inst, timeframe, start, end):
        return self._series


def test_sync_accepts_a_series_truncated_by_the_venue_ceiling() -> None:
    """Dhan's 1m series stops at 15:14, so a window ending at the close can
    never be covered to its last minute. Rejecting it would mark every symbol
    partial_response and write nothing — the regression simple_sync avoids."""
    inst = _eq("X")
    broker = _FixedSeriesBroker(_truncated_series(inst, START, END, drop_minutes=16))
    report = _service(broker).sync([inst], START, END)
    assert report.failed == {}
    assert report.fetched == 1
    assert report.written > 0


def test_sync_still_rejects_a_series_short_of_the_window() -> None:
    """Negative control: a response stopping well before the window is partial."""
    inst = _eq("X")
    broker = _FixedSeriesBroker(_truncated_series(inst, START, END, drop_minutes=120))
    report = _service(broker).sync([inst], START, END)
    assert report.failed.get("X") == "partial_response"
    assert report.written == 0


class _RecordingBroker(FakeBroker):
    """FakeBroker that remembers the exact windows it was asked for."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.windows: list[tuple[datetime, datetime]] = []

    def history(self, inst, timeframe, start, end):
        self.windows.append((start, end))
        return super().history(inst, timeframe, start, end)


class _NoHistoryBroker:
    """A broker that advertises it cannot serve history (e.g. PaperBroker)."""

    supports_history = False

    def __init__(self) -> None:
        self.calls = 0
        self.rate_limiter = None

    def history(self, inst, timeframe, start, end):
        self.calls += 1
        raise RuntimeError("paper broker has no historical data source")


def test_sync_skips_a_broker_without_history_instead_of_retrying() -> None:
    """The broker's own supports_history guard must short-circuit the batch:
    calling history() at all here is the bug (it retries forever otherwise)."""
    broker = _NoHistoryBroker()
    svc = _service(broker, budget_s=30.0)
    t0 = time.monotonic()
    report = svc.sync([_eq("X"), _eq("Y")], START, END)
    assert time.monotonic() - t0 < 2.0
    assert broker.calls == 0
    assert sorted(report.skipped) == ["X", "Y"]
    assert report.failed == {}


def test_sync_range_scoped_fetch_asks_only_for_the_ranges() -> None:
    """A range-scoped sync must not widen the request to the whole window —
    asking Dhan for a full session to fill a tail hole returns the wrong bars."""
    broker = _RecordingBroker()
    svc = _service(broker)
    inst = _eq("X")
    hole = [(datetime(2026, 8, 3, 10, 0), datetime(2026, 8, 3, 10, 5))]
    report = svc.sync([inst], START, END, ranges={str(inst.instrument_id): hole})
    assert report.failed == {}
    assert report.fetched == 1
    assert report.written > 0
    assert broker.windows == [(datetime(2026, 8, 3, 10, 0), datetime(2026, 8, 3, 10, 5))]


def test_scan_honours_holidays() -> None:
    """A holiday is not an expected session, so a symbol with no bars that day
    is complete, not gapped. The scan window spans three weekdays and the
    store holds the first and third — the middle one is the holiday."""
    from tradex_market_data.sync_service import series_to_frame

    inst = _eq("X")
    aug5 = datetime(2026, 8, 5, 9, 15)
    end = datetime(2026, 8, 5, 15, 30)
    holiday = datetime(2026, 8, 4).date()  # Tue, between the two stored days

    store = _MemStore()
    store.frames.append(series_to_frame(_full_series(inst, START, END), inst.symbol))
    store.frames.append(
        series_to_frame(_full_series(inst, aug5, end), inst.symbol)
    )
    svc = SyncService(FakeBroker(), store, SyncConfig(timeframe="1m"))

    assert svc.scan([inst], START, end, holidays={holiday}).gaps == []
    # control: without the exclusion the holiday IS an expected session
    assert len(svc.scan([inst], START, end).gaps) == 1


def test_converge_does_not_refetch_a_deterministic_failure() -> None:
    """A truncated response reproduces on every pass, so re-fetching it only
    burns the budget. converge must try each symbol once and stop."""
    broker = FakeBroker(partial=True)
    svc = _service(broker, budget_s=30.0, backoff_s=0.0)
    report = svc.converge([_eq("X")], START, END)
    assert report.converged is False
    assert report.resumable is True
    assert report.failed.get("X") == "partial_response"
    assert broker._calls["X"] == 1  # not 10 passes of the same partial


def _frame_missing_interior(inst, start, end, drop_from, minutes):
    """A full session's frame with a short interior run removed."""
    from tradex_market_data.sync_service import series_to_frame

    df = series_to_frame(_full_series(inst, start, end), inst.symbol)
    ts = pd.to_datetime(df["timestamp"])
    drop = (ts >= pd.Timestamp(drop_from)) & (
        ts < pd.Timestamp(drop_from) + pd.Timedelta(minutes=minutes)
    )
    return df[~drop]


def test_converge_does_not_report_clean_over_a_floored_hole() -> None:
    """A real hole under min_gap_stamps is not a clean lake — converge must say
    so instead of returning converged=True (the blind spot CLAUDE.md records)."""
    inst = _eq("X")
    store = _MemStore()
    store.frames.append(
        _frame_missing_interior(inst, START, END, datetime(2026, 8, 3, 10, 0), 5)
    )
    svc = SyncService(
        FakeBroker(), store, SyncConfig(timeframe="1m", min_gap_stamps=15)
    )
    report = svc.converge([inst], START, END)
    assert report.converged is False
    assert report.under_floored == ("X",)
    assert report.resumable is False  # a rerun at this floor changes nothing


def test_coverage_accepts_utc_candles_at_ist_instants() -> None:
    """A broker returning tz-aware UTC candles at the correct instants for a
    09:15-15:30 IST window must pass coverage — not be flagged partial_response.
    """
    inst = _eq("X")
    series = _full_series(inst, START, END)
    first_utc = series.candles[0].timestamp
    last_utc = series.candles[-1].timestamp
    assert first_utc.tzinfo is not None
    assert to_ist_naive(first_utc) == START
    assert to_ist_naive(last_utc) == END
    svc = _service(FakeBroker())
    assert svc._covers(series, [(START, datetime(2026, 8, 3, 15, 14))], set()) is True
    report = svc.sync([inst], START, END)
    assert report.failed == {}
    assert report.fetched == 1
    assert report.written > 0


def test_converge_does_not_chase_the_dhan_tail() -> None:
    """A session that ends at 15:14 is complete. 15:15–15:29 is the venue ceiling."""
    inst = _eq("X")
    store = _MemStore()
    store.frames.append(series_to_frame(
        _truncated_series(inst, START, END, drop_minutes=16), inst.symbol,
    ))
    svc = SyncService(FakeBroker(), store, SyncConfig(timeframe="1m", min_gap_stamps=1))
    report = svc.converge([inst], START, END)
    assert report.converged is True
    assert report.fetched == 0


def test_sync_writes_only_the_requested_minutes() -> None:
    """A broker that returns the whole session must not overwrite the rest of the day."""
    inst = _eq("X")
    hole = [(datetime(2026, 8, 3, 10, 0), datetime(2026, 8, 3, 10, 5))]

    class _Capture(_Store):
        def __init__(self) -> None:
            super().__init__()
            self.frames: list[pd.DataFrame] = []

        def upsert(self, df: pd.DataFrame) -> int:
            self.frames.append(df)
            return super().upsert(df)

    store = _Capture()
    svc = SyncService(
        _FixedSeriesBroker(_full_series(inst, START, END)),
        store,
        SyncConfig(timeframe="1m", budget_seconds=30, initial_backoff_s=0),
    )
    report = svc.sync([inst], START, END, ranges={str(inst.instrument_id): hole})
    assert report.failed == {}
    assert report.written == 6
    ts = pd.to_datetime(store.frames[0]["timestamp"])
    assert ts.min() == pd.Timestamp("2026-08-03 10:00")
    assert ts.max() == pd.Timestamp("2026-08-03 10:05")


def test_sync_rejects_a_hole_filled_only_by_a_phantom() -> None:
    """A 16:00 bar does not cover a missing 10:03, and nothing is written."""
    inst = _eq("X")
    hole = [(datetime(2026, 8, 3, 10, 0), datetime(2026, 8, 3, 10, 5))]
    full = _full_series(inst, hole[0][0], hole[0][1])
    candles = [
        c for c in full.candles
        if to_ist_naive(c.timestamp) != datetime(2026, 8, 3, 10, 3)
    ]
    candles.append(_candle(inst, datetime(2026, 8, 3, 16, 0)))
    series = HistoricalSeries(
        instrument=inst, timeframe=Timeframe.M1, candles=candles,
        start=hole[0][0], end=hole[0][1],
    )
    report = _service(_FixedSeriesBroker(series)).sync(
        [inst], START, END, ranges={str(inst.instrument_id): hole},
    )
    assert report.failed.get("X") == "partial_response"
    assert report.written == 0


def test_a_covered_hole_is_written_when_its_sibling_is_short() -> None:
    """One missing minute must not discard a hole that came back complete."""
    inst = _eq("X")
    morning = (datetime(2026, 8, 3, 10, 0), datetime(2026, 8, 3, 10, 5))
    later = (datetime(2026, 8, 3, 11, 0), datetime(2026, 8, 3, 11, 5))

    class _MorningOnly:
        rate_limiter = None

        def history(self, inst, timeframe, start, end):
            if start.hour == 10:
                return _full_series(inst, start, end)
            return _full_series(inst, start, start)

    report = _service(_MorningOnly()).sync(
        [inst], START, END,
        ranges={str(inst.instrument_id): [morning, later]},
        now=datetime(2026, 8, 4, 10, 0),
    )
    assert report.failed.get("X") == "partial_response"
    assert report.written == 6


def test_scan_stops_before_the_minute_that_is_still_forming() -> None:
    """At 10:00:30 the 10:00 bar has not closed. 09:59 is the last required stamp."""
    inst = _eq("X")
    now = datetime(2026, 8, 3, 10, 0, 30)
    store = _MemStore()
    store.frames.append(series_to_frame(
        _full_series(inst, START, datetime(2026, 8, 3, 9, 59)), inst.symbol,
    ))
    svc = SyncService(FakeBroker(), store, SyncConfig(timeframe="1m"))
    assert svc.scan([inst], START, now, now=now).gaps == []


def test_a_date_only_end_includes_that_session() -> None:
    """`--end 2026-08-05` is midnight. That session is in the window."""
    now = datetime(2026, 8, 6, 10, 0)
    bounds = servable_bounds(datetime(2026, 8, 5), datetime(2026, 8, 5), now, set())
    assert bounds == (
        datetime(2026, 8, 5, 9, 15),
        datetime(2026, 8, 5, 15, 14),
    )


def test_a_second_sync_on_the_same_lake_is_refused(tmp_path) -> None:
    """Two syncs would exceed the shared Dhan rate and race on partitions."""
    from tradex_market_data.sync_service import SyncLocked, sync_lock

    with sync_lock(tmp_path):
        try:
            with sync_lock(tmp_path):
                raise AssertionError("second sync acquired the lock")
        except SyncLocked:
            pass
    with sync_lock(tmp_path):  # released on exit
        pass


def test_repair_order_is_partial_day_then_whole_session_then_open() -> None:
    from tradex_market_data.sync_service import _priority

    day = datetime(2026, 8, 3)
    hole = [(day.replace(hour=10, minute=0), day.replace(hour=10, minute=20))]
    session = [(day.replace(hour=9, minute=16), day.replace(hour=15, minute=14))]
    opens = [(day.replace(hour=9, minute=15), day.replace(hour=9, minute=15))]
    ordered = sorted([opens, session, hole], key=_priority)
    assert ordered == [hole, session, opens]
