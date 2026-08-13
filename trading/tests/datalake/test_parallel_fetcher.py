"""Tests for ParallelHistoryFetcher — routing, parallelism, failover."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from tradex_domain import OHLC, Candle, Equity, Timeframe
from tradex_domain.errors import SDKError
from tradex_domain.market import HistoricalSeries
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.datalake.parallel_fetcher import ParallelHistoryFetcher, _split

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
    """Mock broker that returns a HistoricalSeries with optional delay/failure."""
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


# --------------------------------------------------------------------------- #
# _split helper
# --------------------------------------------------------------------------- #

class TestSplit:
    def test_split_even(self):
        assert _split([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]

    def test_split_uneven(self):
        result = _split([1, 2, 3, 4, 5], 2)
        assert len(result) == 2
        assert len(result[0]) + len(result[1]) == 5

    def test_split_more_chunks_than_items(self):
        result = _split([1, 2], 5)
        flat = [x for chunk in result for x in chunk]
        assert flat == [1, 2]

    def test_split_zero_chunks(self):
        assert _split([1, 2], 0) == [[1, 2]]


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #

class TestRouting:
    def test_short_range_uses_all_brokers(self):
        """< 30 days → both brokers selected."""
        brokers = {"dhan": _make_broker("dhan"), "upstox": _make_broker("upstox")}
        fetcher = ParallelHistoryFetcher(brokers)
        picked = fetcher._pick_brokers(days=15)
        assert "dhan" in picked
        assert "upstox" in picked

    def test_long_range_uses_dhan_only(self):
        """>= 30 days → Dhan only."""
        brokers = {"dhan": _make_broker("dhan"), "upstox": _make_broker("upstox")}
        fetcher = ParallelHistoryFetcher(brokers)
        picked = fetcher._pick_brokers(days=90)
        assert picked == ["dhan"]

    def test_long_range_fallback_no_dhan(self):
        """>= 30 days with no Dhan → use whatever is available."""
        brokers = {"upstox": _make_broker("upstox")}
        fetcher = ParallelHistoryFetcher(brokers)
        picked = fetcher._pick_brokers(days=90)
        assert "upstox" in picked

    def test_boundary_30_days_uses_dhan(self):
        """Exactly 30 days → Dhan only (>= threshold)."""
        brokers = {"dhan": _make_broker("dhan"), "upstox": _make_broker("upstox")}
        fetcher = ParallelHistoryFetcher(brokers)
        picked = fetcher._pick_brokers(days=30)
        assert picked == ["dhan"]

    def test_29_days_uses_both(self):
        """29 days → both brokers."""
        brokers = {"dhan": _make_broker("dhan"), "upstox": _make_broker("upstox")}
        fetcher = ParallelHistoryFetcher(brokers)
        picked = fetcher._pick_brokers(days=29)
        assert len(picked) == 2


# --------------------------------------------------------------------------- #
# Dhan intraday window guard
# --------------------------------------------------------------------------- #

class TestDhanIntradayWindowGuard:
    def test_dhan_intraday_range_beyond_api_window_fails_loud(self):
        """> 90-day M1 range, Dhan only → fail loud, no silent truncation."""
        fetcher = ParallelHistoryFetcher({"dhan": _make_broker("dhan")})
        start, end = datetime(2026, 1, 1), datetime(2026, 5, 1)  # 120 days
        with pytest.raises(SDKError, match="Dhan intraday history limited to"):
            fetcher.fetch([INSTRUMENTS[0]], Timeframe.M1, start, end)

    def test_dhan_intraday_range_within_api_window_ok(self):
        """60-day M1 range, Dhan only → within 90-day window, fetches fine."""
        fetcher = ParallelHistoryFetcher({"dhan": _make_broker("dhan")})
        start, end = datetime(2026, 1, 1), datetime(2026, 3, 2)  # 60 days
        results = fetcher.fetch([INSTRUMENTS[0]], Timeframe.M1, start, end)
        assert len(results) == 1

    def test_dhan_daily_range_not_limited(self):
        """> 90-day D1 range, Dhan only → historical endpoint has no cap."""
        fetcher = ParallelHistoryFetcher({"dhan": _make_broker("dhan")})
        start, end = datetime(2026, 1, 1), datetime(2026, 8, 1)  # 212 days
        results = fetcher.fetch([INSTRUMENTS[0]], Timeframe.D1, start, end)
        assert len(results) == 1

    def test_beyond_window_with_other_broker_available_still_guarded(self):
        """> 90-day M1 range with dhan+upstox → Dhan is sole selected broker
        for the range, so it still fails loud rather than truncating."""
        fetcher = ParallelHistoryFetcher(
            {"dhan": _make_broker("dhan"), "upstox": _make_broker("upstox")}
        )
        start, end = datetime(2026, 1, 1), datetime(2026, 5, 1)  # 120 days
        with pytest.raises(SDKError, match="Dhan intraday history limited to"):
            fetcher.fetch([INSTRUMENTS[0]], Timeframe.M1, start, end)


# --------------------------------------------------------------------------- #
# Parallel fetch
# --------------------------------------------------------------------------- #

class TestParallelFetch:
    def test_fetch_all_succeed(self):
        """All instruments fetched successfully."""
        brokers = {"dhan": _make_broker("dhan"), "upstox": _make_broker("upstox")}
        fetcher = ParallelHistoryFetcher(brokers, max_workers=4)
        end = BASE + timedelta(days=7)
        results = fetcher.fetch(INSTRUMENTS, Timeframe.M1, BASE, end)
        assert len(results) == len(INSTRUMENTS)

    def test_fetch_empty_instruments(self):
        """Empty instrument list → empty results."""
        brokers = {"dhan": _make_broker("dhan")}
        fetcher = ParallelHistoryFetcher(brokers)
        results = fetcher.fetch([], Timeframe.M1, BASE, BASE + timedelta(days=7))
        assert results == {}

    def test_fetch_uses_both_brokers_for_short_range(self):
        """< 30 days → both brokers receive history() calls."""
        dhan = _make_broker("dhan")
        upstox = _make_broker("upstox")
        brokers = {"dhan": dhan, "upstox": upstox}
        fetcher = ParallelHistoryFetcher(brokers, max_workers=4)
        end = BASE + timedelta(days=7)
        fetcher.fetch(INSTRUMENTS, Timeframe.M1, BASE, end)
        assert dhan.history.call_count > 0
        assert upstox.history.call_count > 0
        total = dhan.history.call_count + upstox.history.call_count
        assert total == len(INSTRUMENTS)

    def test_fetch_uses_dhan_only_for_long_range(self):
        """>= 30 days → only Dhan receives history() calls."""
        dhan = _make_broker("dhan")
        upstox = _make_broker("upstox")
        brokers = {"dhan": dhan, "upstox": upstox}
        fetcher = ParallelHistoryFetcher(brokers, max_workers=4)
        end = BASE + timedelta(days=60)
        fetcher.fetch(INSTRUMENTS, Timeframe.M1, BASE, end)
        assert dhan.history.call_count == len(INSTRUMENTS)
        assert upstox.history.call_count == 0

    def test_failover_to_second_broker(self):
        """If primary broker fails, failover broker serves the symbol."""
        fail_syms = {str(INSTRUMENTS[0].instrument_id), str(INSTRUMENTS[1].instrument_id)}
        dhan = _make_broker("dhan", fail_symbols=fail_syms)
        upstox = _make_broker("upstox")
        brokers = {"dhan": dhan, "upstox": upstox}
        fetcher = ParallelHistoryFetcher(brokers, max_workers=4)
        end = BASE + timedelta(days=7)
        results = fetcher.fetch(INSTRUMENTS, Timeframe.M1, BASE, end)
        # All instruments should succeed (failover covers the failures)
        assert len(results) == len(INSTRUMENTS)

    def test_parallel_faster_than_sequential(self):
        """Parallel fetch is faster than sequential (mock delay proves it)."""
        delay = 0.05  # 50ms per call
        brokers = {"dhan": _make_broker("dhan", delay=delay)}
        # Inject an effectively-unthrottled limiter: this test measures
        # ThreadPoolExecutor parallelism, not historical-rate throttling
        # (that is covered by test_fetch_respects_historical_rate_bucket).
        from tradex_brokers.common.resilience import limiter_from_table
        fast = limiter_from_table({"historical": {"rate_per_second": 1000.0, "capacity": 1000}})
        fetcher = ParallelHistoryFetcher(brokers, max_workers=4, rate_limiter=fast)
        end = BASE + timedelta(days=7)

        t0 = time.monotonic()
        fetcher.fetch(INSTRUMENTS, Timeframe.M1, BASE, end)
        parallel_time = time.monotonic() - t0

        # Sequential would be 8 × 50ms = 400ms; parallel with 4 workers ~100ms
        sequential_estimate = len(INSTRUMENTS) * delay
        assert parallel_time < sequential_estimate * 0.7  # 30% margin


# --------------------------------------------------------------------------- #
# Failover fan-out
# --------------------------------------------------------------------------- #

class TestFailoverFanout:
    def test_broker_wide_outage_does_not_fan_out_n_times_m(self):
        """A broker-wide outage must not explode into N×M API calls.

        With 3 brokers, all down for every symbol, the naive failover tries
        every broker per symbol (8 symbols × 3 brokers = 24 calls). The
        known-failed set caps this to ~M + N (one failure per broker, then
        one primary attempt per remaining symbol). ``max_workers=1`` makes
        the bound deterministic — with concurrent workers the mark-then-check
        race could legitimately exceed the tight bound.
        """
        all_symbols = {str(i.instrument_id) for i in INSTRUMENTS}
        brokers = {
            "a": _make_broker("a", fail_symbols=all_symbols),
            "b": _make_broker("b", fail_symbols=all_symbols),
            "c": _make_broker("c", fail_symbols=all_symbols),
        }
        fetcher = ParallelHistoryFetcher(brokers, max_workers=1)
        end = BASE + timedelta(days=7)
        results = fetcher.fetch(INSTRUMENTS, Timeframe.M1, BASE, end)
        assert results == {}  # everything failed

        total_calls = sum(b.history.call_count for b in brokers.values())
        naive = len(INSTRUMENTS) * len(brokers)  # 8 × 3 = 24
        assert total_calls < naive
        # Sequential worst case: first symbol tries every broker (M calls),
        # each remaining symbol only its primary (N - 1 calls).
        assert total_calls <= len(brokers) + len(INSTRUMENTS) - 1

    def test_single_broker_outage_still_fails_over(self):
        """One broker down: other brokers absorb its chunk without fan-out."""
        all_symbols = {str(i.instrument_id) for i in INSTRUMENTS}
        dhan = _make_broker("dhan", fail_symbols=all_symbols)  # down for all
        upstox = _make_broker("upstox")
        brokers = {"dhan": dhan, "upstox": upstox}
        fetcher = ParallelHistoryFetcher(brokers, max_workers=4)
        end = BASE + timedelta(days=7)
        results = fetcher.fetch(INSTRUMENTS, Timeframe.M1, BASE, end)
        # Every symbol succeeds via failover to the healthy broker
        assert len(results) == len(INSTRUMENTS)
        # Each failed dhan call is followed by exactly one upstox call
        # (no retrying upstox per symbol, and dhan is not re-tried)
        assert dhan.history.call_count <= len(INSTRUMENTS)
        assert upstox.history.call_count == len(INSTRUMENTS)


# --------------------------------------------------------------------------- #
# Rate-limit throttling
# --------------------------------------------------------------------------- #

def test_fetch_respects_historical_rate_bucket() -> None:
    """Fan-out across N workers must still throttle to the historical rate."""
    class _SlowBroker:
        def history(self, inst, timeframe, start, end):
            return _series(5)

    fetcher = ParallelHistoryFetcher({"dhan": _SlowBroker()}, max_workers=8)
    # The fetcher builds its own limiter via limiter_for_provider("dhan"),
    # whose "historical" bucket is 5/s cap 10. A 12-instrument batch must
    # take >= ~0.4s (12 tokens / 5 per sec) rather than finishing instantly.
    t0 = time.monotonic()
    fetcher.fetch(
        [Equity.of("NSE", f"THR{i}") for i in range(12)],
        Timeframe.M1, BASE, BASE + timedelta(days=7),
    )
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.35
