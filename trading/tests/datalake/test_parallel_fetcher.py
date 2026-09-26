"""Tests for ParallelHistoryFetcher — single-broker chunked concurrent fetch."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from tradex_domain import OHLC, Candle, Equity, Timeframe
from tradex_domain.market import HistoricalSeries
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.datalake.parallel_fetcher import (
    ParallelHistoryFetcher,
    _default_workers,
    fetch_with_backoff,
)

INSTRUMENTS = [Equity.of("NSE", f"SYM{i}") for i in range(8)]
BASE = datetime(2026, 8, 1, 9, 15, tzinfo=UTC)


def _series(n: int = 10) -> HistoricalSeries:
    candles = [
        Candle(
            instrument=INSTRUMENTS[0],
            timeframe=Timeframe.M1,
            ohlc=OHLC(open=Price(Decimal("100")), high=Price(Decimal("101")),
                      low=Price(Decimal("99")), close=Price(Decimal("100"))),
            volume=Quantity(Decimal("1000")),
            timestamp=BASE + timedelta(minutes=i),
        )
        for i in range(n)
    ]
    return HistoricalSeries(
        instrument=INSTRUMENTS[0], timeframe=Timeframe.M1,
        candles=candles, start=BASE, end=BASE + timedelta(minutes=n - 1),
    )


def _make_broker(name: str, delay: float = 0.0, fail_symbols: set[str] | None = None) -> MagicMock:
    broker = MagicMock()
    fail_symbols = fail_symbols or set()

    def _history(inst, tf, start, end):
        if delay:
            time.sleep(delay)
        if str(inst.instrument_id) in fail_symbols:
            raise RuntimeError(f"{name}: simulated failure for {inst.instrument_id}")
        return _series()

    broker.history = MagicMock(side_effect=_history)
    broker.name = name
    return broker


class TestConstructor:
    def test_exactly_one_broker_required(self):
        with pytest.raises(ValueError, match="exactly one broker"):
            ParallelHistoryFetcher({})
        with pytest.raises(ValueError, match="exactly one broker"):
            ParallelHistoryFetcher({
                "dhan": _make_broker("dhan"),
                "upstox": _make_broker("upstox"),
            })

    def test_multi_broker_dict_error_names_every_broker(self):
        # Contract: callers must build one fetcher per broker. The message names
        # the offending brokers so a multi-broker call site is diagnosable
        # instead of just crashing.
        brokers = {
            "dhan": _make_broker("dhan"),
            "upstox": _make_broker("upstox"),
            "paper": _make_broker("paper"),
        }
        with pytest.raises(ValueError) as excinfo:
            ParallelHistoryFetcher(brokers, max_workers=4)
        message = str(excinfo.value)
        assert "exactly one broker" in message
        for name in brokers:
            assert name in message

    def test_empty_broker_dict_error_mentions_no_brokers(self):
        with pytest.raises(ValueError, match="exactly one broker"):
            ParallelHistoryFetcher({}, max_workers=4)


class TestDhanIntradayWindowGuard:
    def test_dhan_intraday_range_beyond_api_window_auto_chunks(self):
        start, end = datetime(2026, 1, 1), datetime(2026, 5, 1)  # 120 days
        dhan = _make_broker("dhan")
        fetcher = ParallelHistoryFetcher({"dhan": dhan})
        results, _ = fetcher.fetch([INSTRUMENTS[0]], Timeframe.M1, start, end)
        assert len(results) == 1
        assert dhan.history.call_count == 2

    def test_dhan_intraday_range_within_api_window_ok(self):
        fetcher = ParallelHistoryFetcher({"dhan": _make_broker("dhan")})
        start, end = datetime(2026, 1, 1), datetime(2026, 3, 2)
        results, _ = fetcher.fetch([INSTRUMENTS[0]], Timeframe.M1, start, end)
        assert len(results) == 1

    def test_dhan_daily_range_not_limited(self):
        fetcher = ParallelHistoryFetcher({"dhan": _make_broker("dhan")})
        start, end = datetime(2026, 1, 1), datetime(2026, 8, 1)
        results, _ = fetcher.fetch([INSTRUMENTS[0]], Timeframe.D1, start, end)
        assert len(results) == 1


class TestParallelFetch:
    def test_fetch_all_succeed(self):
        fetcher = ParallelHistoryFetcher({"dhan": _make_broker("dhan")})
        results, errors = fetcher.fetch(
            INSTRUMENTS, Timeframe.M1, BASE, BASE + timedelta(days=7),
        )
        assert len(results) == 8
        assert errors == []

    def test_empty_series_is_an_error(self):
        broker = _make_broker("dhan")
        broker.history = MagicMock(return_value=_series(0))
        fetcher = ParallelHistoryFetcher({"dhan": broker})
        results, errors = fetcher.fetch(
            [INSTRUMENTS[0]], Timeframe.M1, BASE, BASE + timedelta(days=1),
        )
        assert results == {}
        assert errors and "empty" in errors[0]


class TestRateLimit:
    def test_fetch_respects_historical_rate_bucket(self) -> None:
        class _SlowBroker:
            def history(self, inst, timeframe, start, end):
                return _series(5)

        fetcher = ParallelHistoryFetcher({"dhan": _SlowBroker()}, max_workers=8)
        t0 = time.monotonic()
        fetcher.fetch(
            [Equity.of("NSE", f"THR{i}") for i in range(12)],
            Timeframe.M1, BASE, BASE + timedelta(days=7),
        )
        assert time.monotonic() - t0 >= 0.35

    def test_transport_limiter_skips_fetcher_prefetch_acquire(self) -> None:
        from tradex_brokers.common.resilience import limiter_for_provider

        limiter = limiter_for_provider("dhan")
        acquires = 0
        original = limiter.acquire

        def counting(bucket: str, *, timeout: float = 30.0) -> bool:
            nonlocal acquires
            acquires += 1
            return original(bucket, timeout=timeout)

        limiter.acquire = counting  # type: ignore[method-assign]
        broker = MagicMock()
        broker.rate_limiter = limiter
        broker.history = MagicMock(return_value=_series(5))

        fetcher = ParallelHistoryFetcher({"dhan": broker}, max_workers=4)
        fetcher.fetch(
            [Equity.of("NSE", f"ACQ{i}") for i in range(10)],
            Timeframe.M1, BASE, BASE + timedelta(days=7),
        )
        assert broker.history.call_count == 10
        assert acquires == 0


class TestRangedFetch:
    def test_ranged_instrument_fetched_only_for_given_windows(self):
        dhan = _make_broker("dhan")
        fetcher = ParallelHistoryFetcher({"dhan": dhan})
        inst = INSTRUMENTS[0]
        r1 = (datetime(2026, 8, 3), datetime(2026, 8, 4))
        r2 = (datetime(2026, 8, 10), datetime(2026, 8, 11))
        results, _ = fetcher.fetch(
            [inst], Timeframe.M1,
            datetime(2026, 8, 1), datetime(2026, 8, 20),
            ranges={str(inst.instrument_id): [r1, r2]},
        )
        assert len(results) == 1
        assert dhan.history.call_count == 2
        calls = dhan.history.call_args_list
        assert (calls[0].args[2], calls[0].args[3]) == r1
        assert (calls[1].args[2], calls[1].args[3]) == r2

    def test_ranged_overlap_deduplicated(self):
        fetcher = ParallelHistoryFetcher({"dhan": _make_broker("dhan")})
        inst = INSTRUMENTS[0]
        win = (datetime(2026, 8, 3), datetime(2026, 8, 4))
        results, _ = fetcher.fetch(
            [inst], Timeframe.M1,
            datetime(2026, 8, 1), datetime(2026, 8, 20),
            ranges={str(inst.instrument_id): [win, win]},
        )
        candles = next(iter(results.values())).candles
        stamps = [c.timestamp for c in candles]
        assert len(stamps) == len(set(stamps))

    def test_unranged_instrument_ignores_ranges_map(self):
        dhan = _make_broker("dhan")
        fetcher = ParallelHistoryFetcher({"dhan": dhan})
        a, b = INSTRUMENTS[0], INSTRUMENTS[1]
        results, _ = fetcher.fetch(
            [a, b], Timeframe.M1,
            BASE, BASE + timedelta(days=7),
            ranges={str(a.instrument_id): [(BASE, BASE + timedelta(days=1))]},
        )
        assert len(results) == 2
        assert dhan.history.call_count == 2


def test_default_workers_dhan_is_conservative() -> None:
    assert _default_workers("dhan") == 2
    assert _default_workers("upstox") == 4


def test_fetch_with_backoff_retries_after_partial_failure() -> None:
    calls = {"n": 0}

    class _FlakyFetcher:
        def fetch(self, instruments, tf, start, end, ranges=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return {}, [
                    f"{i.instrument_id}: Rate limit exceeded"
                    for i in instruments
                ]
            return {str(instruments[0].instrument_id): _series(3)}, []

    inst = Equity.of("NSE", "RETRY")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(time, "sleep", lambda _s: None)
        out = fetch_with_backoff(
            _FlakyFetcher(), [inst], Timeframe.M1,
            BASE, BASE + timedelta(days=1), max_retries=3,
        )
    assert calls["n"] == 2
    assert str(inst.instrument_id) in out


def test_fetch_with_backoff_fails_fast_on_permanent_error() -> None:
    class _PermanentFaker:
        def fetch(self, instruments, tf, start, end, ranges=None):
            return {}, [
                f"{i.instrument_id}: empty stitched series"
                for i in instruments
            ]

    inst = Equity.of("NSE", "DEAD")
    t0 = time.monotonic()
    out = fetch_with_backoff(
        _PermanentFaker(), [inst], Timeframe.M1,
        BASE, BASE + timedelta(days=1), max_retries=6,
    )
    assert time.monotonic() - t0 < 1.0
    assert out == {}
