"""SyncService — bounded, coverage-validated datalake sync."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
from tradex_domain import OHLC, Candle, Equity, HistoricalSeries, Price, Quantity, Timeframe
from tradex_domain.market_calendar import IST, to_ist_naive
from tradex_market_data.sync_service import SyncConfig, SyncService

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
    assert svc._validate_coverage(series, START, END) is True
    report = svc.sync([inst], START, END)
    assert report.failed == {}
    assert report.fetched == 1
    assert report.written > 0
