"""Scanner engine — condition evaluation + ranking over a market provider."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from tradex_domain import IndicatorComputer, ScannerDefinition, ScannerResult
from tradex_domain.enums import Timeframe
from tradex_domain.errors import SDKError
from tradex_domain.execution import PortfolioSnapshot
from tradex_domain.market import Candle, HistoricalSeries
from tradex_domain.strategy import Condition

from tradex_strategy.core.scanner_limits import (
    ScanRateLimiter,
    ScanResultCache,
    _fingerprint,
)

_OPS = {
    ">": lambda v, t: v > t,
    ">=": lambda v, t: v >= t,
    "<": lambda v, t: v < t,
    "<=": lambda v, t: v <= t,
    "==": lambda v, t: v == t,
    "!=": lambda v, t: v != t,
}

# Storage bound for the per-instrument streaming buffer until a definition is
# known, and the size an explicit ``max_bars`` starts from.  It is deliberately
# NOT the warmup window: the first definition run resizes the buffer to
# ``warmup_bars(conditions)``, so a period longer than 30 bars is no longer
# truncated.  30 keeps memory bounded at ~30 candles x N instruments until then.
_DEFAULT_MAX_BARS = 30

# Bars held beyond the longest declared indicator period.  A declared period
# of P needs P + 1 bars before the indicator emits its first value (every
# period-based indicator needs one bar to compute a change from), so a buffer
# of exactly P bars holds nothing but warmup.  This is the same quantity the
# walk-forward warmup carry uses (lane 1d), deliberately: "warm" means the same
# thing in both places.
_WARMUP_MARGIN_BARS = 1

# Longest a finished scan may be reused.  Short enough that the UI's "scan
# again" is really a rescan, long enough that a double-click or a client
# retry inside one session does not re-read the whole universe.  The bar
# version in the key, not this number, is what makes a stale read impossible
# — see ``ScannerEngine.consume``.
_DEFAULT_CACHE_TTL_SECONDS = 30.0

# Cache entries retained.  One entry per distinct scan, and a session runs a
# handful of scanners, so this is a bound against a caller that varies a
# condition on every request rather than a real working set.
_DEFAULT_CACHE_MAX_ENTRIES = 64

# Seconds between two consecutive provider reads by one scanner, used when
# the provider is shared with the venue.  This is the scanner's own budget
# and deliberately slow: a scan is not latency-critical, and its whole point
# is to stop drawing on the quota the live feed is using.  It is NOT a
# reproduction of the venue's published table — ``TokenBucketRateLimiter``
# already does that inside the HTTP layer, per request — so it is
# deliberately not read from config, because a half-configured value here
# would silently become a second, competing quota.
DEFAULT_SCANNER_CALL_INTERVAL = 0.25


#: Provider classes whose reads are local and must not be paced.  Keyed by
#: class name so an import cycle (market_data -> brokers -> ...) cannot be
#: introduced here, and so a test fake is paced like the real thing it stands
#: in for unless it is one of these.
_LOCAL_MARKET_PROVIDERS = frozenset(
    {"MarketWindowLoader", "ParquetMarketProvider", "BulkPrefetchMarketProvider"}
)


def scanner_call_interval(market: Any) -> float:
    """Provider call interval for *market*: paced when shared, free when local.

    ``ScannerEngine`` is constructed with three different things across the
    repository, and only one of them is a venue:

    * ``runtime/startup.py`` — ``scanner_market = broker`` in paper/live.
      A 500-symbol scan is 500 calls into the same quota the live feed is
      drawing from, so this is the case the limit exists for.
    * ``runtime/startup.py`` — ``MarketWindowLoader`` wrapping ``ParquetMarketProvider``
      in backtest/replay. Local disk. Imposing a crawl on a local read would
      only add minutes to a scan and gain nothing, so the gate is off.
    * ``interfaces/routes/chart.py`` — ``BulkPrefetchMarketProvider``. One
      bulk read, then in-memory slices. Nothing is paced.

    The discriminator is the class name rather than an ``isinstance`` chain:
    the scanner deliberately duck-types its provider (``_market`` is typed
    ``Any`` and every test supplies a fake), and a duck-typed object that is
    neither of the two local classes is *not* presumed to be a venue.

    The composition root overrides this explicitly
    (``ScannerEngine(min_call_interval=...)``), so this is the default for
    a directly-constructed engine, not the authority.
    """
    if type(market).__name__ in _LOCAL_MARKET_PROVIDERS:
        return 0.0
    return DEFAULT_SCANNER_CALL_INTERVAL


#: The name this had when it was first added.  Kept because it reads well
#: at the call site; the behaviour is identical.
default_call_interval = scanner_call_interval


def warmup_bars(conditions: Iterable[Condition]) -> int:
    """Bars an instrument must hold before every condition in *conditions* can match.

    Derived from the longest indicator period the conditions declare, plus
    ``_WARMUP_MARGIN_BARS``.  It was a flat 30 before, which silently
    truncated any longer period into a permanently empty history.

    Deliberately conservative.  It takes the largest ``int`` the indicator's
    registry spec declares (or the condition supplies), which over-reads the
    few params that are not periods (a table width) and under-reads chained
    ones (a smoothing stage is a sum, not a max).  Over-reading costs a few
    bars of buffer; under-reading is covered by the other half of this lane —
    an indicator that is still not warm returns no match rather than a number.
    """
    longest = max((_declared_period(c.name, c.params) for c in conditions), default=0)
    return longest + _WARMUP_MARGIN_BARS


def _declared_period(name: str, params: Mapping[str, Any]) -> int:
    """Longest int period the indicator *name* declares, given ``params``."""
    from tradex_analytics.indicators import get_indicator_spec

    spec = get_indicator_spec(name.lower())
    if spec is None:
        # A raw price field (``close``), not an indicator: it declares no
        # period at all, and the margin alone is its whole warmup.
        return 0
    declared = [
        default
        for _param, kind, default in spec.params
        if kind == "int" and isinstance(default, int) and not isinstance(default, bool)
    ]
    supplied = [v for v in params.values() if isinstance(v, int) and not isinstance(v, bool)]
    return max(declared + supplied, default=0)


class ScannerEngine:
    """Evaluates ``Condition`` values over a default D1 window per instrument.

    Supports two data paths:

    * **Snapshot** (legacy): ``_history()`` fetches from the market provider
      on every scan.  Still used as fallback when no bars have been streamed.
    * **Streaming** (G7): call ``consume(candle)`` to append bars into a
      per-instrument rolling buffer.  Once the buffer has data, ``_history()``
      returns it directly — no market round-trip.

    The streaming buffer is sized from the definition it is asked to evaluate
    (:func:`warmup_bars`), not from a constant: a buffer shorter than an
    indicator's warmup is what turns "unknown" into a value that passes a
    threshold, and no caller-supplied ``max_bars`` can be small enough to
    reintroduce that.

    **Rate limit.** Every provider read goes through :class:`ScanRateLimiter`
    before it is issued, so a scan over a large universe cannot fire one call
    per symbol back to back at a provider that is shared with the live feed.
    See :func:`default_call_interval` for when that applies.

    **Result cache.** :meth:`run` memoizes on a key derived from the scan
    itself and the state it was computed against; see :meth:`_cache_key` for
    exactly what is in it and why.

    **Portfolio context.** :meth:`set_portfolio` lets a caller tell the
    scanner what is already held, and :meth:`_annotate` carries that on
    every result.  A scan returns the same twenty symbols whether you are
    flat or maxed out, which is the defect this answers.
    """

    def __init__(
        self,
        market: Any,
        analytics: IndicatorComputer | None = None,
        window_days: int = 30,
        max_bars: int | None = _DEFAULT_MAX_BARS,
        *,
        min_call_interval: float | None = None,
        cache_ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS,
        cache_max_entries: int = _DEFAULT_CACHE_MAX_ENTRIES,
        limiter: ScanRateLimiter | None = None,
    ) -> None:
        self._market = market
        if analytics is None:
            from tradex_analytics.engine import AnalyticsEngine
            analytics = AnalyticsEngine()  # type: ignore[assignment]
        self._analytics: IndicatorComputer = analytics  # type: ignore[assignment]
        self._window_days = window_days
        # Buffer size until the first definition run sizes it from the warmup.
        self._buffer_cap = _DEFAULT_MAX_BARS if max_bars is None else max_bars
        self._sized = False
        # Per-instrument rolling buffer keyed by instrument symbol.
        self._buffers: dict[str, deque[Candle]] = defaultdict(self._new_buffer)
        # AC-13 rate limit.  ``None`` means "decide from the provider",
        # which is the wiring-aware answer; an explicit 0.0 opts out.
        self._limiter = limiter if limiter is not None else ScanRateLimiter(
            min_interval=(
                scanner_call_interval(market)
                if min_call_interval is None
                else max(0.0, float(min_call_interval))
            )
        )
        self._cache: ScanResultCache[ScannerResult] = ScanResultCache(
            max_entries=cache_max_entries,
            ttl_seconds=cache_ttl_seconds,
        )
        # Bumped by every ``consume`` — one streamed bar can change what a
        # scan returns, and the buffers are rolling deques whose dropped
        # bars are not cheaply re-derivable into a key.  It is in the cache
        # key so a new bar makes every prior entry unreachable rather than
        # relying on a TTL to eventually notice.
        #
        # ``set_portfolio`` does NOT bump it.  The portfolio is not a stream
        # of deltas, it is a value: the caller re-reads the whole book each
        # request and hands over an equal snapshot most of the time, and a
        # counter bumped per call would turn every scan into a miss and the
        # cache into dead code.  The portfolio's *content* is in the key
        # instead, which is the honest signal: a changed book changes the
        # digest, an unchanged one does not.
        self._bar_version = 0
        self._portfolio: PortfolioSnapshot | None = None

    def _new_buffer(self) -> deque[Candle]:
        return deque(maxlen=self._buffer_cap)

    def _size_buffers(self, bars: int) -> None:
        """Size every buffer to a *bars* bar warmup, from the first run on.

        The first definition run replaces the pre-scan storage bound: from
        then on the definition decides how many bars matter, so the derived
        number is the buffer size rather than a floor under a constant.  Later
        runs only grow it — a bar the deque has already dropped is not
        recoverable, and a shorter requirement must not shrink a buffer another
        definition has already sized.
        """
        if self._sized and bars <= self._buffer_cap:
            return
        for key in list(self._buffers):
            self._buffers[key] = deque(self._buffers[key], maxlen=bars)
        self._buffer_cap = bars
        self._sized = True

    # -- streaming entry point ---------------------------------------------------

    def consume(self, candle: Candle) -> None:
        """Append *candle* to the per-instrument rolling buffer.

        This is the public entry point for the bar stream (bus subscription,
        aggregator callback, etc.).  The buffer is bounded by ``max_bars``
        until the first definition run resizes it to that definition's warmup
        (and grows for a later, longer one); oldest bars are dropped
        automatically by the underlying ``deque``.

        Also invalidates the result cache.  A streamed bar can change what a
        scan returns — a new D1 close, or a buffer that has just become long
        enough for an indicator to warm — and a cache that kept serving the
        pre-bar answer would report a setup the book has already moved past.
        """
        key = _instrument_key(candle.instrument)
        self._buffers[key].append(candle)
        self._bar_version += 1

    def set_portfolio(self, portfolio: PortfolioSnapshot | None) -> None:
        """Tell the scanner what the book currently holds.

        Without this, a scan is a vacuum: it cannot tell a fresh entry from
        one that doubles a position the account is already maxed into.  The
        portfolio is not used to *filter* results — the caller decides what
        to do with a candidate, and hiding one is not this engine's call —
        but every result comes back annotated with what is already held, so
        the decision has the context it needs.

        The stored object is copied, so a snapshot the caller keeps mutating
        cannot change what a cached result was annotated with.  A closed
        position is dropped: it is not a holding, and leaving flat symbols in
        would report the book as holding something it has exited.

        Setting the same book twice is a no-op for the cache: the snapshot's
        content is in the key, so an identical rebuild produces an identical
        key and still hits.
        """
        self._portfolio = _copy_portfolio(portfolio)

    @property
    def portfolio(self) -> PortfolioSnapshot | None:
        """The portfolio context results are annotated against."""
        return self._portfolio

    @property
    def bar_version(self) -> int:
        """Counter bumped by :meth:`consume` on every streamed bar."""
        return self._bar_version

    def invalidate_cache(self) -> int:
        """Drop every cached scan. Returns how many entries were removed."""
        return self._cache.invalidate()

    @property
    def cache_hits(self) -> int:
        return self._cache.hits

    @property
    def cache_misses(self) -> int:
        return self._cache.misses

    @property
    def throttled_calls(self) -> int:
        """Provider reads the rate limit actually delayed."""
        return self._limiter.throttled_calls

    def run(self, definition: ScannerDefinition) -> list[ScannerResult]:
        """Evaluate all conditions and return the ranked matches.

        An instrument that matched nothing is not returned: a scan whose
        result set is the whole universe is indistinguishable from a scan that
        found candidates everywhere, which is how an unwarmed indicator used
        to report success.  A definition with no conditions has nothing to
        match, so every instrument is returned.

        A repeat of the same scan against unchanged state is served from the
        result cache; see :meth:`_cache_key` for what makes a repeat
        legitimate.
        """
        key = self._cache_key(definition)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        results = self._scan(definition)
        self._cache.set(key, results)
        return results

    def _scan(self, definition: ScannerDefinition) -> list[ScannerResult]:
        conditions = definition.conditions
        self._size_buffers(warmup_bars(conditions))
        results: list[ScannerResult] = []
        for instrument in definition.universe:
            series = self._history(instrument)
            result = self._evaluate(instrument, series, conditions)
            if conditions and not result.matched_conditions:
                continue
            results.append(result)
        results.sort(key=lambda r: (-r.score, r.instrument.symbol))
        ranked = [
            ScannerResult(
                instrument=r.instrument,
                score=r.score,
                matched_conditions=r.matched_conditions,
                indicator_values=r.indicator_values,
                rank=i + 1,
                metadata=self._annotate(r),
            )
            for i, r in enumerate(results)
        ]
        return ranked

    def _cache_key(self, definition: ScannerDefinition) -> str:
        """Digest of everything that can change what this scan returns.

        Enumerated, with the reason each is in:

        ``universe``  the set of instruments evaluated
        ``conditions``  name/params/operator/threshold per condition — the
            whole of what is being tested
        ``window_days``  the span the snapshot path asks the provider for, so
            a 5-day scan cannot be served a 30-day scan's answer
        ``timeframe``   the bars are read at; D1 today, named rather than
            assumed so changing it cannot silently widen the key
        ``bar_version``  bumped by ``consume``.  This is the invalidation
            rule for streamed data: it makes a pre-bar entry unreachable, so
            a new D1 bar never serves a pre-close answer.
        ``portfolio``  the book's *content*.  This is the invalidation rule
            for holdings: a different book is a different digest and must
            miss; an identical one is the same digest and must hit, which is
            what makes the cache survive a caller that re-reads the book on
            every request.  Only what the annotations actually read is
            hashed — instrument key and quantity — not the whole snapshot,
            so an account-equity tick that changes nothing a result reports
            does not invalidate the scan.

        Deliberately **not** in the key: the analytics engine instance, the
        market provider instance, the limiter, and the buffer contents.  The
        first three are construction detail that does not change an answer,
        and the fourth is already represented by ``data_version``, which
        ``consume`` bumps on every append — putting the bars in the key as
        well would just hash every bar of a 500-symbol universe on every
        request to learn something the counter already says.

        The TTL is the backstop for the one case a key cannot express: the
        snapshot path's window is anchored to ``datetime.now(UTC)``, so with
        no new bar streamed the key is identical while the data underneath
        moves on.
        """
        return _fingerprint(
            {
                "universe": [_instrument_key(i) for i in definition.universe],
                "conditions": [
                    [c.name, c.params, c.operator, c.threshold] for c in definition.conditions
                ],
                "window_days": self._window_days,
                "timeframe": str(Timeframe.D1),
                "bar_version": self._bar_version,
                "portfolio": _portfolio_key(self._portfolio),
            }
        )

    def top(self, definition: ScannerDefinition, limit: int = 20) -> list[ScannerResult]:
        """Return the top-*limit* results from ``run()``."""
        return self.run(definition)[:limit]

    # -- internals ---------------------------------------------------------------

    def _history(self, instrument: Any) -> HistoricalSeries:
        key = _instrument_key(instrument)
        buf = self._buffers.get(key)
        if buf:
            # Streaming path — return buffered candles directly.  No provider
            # call, so the rate limit does not apply (and must not: pacing
            # an in-memory read would only slow a scan down).
            candles = list(buf)
            return HistoricalSeries(
                instrument=instrument,
                timeframe=Timeframe.D1,
                candles=candles,
                start=candles[0].timestamp,
                end=candles[-1].timestamp,
            )
        # Snapshot fallback — no bars streamed yet; hit the market provider.
        # This is the scanner's only outbound path, so this is the only place
        # the rate limit has to sit.  Paced *before* the call, not after, so
        # the throttled read is the one that waits.
        end = datetime.now(UTC)
        start = end - timedelta(days=self._window_days)
        if self._limiter.min_interval > 0.0:
            # ``acquire`` blocks until the slot is free; it cannot fail.  If
            # a future limiter could refuse, refusing here raises inside the
            # ``SDKError`` handler below and degrades that one symbol to an
            # empty series, which is the contract the snapshot path already
            # has for a provider read that raises.
            self._limiter.acquire()
        try:
            return self._market.history(instrument, Timeframe.D1, start, end)
        except (SDKError, NotImplementedError):
            # ``NotImplementedError`` is not an ``SDKError``: the paper broker
            # raises ``PaperHistoryNotImplemented`` (C5d) rather than fabricate
            # an empty series, which is right for a caller that would
            # otherwise read "no data" as "the market was shut". A screener is
            # a different kind of caller — it asks "does anything score here?"
            # and an empty answer is a legitimate one, not a wrong window. So
            # the refusal is absorbed here and degrades that one symbol to an
            # empty series, which is the same contract the SDKError branch
            # already had.
            return HistoricalSeries(
                instrument=instrument,
                timeframe=Timeframe.D1,
                candles=[],
                start=start,
                end=end,
            )

    def _annotate(self, result: ScannerResult) -> dict[str, Any]:
        """Attach portfolio context to one result.

        ``held_quantity`` is the signed quantity the book already carries in
        this instrument (0 when flat, negative when short) and
        ``in_portfolio`` says whether that is a real position.  Both are
        reported per result rather than as one book-level total because the
        question a screener is actually asked is "do I already own this?".

        With no portfolio supplied this returns the empty dict the result
        already had, so an engine that is never told about the book behaves
        exactly as it did before.
        """
        portfolio = self._portfolio
        if portfolio is None:
            return result.metadata
        held = _held_quantity(portfolio, result.instrument)
        return {
            **result.metadata,
            "held_quantity": held,
            "in_portfolio": held != 0,
        }

    def _evaluate(
        self,
        instrument: Any,
        series: HistoricalSeries,
        conditions: list[Condition],
    ) -> ScannerResult:
        values = {cond.name: self._value(series, cond.name, cond.params) for cond in conditions}
        matched = [cond.name for cond in conditions if self._matches(cond, values.get(cond.name))]
        score = len(matched) / len(conditions) if conditions else 1.0
        return ScannerResult(
            instrument=instrument,
            score=score,
            matched_conditions=matched,
            # An unwarmed indicator is unknown, not 0.0.  It is left out of
            # the values rather than reported as a number, because 0.0 is a
            # value that passes a "below" threshold: an unwarmed ``rsi`` read
            # as 0.0 matched every instrument in the universe.
            indicator_values={k: float(v) for k, v in values.items() if v is not None},
            rank=0,  # assigned by run()
        )

    def _value(self, series: HistoricalSeries, name: str, params: dict[str, Any]) -> float | None:
        """Tail value of *name*, or ``None`` when it has not warmed up.

        ``None`` means unknown, and unknown must never be handed to a
        threshold comparison — ``_matches`` treats it as no match.
        """
        if not series.candles:
            return None
        if name == "close":
            return float(series.candles[-1].ohlc.close.value)
        # Raw-values surface: signed indicators (roc, macd) are legitimate
        # condition inputs but cannot ride the Price-typed close of a
        # HistoricalSeries. Prefer indicator_values when the engine provides
        # it; fall back to close-wrapping for engines that only implement the
        # base protocol (their price-ranged indicators still work).
        get_values = getattr(self._analytics, "indicator_values", None)
        if callable(get_values):
            values = cast(
                "list[float | None]",
                get_values(series, name, **params),
            )
            tail = next((v for v in reversed(values) if v is not None), None)
            return None if tail is None else float(tail)
        indicator = cast(HistoricalSeries, self._analytics.indicator(series, name, **params))
        if not indicator.candles:
            return None
        return float(indicator.candles[-1].ohlc.close.value)

    @staticmethod
    def _matches(cond: Condition, value: float | None) -> bool:
        if value is None or cond.threshold is None:
            return False
        op = _OPS.get(cond.operator)
        if op is None:
            return False
        return bool(op(value, cond.threshold))


def _instrument_key(instrument: Any) -> str:
    """Stable hashable key for per-instrument buffer lookup."""
    return getattr(instrument, "symbol", None) or str(instrument)


def _portfolio_key(portfolio: PortfolioSnapshot | None) -> Any:
    """The part of *portfolio* that can change a result.

    Deliberately narrower than the snapshot: the annotations read only the
    per-instrument held quantity, so that is all that is keyed.  Hashing
    the whole ``PortfolioSnapshot`` would fold in account equity and
    unrealized P&L, which move on every mark and would invalidate the scan
    on every request for a field no result reports — the too-wide key.

    Sorted so the order positions arrive in (broker order is not stable)
    cannot itself cause a miss.
    """
    if portfolio is None:
        return None
    return sorted(
        (
            _instrument_key(p.instrument),
            str(getattr(p.instrument, "exchange", "")),
            str(p.quantity.value),
        )
        for p in portfolio.positions
    )


def _held_quantity(portfolio: PortfolioSnapshot, instrument: Any) -> float:
    """Signed quantity the book holds in *instrument*; 0 when flat.

    Matched on ``(exchange, symbol)``, not on object identity: the scan's
    universe and the OMS cache each build their own ``Equity``, so an
    identity match would report every position as "not held" — the same
    class of bug the instrument key above already avoids for buffers.
    """
    key = _instrument_key(instrument)
    exchange = getattr(instrument, "exchange", None)
    for position in portfolio.positions:
        if _instrument_key(position.instrument) != key:
            continue
        held_exchange = getattr(position.instrument, "exchange", None)
        if (
            exchange is not None
            and held_exchange is not None
            and held_exchange != exchange
        ):
            continue
        return float(position.quantity.value)
    return 0.0


def _copy_portfolio(portfolio: PortfolioSnapshot | None) -> PortfolioSnapshot | None:
    """Copy *portfolio*, dropping positions with no quantity.

    A copy, because the engine holds this across cached results: a caller
    mutating the list it passed in must not retroactively change what an
    already-cached result was annotated with.

    Flat positions are dropped rather than kept.  A closed position in a
    broker's holdings response has a zero quantity, and reporting it would
    make ``in_portfolio`` true for a symbol the book exited — the scan
    would then be telling a user they hold something they do not.
    """
    if portfolio is None:
        return None
    return PortfolioSnapshot(
        positions=[p for p in portfolio.positions if p.quantity.value != 0],
        account=portfolio.account,
    )


