"""AC-13 — scanner portfolio context, rate limit, and result cache.

Three sub-items of lane 3i, each pinned by the failure it prevents:

* the rate limit is ENFORCED on the real provider path, not merely present;
* the cache key is neither too narrow (serves stale) nor too wide (never
  hits) — the miss-after-change cases are the load-bearing ones;
* portfolio context reaches the result, and changing the book is a cache
  miss, so a cached "fresh entry" cannot survive a position being opened.

The existing ``max_bars`` warmup work is covered in
``test_strategy_scanner.py`` and is untouched here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain import (
    OHLC,
    Candle,
    Condition,
    Equity,
    HistoricalSeries,
    Price,
    Quantity,
    ScannerDefinition,
    Timeframe,
)
from tradex_domain.execution import PortfolioSnapshot, Position
from tradex_domain.value_objects import Money
from tradex_strategy.core.scanner import (
    ScannerEngine,
    default_call_interval,
    warmup_bars,
)
from tradex_strategy.core.scanner_limits import (
    ScanRateLimiter,
    ScanResultCache,
    _fingerprint,
)


def _candle(close: float, ts: datetime, instrument: Equity | None = None) -> Candle:
    return Candle(
        instrument=instrument or Equity.of("NSE", "RELIANCE"),
        timeframe=Timeframe.D1,
        ohlc=OHLC(
            open=Price(value=Decimal(str(close - 1))),
            high=Price(value=Decimal(str(close + 1))),
            low=Price(value=Decimal(str(close - 1))),
            close=Price(value=Decimal(str(close))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


def _series(closes: list[float]) -> list[Candle]:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return [_candle(c, base + timedelta(days=i)) for i, c in enumerate(closes)]


class _CountingMarket:
    """Market provider that counts calls and can swap its answer."""

    def __init__(self, candles: list[Candle] | None = None) -> None:
        self._candles = candles or []
        self.calls = 0

    def history(self, instrument, timeframe, start, end) -> HistoricalSeries:
        self.calls += 1
        return HistoricalSeries(
            instrument=instrument,
            timeframe=timeframe,
            candles=self._candles,
            start=start,
            end=end,
        )

    def set_closes(self, closes: list[float]) -> None:
        self._candles = _series(closes)


class _Clock:
    """Monotonic clock that only advances when something sleeps."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def _universe(n: int) -> list[Equity]:
    return [Equity.of("NSE", f"SYM{i}") for i in range(n)]


def _defn(universe: list[Equity], **kw) -> ScannerDefinition:
    return ScannerDefinition(
        universe=universe,
        conditions=[Condition(name="close", operator=">", threshold=50.0)],
        **kw,
    )


# ---------------------------------------------------------------------------
# (a) Rate limit — ENFORCED.  Fails if the limiter is removed from _history.
# ---------------------------------------------------------------------------


class TestScannerRateLimit:
    def test_scan_over_universe_is_paced_not_bursty(self) -> None:
        """A 10-symbol scan against a shared provider sleeps between reads.

        This is the load-bearing direction: the provider is a fake, the
        clock is injected, and the assertion is on the *gaps* the scan
        actually took.  Deleting the limiter (or moving it off the
        provider call) leaves ``slept`` empty and fails here.
        """
        market = _CountingMarket(_series([100.0]))
        clock = _Clock()
        engine = ScannerEngine(market=market, min_call_interval=0.25)
        # Inject the fake clock after construction: the limiter is the
        # scanner's, and swapping its clock is how a test observes pacing
        # without spending real seconds.
        engine._limiter.clock = clock
        engine._limiter.sleep = clock.sleep

        engine.run(_defn(_universe(10)))

        assert market.calls == 10
        # First read is immediate; every later read waits out the remainder.
        # 9 gaps of the full interval: the clock advances by exactly the
        # sleep, so a run never leaves a partial gap behind.
        assert clock.slept == [0.25] * 9
        assert engine.throttled_calls == 9

    def test_first_call_is_never_delayed(self) -> None:
        """A single-symbol scan sleeps nothing — the gate is not a fixed tax."""
        market = _CountingMarket(_series([100.0]))
        clock = _Clock()
        engine = ScannerEngine(market=market, min_call_interval=0.25)
        engine._limiter.clock = clock
        engine._limiter.sleep = clock.sleep

        engine.run(_defn(_universe(1)))

        assert clock.slept == []
        assert engine.throttled_calls == 0

    def test_pacing_carries_across_separate_scans(self) -> None:
        """The gate is on the engine, so back-to-back scans stay paced."""
        market = _CountingMarket(_series([100.0]))
        clock = _Clock()
        engine = ScannerEngine(market=market, min_call_interval=0.25)
        engine._limiter.clock = clock
        engine._limiter.sleep = clock.sleep

        engine.run(_defn(_universe(2)))
        engine.invalidate_cache()  # force a second real read pass
        engine.run(_defn(_universe(2)))

        # 1 gap inside each scan, 1 more across the boundary.
        assert clock.slept == [0.25, 0.25, 0.25]
        assert market.calls == 4

    def test_streaming_path_is_not_paced(self) -> None:
        """A buffered instrument never reaches the provider, so never waits.

        Pacing an in-memory read would be a pure slowdown; the gate belongs
        on the outbound path only.
        """
        market = _CountingMarket(_series([1.0]))
        clock = _Clock()
        engine = ScannerEngine(market=market, min_call_interval=0.25, max_bars=50)
        engine._limiter.clock = clock
        engine._limiter.sleep = clock.sleep
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(20):
            for sym in _universe(5):
                engine.consume(_candle(100.0, base + timedelta(days=i), sym))

        engine.run(_defn(_universe(5)))

        assert market.calls == 0
        assert clock.slept == []

    def test_local_parquet_provider_is_not_paced(self) -> None:
        """Offline providers are exempt — a scan must not crawl at 4 Hz."""
        class ParquetMarketProvider(_CountingMarket):  # noqa: D401 - name IS the point
            pass

        market = ParquetMarketProvider(_series([100.0]))
        clock = _Clock()
        engine = ScannerEngine(market=market)
        engine._limiter.clock = clock
        engine._limiter.sleep = clock.sleep

        engine.run(_defn(_universe(8)))

        assert engine._limiter.min_interval == 0.0
        assert clock.slept == []

    def test_bulk_prefetch_provider_is_not_paced(self) -> None:
        class BulkPrefetchMarketProvider(_CountingMarket):
            pass

        engine = ScannerEngine(market=BulkPrefetchMarketProvider())
        assert engine._limiter.min_interval == 0.0

    def test_default_interval_paces_an_unrecognised_provider(self) -> None:
        """A provider that is not one of the two local classes is paced.

        The scanner duck-types its provider, so the safe default for an
        unknown one is the limit, not no limit.
        """
        assert default_call_interval(_CountingMarket()) > 0.0
        assert default_call_interval(object()) == default_call_interval(
            _CountingMarket()
        )

    def test_explicit_zero_opts_out(self) -> None:
        engine = ScannerEngine(market=_CountingMarket(), min_call_interval=0.0)
        assert engine._limiter.min_interval == 0.0

    def test_limiter_itself_spaces_acquires(self) -> None:
        """Unit-level: acquire() sleeps the remainder, first call is free."""
        clock = _Clock()
        limiter = ScanRateLimiter(min_interval=2.0, sleep=clock.sleep, clock=clock)

        assert limiter.acquire() == 0.0
        assert limiter.acquire() == 2.0  # clock did not move on its own
        assert limiter.throttled_calls == 1

    def test_zero_interval_limiter_never_sleeps(self) -> None:
        clock = _Clock()
        limiter = ScanRateLimiter(min_interval=0.0, sleep=clock.sleep, clock=clock)
        for _ in range(5):
            assert limiter.acquire() == 0.0
        assert clock.slept == []
        assert limiter.throttled_calls == 0


# ---------------------------------------------------------------------------
# (b) Result cache — CORRECT in both directions.
#     The miss-after-change cases below are the load-bearing ones.
# ---------------------------------------------------------------------------


class TestScannerResultCache:
    def test_identical_rescan_does_not_re_read(self) -> None:
        market = _CountingMarket(_series([100.0]))
        engine = ScannerEngine(market=market, min_call_interval=0.0)
        defn = _defn(_universe(3))

        first = engine.run(defn)
        second = engine.run(defn)

        assert market.calls == 3
        assert engine.cache_hits == 1
        assert engine.cache_misses == 1
        assert [r.instrument.symbol for r in second] == [
            r.instrument.symbol for r in first
        ]

    def test_new_bar_invalidates_the_cached_result(self) -> None:
        """A streamed bar changes the answer, so the cached one must not be used.

        The negative control that a too-narrow key fails: the price moves
        above the threshold only after ``consume``.  Serving the pre-bar
        result would report no match for a setup that now matches.
        """
        market = _CountingMarket(_series([10.0]))
        engine = ScannerEngine(market=market, min_call_interval=0.0)
        sym = Equity.of("NSE", "SYM0")
        defn = _defn([sym])

        assert engine.run(defn) == []  # close 10 is not > 50

        base = datetime(2026, 1, 1, tzinfo=UTC)
        engine.consume(_candle(100.0, base, sym))

        after = engine.run(defn)
        assert len(after) == 1
        assert after[0].indicator_values["close"] == 100.0
        assert engine.cache_hits == 0

    def test_provider_data_change_misses_when_no_bar_streamed(self) -> None:
        """With no ``consume`` the key is stable, so the data version is the
        backstop: an explicit invalidation is what makes a changed snapshot
        visible.  This pins that the knob exists and works.
        """
        market = _CountingMarket(_series([10.0]))
        engine = ScannerEngine(market=market, min_call_interval=0.0)
        defn = _defn([Equity.of("NSE", "SYM0")])

        assert engine.run(defn) == []
        assert engine.cache_hits == 0

        market.set_closes([100.0])
        engine.invalidate_cache()
        assert len(engine.run(defn)) == 1
        assert engine.cache_hits == 0

    def test_changed_threshold_misses(self) -> None:
        """A too-wide key (definition dropped) would serve the old answer."""
        market = _CountingMarket(_series([100.0]))
        engine = ScannerEngine(market=market, min_call_interval=0.0)
        sym = Equity.of("NSE", "SYM0")

        loose = ScannerDefinition(
            universe=[sym],
            conditions=[Condition(name="close", operator=">", threshold=50.0)],
        )
        strict = ScannerDefinition(
            universe=[sym],
            conditions=[Condition(name="close", operator=">", threshold=500.0)],
        )

        assert len(engine.run(loose)) == 1
        assert engine.run(strict) == []
        assert engine.cache_hits == 0

    def test_changed_condition_params_miss(self) -> None:
        """Params are part of the key, not just the name/operator.

        Uses two engines over the same bars so neither definition's warmup
        sizing truncates the other's buffer — the point under test is the
        cache key, not the buffer.
        """
        sym = Equity.of("NSE", "SYM0")
        base = datetime(2026, 1, 1, tzinfo=UTC)
        fast = ScannerDefinition(
            universe=[sym],
            conditions=[
                Condition(name="sma", params={"period": 5}, operator=">", threshold=0.0)
            ],
        )
        slow = ScannerDefinition(
            universe=[sym],
            conditions=[
                Condition(name="sma", params={"period": 30}, operator=">", threshold=0.0)
            ],
        )

        def _seeded() -> ScannerEngine:
            engine = ScannerEngine(market=_CountingMarket(), min_call_interval=0.0)
            for i in range(60):
                engine.consume(_candle(float(100 + i), base + timedelta(days=i), sym))
            return engine

        fast_vals = _seeded().run(fast)[0].indicator_values
        slow_vals = _seeded().run(slow)[0].indicator_values
        # A rising series, so the two SMAs are genuinely different values.
        assert fast_vals["sma"] != slow_vals["sma"]

    def test_params_change_misses_within_one_engine(self) -> None:
        """On a single engine, changing the period is a key change, not a hit.

        Both definitions see the same 60-bar buffer, so any difference in
        outcome is attributable to the key, not to buffer sizing.
        """
        engine = ScannerEngine(market=_CountingMarket(), min_call_interval=0.0)
        sym = Equity.of("NSE", "SYM0")
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(60):
            engine.consume(_candle(float(100 + i), base + timedelta(days=i), sym))

        def _sma(period: int) -> ScannerDefinition:
            return ScannerDefinition(
                universe=[sym],
                conditions=[
                    Condition(name="sma", params={"period": period},
                              operator=">", threshold=0.0)
                ],
            )

        # Run the long one first so the buffer is large enough for both.
        first = engine.run(_sma(30))[0].indicator_values["sma"]
        second = engine.run(_sma(5))[0].indicator_values["sma"]
        assert first != second
        assert engine.cache_hits == 0

    def test_changed_universe_misses(self) -> None:
        market = _CountingMarket(_series([100.0]))
        engine = ScannerEngine(market=market, min_call_interval=0.0)
        one = _defn([Equity.of("NSE", "SYM0")])
        two = _defn([Equity.of("NSE", "SYM0"), Equity.of("NSE", "SYM1")])

        assert len(engine.run(one)) == 1
        assert len(engine.run(two)) == 2
        assert market.calls == 3  # 1 + 2, no reuse of the 1-symbol answer

    def test_window_days_misses(self) -> None:
        """Two engines over the same definition but different windows differ."""
        market = _CountingMarket(_series([100.0]))
        defn = _defn([Equity.of("NSE", "SYM0")])
        wide = ScannerEngine(market=market, window_days=30, min_call_interval=0.0)
        narrow = ScannerEngine(market=market, window_days=5, min_call_interval=0.0)

        # Distinct engines, so the key must differ on window_days alone —
        # asserted directly so the test does not depend on cache timing.
        assert wide._cache_key(defn) != narrow._cache_key(defn)

    def test_equal_definitions_hit_regardless_of_param_order(self) -> None:
        """Key canonicalisation: param dict order must not cause a miss."""
        engine = ScannerEngine(market=_CountingMarket(), min_call_interval=0.0)
        sym = Equity.of("NSE", "SYM0")
        a = ScannerDefinition(
            universe=[sym],
            conditions=[
                Condition(name="sma", params={"period": 5, "source": "close"},
                          operator=">", threshold=1.0)
            ],
        )
        b = ScannerDefinition(
            universe=[sym],
            conditions=[
                Condition(name="sma", params={"source": "close", "period": 5},
                          operator=">", threshold=1.0)
            ],
        )
        assert engine._cache_key(a) == engine._cache_key(b)

    def test_cached_list_is_copied_not_shared(self) -> None:
        """A caller mutating a returned list must not corrupt the cache."""
        market = _CountingMarket(_series([100.0]))
        engine = ScannerEngine(market=market, min_call_interval=0.0)
        defn = _defn([Equity.of("NSE", "SYM0")])

        first = engine.run(defn)
        first.clear()
        second = engine.run(defn)

        assert len(second) == 1

    def test_zero_ttl_disables_caching(self) -> None:
        market = _CountingMarket(_series([100.0]))
        engine = ScannerEngine(
            market=market, min_call_interval=0.0, cache_ttl_seconds=0.0
        )
        defn = _defn([Equity.of("NSE", "SYM0")])
        engine.run(defn)
        engine.run(defn)
        assert market.calls == 2
        assert engine.cache_hits == 0

    def test_cache_expires(self) -> None:
        now = [0.0]
        market = _CountingMarket(_series([100.0]))
        engine = ScannerEngine(market=market, min_call_interval=0.0)
        engine._cache._clock = lambda: now[0]
        defn = _defn([Equity.of("NSE", "SYM0")])

        engine.run(defn)
        now[0] = 10.0
        engine.run(defn)
        assert market.calls == 1
        now[0] = 1_000.0
        engine.run(defn)
        assert market.calls == 2

    def test_cache_evicts_at_max_entries(self) -> None:
        cache: ScanResultCache[int] = ScanResultCache(
            max_entries=2, ttl_seconds=100.0
        )
        for i in range(4):
            cache.set(f"k{i}", [i])
        assert cache.size == 2

    def test_fingerprint_distinguishes_bool_from_int(self) -> None:
        """True and 1 must not collide — a bool param is a real param."""
        assert _fingerprint({"p": True}) != _fingerprint({"p": 1})
        assert _fingerprint({"p": False}) != _fingerprint({"p": 0})


# ---------------------------------------------------------------------------
# (c) Portfolio context.
# ---------------------------------------------------------------------------


def _position(symbol: str, qty: str) -> Position:
    return Position(
        instrument=Equity.of("NSE", symbol),
        quantity=Quantity(value=Decimal(qty)),
        avg_price=Price(value=Decimal("100")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )


class TestScannerPortfolioContext:
    def test_result_reports_what_the_book_already_holds(self) -> None:
        engine = ScannerEngine(
            market=_CountingMarket(_series([100.0])), min_call_interval=0.0
        )
        held, flat = Equity.of("NSE", "HELD"), Equity.of("NSE", "FLAT")
        engine.set_portfolio(PortfolioSnapshot(positions=[_position("HELD", "25")]))

        results = {r.instrument.symbol: r for r in engine.run(_defn([held, flat]))}

        assert results["HELD"].metadata["held_quantity"] == 25.0
        assert results["HELD"].metadata["in_portfolio"] is True
        assert results["FLAT"].metadata["held_quantity"] == 0.0
        assert results["FLAT"].metadata["in_portfolio"] is False

    def test_short_position_is_reported_signed(self) -> None:
        engine = ScannerEngine(
            market=_CountingMarket(_series([100.0])), min_call_interval=0.0
        )
        sym = Equity.of("NSE", "SHORTY")
        engine.set_portfolio(PortfolioSnapshot(positions=[_position("SHORTY", "-10")]))

        result = engine.run(_defn([sym]))[0]
        assert result.metadata["held_quantity"] == -10.0
        assert result.metadata["in_portfolio"] is True

    def test_same_symbol_other_exchange_is_not_counted_as_held(self) -> None:
        """A BSE TCS is not the NSE TCS the scan is looking at."""
        from tradex_domain import ExchangeId

        engine = ScannerEngine(
            market=_CountingMarket(_series([100.0])), min_call_interval=0.0
        )
        engine.set_portfolio(
            PortfolioSnapshot(
                positions=[
                    Position(
                        instrument=Equity.of(ExchangeId.BSE, "TCS"),
                        quantity=Quantity(value=Decimal("5")),
                        avg_price=Price(value=Decimal("100")),
                        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
                        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
                    )
                ]
            )
        )
        result = engine.run(_defn([Equity.of(ExchangeId.NSE, "TCS")]))[0]
        assert result.metadata["in_portfolio"] is False

    def test_zero_quantity_position_is_not_a_holding(self) -> None:
        """A broker's holdings response keeps exited rows at quantity 0."""
        engine = ScannerEngine(
            market=_CountingMarket(_series([100.0])), min_call_interval=0.0
        )
        sym = Equity.of("NSE", "EXITED")
        engine.set_portfolio(
            PortfolioSnapshot(positions=[_position("EXITED", "0")])
        )
        result = engine.run(_defn([sym]))[0]
        assert result.metadata["in_portfolio"] is False
        assert result.metadata["held_quantity"] == 0.0

    def test_no_portfolio_means_no_annotations(self) -> None:
        """An engine never told about the book behaves exactly as before."""
        engine = ScannerEngine(
            market=_CountingMarket(_series([100.0])), min_call_interval=0.0
        )
        result = engine.run(_defn([Equity.of("NSE", "SYM0")]))[0]
        assert "held_quantity" not in result.metadata
        assert "in_portfolio" not in result.metadata

    def test_opening_a_position_invalidates_the_cached_scan(self) -> None:
        """The load-bearing portfolio test: a cached 'fresh entry' must die.

        A scan run while flat caches ``in_portfolio: False``.  Opening the
        position then has to miss, or the UI keeps telling the user the
        symbol is a fresh entry after they bought it.
        """
        market = _CountingMarket(_series([100.0]))
        engine = ScannerEngine(market=market, min_call_interval=0.0)
        sym = Equity.of("NSE", "SYM0")
        defn = _defn([sym])
        # Flat book first, so the cached answer really is "fresh entry".
        engine.set_portfolio(PortfolioSnapshot())
        flat = engine.run(defn)[0]
        assert flat.metadata["in_portfolio"] is False

        engine.set_portfolio(PortfolioSnapshot(positions=[_position("SYM0", "10")]))

        held = engine.run(defn)[0]
        assert held.metadata["in_portfolio"] is True
        assert engine.cache_hits == 0
        assert market.calls == 2

    def test_equal_portfolio_snapshot_still_hits(self) -> None:
        """Rebuilding an equal snapshot must not defeat the cache.

        This is the too-wide-key control.  ``set_portfolio`` is what a
        caller does on every request (re-read the book, hand it over), so
        if the key moved on every call the cache would never hit at all.
        The portfolio's content is in the key precisely so an identical
        rebuild produces an identical key.
        """
        market = _CountingMarket(_series([100.0]))
        engine = ScannerEngine(market=market, min_call_interval=0.0)
        defn = _defn([Equity.of("NSE", "SYM0")])
        engine.set_portfolio(PortfolioSnapshot(positions=[_position("SYM0", "10")]))
        first = engine.run(defn)
        engine.set_portfolio(PortfolioSnapshot(positions=[_position("SYM0", "10")]))
        second = engine.run(defn)
        assert engine.cache_hits == 1
        assert market.calls == 1
        assert second[0].metadata == first[0].metadata

    def test_position_order_does_not_cause_a_miss(self) -> None:
        """Broker position order is not stable and must not key the cache."""
        market = _CountingMarket(_series([100.0]))
        engine = ScannerEngine(market=market, min_call_interval=0.0)
        defn = _defn([Equity.of("NSE", "SYM0"), Equity.of("NSE", "SYM1")])
        engine.set_portfolio(
            PortfolioSnapshot(
                positions=[_position("SYM0", "10"), _position("SYM1", "5")]
            )
        )
        engine.run(defn)
        engine.set_portfolio(
            PortfolioSnapshot(
                positions=[_position("SYM1", "5"), _position("SYM0", "10")]
            )
        )
        engine.run(defn)
        assert engine.cache_hits == 1

    def test_account_equity_change_does_not_invalidate(self) -> None:
        """Too-narrow-key control in the other direction.

        Equity moves on every mark but changes no result, so it must not
        invalidate the scan — otherwise the cache is dead again.
        """
        from tradex_domain import Account

        market = _CountingMarket(_series([100.0]))
        engine = ScannerEngine(market=market, min_call_interval=0.0)
        defn = _defn([Equity.of("NSE", "SYM0")])
        engine.set_portfolio(
            PortfolioSnapshot(
                positions=[_position("SYM0", "10")],
                account=Account(account_id="acct-1"),
            )
        )
        engine.run(defn)
        engine.set_portfolio(
            PortfolioSnapshot(
                positions=[_position("SYM0", "10")],
                account=Account(
                    account_id="acct-1",
                    equity=Money(amount=Decimal("999"), currency="INR"),
                ),
            )
        )
        engine.run(defn)
        assert engine.cache_hits == 1

    def test_mutating_the_caller_list_does_not_change_the_copy(self) -> None:
        positions = [_position("SYM0", "10")]
        engine = ScannerEngine(
            market=_CountingMarket(_series([100.0])), min_call_interval=0.0
        )
        engine.set_portfolio(PortfolioSnapshot(positions=positions))
        positions.clear()
        result = engine.run(_defn([Equity.of("NSE", "SYM0")]))[0]
        assert result.metadata["held_quantity"] == 10.0

    def test_portfolio_property_round_trips(self) -> None:
        engine = ScannerEngine(market=_CountingMarket(), min_call_interval=0.0)
        assert engine.portfolio is None
        snap = PortfolioSnapshot(positions=[_position("SYM0", "3")])
        engine.set_portfolio(snap)
        assert engine.portfolio is not None
        assert len(engine.portfolio.positions) == 1
        engine.set_portfolio(None)
        assert engine.portfolio is None


# ---------------------------------------------------------------------------
# The already-done half of lane 3i, re-asserted so a future change that
# un-does it is caught here too.
# ---------------------------------------------------------------------------


class TestScannerWarmupUnchanged:
    def test_long_period_warmup_is_still_derived(self) -> None:
        cond = Condition(name="sma", params={"period": 200}, operator=">", threshold=1.0)
        assert warmup_bars([cond]) >= 201

    def test_raw_price_field_needs_no_warmup(self) -> None:
        cond = Condition(name="close", operator=">", threshold=1.0)
        assert warmup_bars([cond]) == 1
