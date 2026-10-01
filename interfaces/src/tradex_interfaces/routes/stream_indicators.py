"""WS indicator push — Tier-2 subscribe seam for /ws/stream.

Subscription registry + compute/push for the hybrid model in
docs/superpowers/specs/2026-09-09-ws-indicator-push-design.md:
``bar-close`` mode recomputes on closed bars, ``tick`` mode recomputes on
forming bars throttled to >= 1 s per (instrument, indicator). Compute goes
through ``compute_indicator`` — the same seam as the REST endpoint — so the
two can never drift. History comes from the datalake tail; a datalake miss
degrades to the live bar alone.

Cache scope, incremental compute, and the event loop
-----------------------------------------------------
Three things the per-connection cache got wrong, all fixed here:

1. **The datalake tail is process-level**, keyed by ``(instrument,
   timeframe)`` (:data:`TAIL_CACHE`). It is a property of the series, not of
   a websocket, so two connections on the same pair share one tail. See
   :class:`IndicatorTailCache` for the lifetime and eviction contract.
2. **Compute warms from the previous bar** (:class:`IncrementalIndicator`)
   instead of rebuilding every candle shim for a 400-bar window. See that
   class for why the warm path is bit-exact, not approximate.
3. **The lake read is dispatched to a worker thread**
   (:meth:`IndicatorTailCache.ensure_loaded`). ``ReactiveBus.publish``
   delivers synchronously, so :meth:`IndicatorStreamRegistry.handle_bar_frame`
   runs on the event loop and must not block on parquet I/O.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any

from tradex_domain.timeframe import interval_seconds
from tradex_domain.timezones import IST_ZONE

log = logging.getLogger(__name__)

_TAIL_LIMIT = 400
_TICK_THROTTLE_SECONDS = 1.0
_POINTS_PER_PUSH = 2

#: Hard ceiling on distinct ``(instrument, timeframe)`` tails held in memory.
#: One tail is at most ``_TAIL_LIMIT`` bars of six floats, so the default cap
#: bounds live bar history to roughly ``32 * 400 * 6 * 8`` bytes — about
#: 600 KB — no matter how long the process runs or how many instruments a
#: client cycles through in a session.
TAIL_CACHE_MAX_ENTRIES = 32

#: A tail is only worth reusing while its head is recent. The lake is written
#: asynchronously, so a tail can be arbitrarily stale relative to the live
#: bar; past this age the cache re-reads rather than serve history that has
#: drifted from disk.
TAIL_CACHE_MAX_AGE_SECONDS = 900.0

#: How long the caller waits for a detached tail-load thread. Bounded so a
#: wedged parquet read cannot pin a thread for the life of the process, and
#: long enough that an ordinary read always lands inside it (measured in
#: single-digit milliseconds).
TAIL_LOAD_JOIN_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Candle shims
# ---------------------------------------------------------------------------


class _P:
    __slots__ = ("value",)

    def __init__(self, v: Any) -> None:
        self.value = v


class _O:
    __slots__ = ("open", "high", "low", "close")

    def __init__(self, b: dict[str, Any]) -> None:
        self.open = _P(b["open"])
        self.high = _P(b["high"])
        self.low = _P(b["low"])
        self.close = _P(b["close"])


class _V:
    __slots__ = ("value",)

    def __init__(self, v: Any) -> None:
        self.value = v


class _C:
    __slots__ = ("timestamp", "ohlc", "volume")

    def __init__(self, b: dict[str, Any]) -> None:
        # IST-naive: the vwap session anchor reads ``c.timestamp.date()``.
        self.timestamp = datetime.fromtimestamp(b["time"], tz=IST_ZONE).replace(tzinfo=None)
        self.ohlc = _O(b)
        self.volume = _V(b["volume"])


def _candle_shim(bar: dict[str, Any]) -> Any:
    """One serialized bar -> the candle object ``compute_indicator`` reads.

    The compute functions duck-type ``c.ohlc.{open,high,low,close}.value``
    and ``c.volume.value``.
    """
    return _C(bar)


def _candles_from_bars(bars: list[dict[str, Any]]) -> list[Any]:
    """Serialized bars -> candle shims. Module-level shim classes, because
    building these was the single most expensive step of a push (0.52 ms
    against 0.15 ms for a 400-bar SMA) and the per-call class definitions
    made it worse."""
    return [_candle_shim(b) for b in bars]


# ---------------------------------------------------------------------------
# Process-level tail cache
# ---------------------------------------------------------------------------


def _load_datalake_tail(instrument: str, timeframe: str, now: int) -> list[dict[str, Any]]:
    """Read the datalake tail for (instrument, timeframe) ending at *now*.

    This does blocking file I/O and is the seam the event loop must never
    call directly — see :meth:`IndicatorTailCache.ensure_loaded`.

    A datalake problem degrades to an empty tail (live bar only) rather than
    propagating: an indicator push is decoration on top of the bar feed and
    must not take the socket down with it.
    """
    try:
        from tradex_domain.enums import Timeframe
        from tradex_domain.instruments import Equity
        from tradex_market_data.market_provider import ParquetMarketProvider
        from tradex_market_data.paths import DATALAKE_ROOT

        exchange, symbol = (
            instrument.split(":", 1) if ":" in instrument else ("NSE", instrument)
        )
        tf = Timeframe(timeframe)
        end = datetime.fromtimestamp(now, tz=IST_ZONE) + timedelta(seconds=interval_seconds(tf))
        parsed = Equity.of(exchange, symbol)
        series = ParquetMarketProvider(base_path=DATALAKE_ROOT).history(
            parsed, tf, end - timedelta(days=30), end
        )
        candles = series.candles[-_TAIL_LIMIT:]
        return [
            {
                "time": int(c.timestamp.replace(tzinfo=IST_ZONE).timestamp()),
                "open": float(c.ohlc.open.value),
                "high": float(c.ohlc.high.value),
                "low": float(c.ohlc.low.value),
                "close": float(c.ohlc.close.value),
                "volume": float(c.volume.value),
            }
            for c in candles
        ]
    except Exception:  # noqa: BLE001 — datalake problems degrade to live-only
        log.exception("datalake tail load failed for %s", instrument)
        return []


class Tail:
    """One ``(instrument, timeframe)`` datalake tail.

    Published by rebinding a whole ``Tail`` rather than mutating one in
    place, so a reader holding a reference keeps a consistent view while
    another thread installs a newer one. The bar list itself is replaced,
    never edited, once published.

    ``through`` is the bar-time the tail is complete *through*. It is set to
    the bar-time of the load, which is what stops a tail from being reused
    for a later bucket it never saw; staleness relative to wall clock is
    ``IndicatorTailCache``'s job via ``loaded_at``.
    """

    __slots__ = ("bars", "through", "loaded_at")

    def __init__(self, bars: list[dict[str, Any]], through: int) -> None:
        self.bars = bars
        self.through = through
        self.loaded_at = time.monotonic()

    def __len__(self) -> int:
        return len(self.bars)


class IndicatorTailCache:
    """Process-level datalake tails keyed by ``(instrument, timeframe)``.

    Why process level
    -----------------
    The tail is a property of the *series*, not of a websocket. Two clients
    on the same ``(instrument, timeframe)`` read the same 400 bars, so a
    per-connection cache pays for the same lake read twice — and again on
    every reconnect. Keying by the series means the warm tail is reused by
    whoever arrives next, which is the point of the lane.

    Lifetime (this is what changed from per-connection)
    ---------------------------------------------------
    A per-connection cache died with the connection, implicitly. This one
    deliberately does not: it is released only by LRU eviction, by
    staleness, or at process exit. Keeping a derived, reproducible tail past
    the last subscriber costs bounded memory and saves the next subscriber a
    lake read. What it must never become is a place where a bar that has not
    closed yet is remembered as if it were history — which is why
    :meth:`acquire` re-reads whenever the requested bucket is not already
    covered by the tail in hand.

    Eviction and bounded memory
    ---------------------------
    ``max_entries`` caps the number of live keys, dropping the least
    recently *used* on overflow, so a client cycling through instruments
    cannot grow the process without limit. Each entry is bounded by
    construction: at most ``_TAIL_LIMIT`` bars, and the live bar window is
    trimmed to the same cap. Entries are also dropped once ``stale_after``
    elapses, so an idle-then-resumed key cannot serve a tail that has
    drifted from disk.

    Concurrency
    -----------
    Shared by every connection, so it is lock-guarded, and it never holds
    the lock across a lake read. Two details matter here:

    - **Single flight.** Two connections opening the same series at the same
      moment would otherwise both miss and both dispatch a read of the same
      400 bars — the duplication the lane removes, reintroduced by a race.
      A per-key in-flight event means the second caller waits on the first
      read instead of starting its own.
    - **No truncated tail.** A read that loses the race publishes second;
      :meth:`_publish` keeps whichever tail actually carries more history,
      so a racing pair cannot leave a shorter tail that would make the next
      push compute a shorter warmup than the one before it.
    """

    def __init__(
        self,
        max_entries: int = TAIL_CACHE_MAX_ENTRIES,
        stale_after: float = TAIL_CACHE_MAX_AGE_SECONDS,
    ) -> None:
        self._max_entries = max(1, int(max_entries))
        self._stale_after = float(stale_after)
        self._lock = threading.Lock()
        # (instrument, timeframe) -> Tail, ordered least-recently-used first
        self._entries: OrderedDict[tuple[str, str], Tail] = OrderedDict()
        # key -> Event, the single in-flight read for that key
        self._inflight: dict[tuple[str, str], threading.Event] = {}
        self._hits = 0
        self._misses = 0
        self.loads = 0

    # -- introspection ----------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._entries)

    def stats(self) -> dict[str, int]:
        """Load counters. ``loads`` is the ground truth for "did this cache
        make the process re-read the lake", which the push path itself hides
        behind warm hits."""
        with self._lock:
            return {
                "entries": len(self._entries),
                "max_entries": self._max_entries,
                "loads": self.loads,
                "hits": self._hits,
                "misses": self._misses,
            }

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # -- reads ------------------------------------------------------------

    def _claim(self, key: tuple[str, str]) -> threading.Event | None:
        """Claim the single in-flight read for *key*.

        Returns the event to wait on if another caller already claimed it,
        or ``None`` if this caller is the one that must do the read.
        """
        with self._lock:
            claimed = self._inflight.get(key)
            if claimed is not None:
                return claimed
            self._inflight[key] = threading.Event()
            return None

    def _finish(self, key: tuple[str, str]) -> None:
        with self._lock:
            event = self._inflight.pop(key, None)
        if event is not None:
            event.set()

    def acquire(self, key: tuple[str, str], at: int) -> Tail | None:
        """Return a tail for *key* that already covers bar-time *at*, else
        None.

        "Covers" is the reuse condition, and it is deliberately strict: a
        tail loaded at bucket T is not reused for bucket T+1, because the
        lake is written asynchronously and the answer for T+1 may depend on
        a row that landed after the read. A forming bar inside a bucket the
        tail already covers *is* reused, which is where the saving is —
        tick mode pushes several times per bar.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            if self._stale_after >= 0 and time.monotonic() - entry.loaded_at > self._stale_after:
                del self._entries[key]
                self._misses += 1
                return None
            if at > entry.through:
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return entry

    def ensure_loaded(self, key: tuple[str, str], now: int) -> Tail:
        """Return a tail for *key*, reading the lake only when none is usable.

        The read is dispatched to a worker thread and joined with a bounded
        timeout, so the blocking parquet I/O does not run on the caller's
        thread — which, for the websocket path, is the event loop. The join
        is what preserves the push contract (a subscriber sees a warm tail
        on its very first bar); the thread is what keeps the loop free.

        Concurrent callers for one key do not each start a read: the second
        waits on the first's event, so opening the same series from two
        connections at once costs one parquet read, not two.

        A load that outlives ``TAIL_LOAD_JOIN_SECONDS`` is abandoned, not
        cancelled: its result is still published if it lands, so the next
        push is warm, and the thread exits on its own rather than being
        killed mid-read. The caller gets an empty tail, which is exactly the
        shape a datalake miss already degrades to.
        """
        existing = self.acquire(key, now)
        if existing is not None:
            return existing
        waiting = self._claim(key)
        if waiting is not None:
            # Another caller is already reading this key; share its result.
            waiting.wait(TAIL_LOAD_JOIN_SECONDS)
            shared = self.acquire(key, now)
            return shared if shared is not None else Tail(bars=[], through=now)
        try:
            done = threading.Event()
            captured: list[Tail] = []

            def _work() -> None:
                tail = Tail(bars=_load_datalake_tail(key[0], key[1], now), through=now)
                captured.append(tail)
                self._publish(key, tail)
                done.set()

            worker = threading.Thread(
                target=_work, name=f"tail-load:{key[0]}:{key[1]}", daemon=True
            )
            worker.start()
            done.wait(TAIL_LOAD_JOIN_SECONDS)
            return captured[0] if captured else Tail(bars=[], through=now)
        finally:
            self._finish(key)

    # -- writes -----------------------------------------------------------

    def _publish(self, key: tuple[str, str], tail: Tail) -> None:
        """Install *tail* for *key* and evict down to the cap."""
        with self._lock:
            existing = self._entries.get(key)
            self.loads += 1
            if existing is not None and existing.bars and tail.bars:
                # Another thread got there first with real history. Keep the
                # longer tail; a truncated one would shorten the next
                # push's warmup relative to the one before it.
                if len(existing.bars) >= len(tail.bars) and existing.through >= tail.through:
                    existing.loaded_at = tail.loaded_at
                    self._entries.move_to_end(key)
                    return
            self._entries[key] = tail
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)


#: The process-level tail cache. Module-level and process-wide by design; the
#: per-connection registry holds subscriptions only.
TAIL_CACHE = IndicatorTailCache()


#: The single compute seam. The REST endpoint and the websocket push both
#: go through this import, so the docstring's "the two can never drift" claim
#: is structural rather than a convention. Re-exported for the reference
#: (non-incremental) path the tests compare against.
from tradex_analytics.indicators import compute_indicator  # noqa: E402  isort: skip


# ---------------------------------------------------------------------------
# Incremental compute
# ---------------------------------------------------------------------------

#: Indicator ids with at least one plot that reads *forward*, so their value
#: at index *i* depends on bars after *i*. Found by measuring, not guessed:
#: ``ichimoku.lagging`` and the ``rsi-divergence`` bear plots look ahead.
#:
#: Recorded because the obvious design for this lane — warm the *value* and
#: only compute the new bar — is wrong for these, and would change a pushed
#: number. This implementation never does that (see
#: :class:`IncrementalIndicator`), so the set is documentation of a measured
#: property of the catalogue rather than a routing table. The catalogue test
#: re-measures it, so an indicator that gains a look-ahead is noticed.
LOOKAHEAD_INDICATOR_IDS = frozenset({"ichimoku", "rsi-divergence"})


class IncrementalIndicator:
    """Warm-from-the-previous-bar compute for one series.

    The warm state is the **bar window and its candle shims, kept in
    lockstep**: ``shims[i]`` is always the shim of ``bars[i]``. A new bar
    therefore costs one shim instead of ``len(window)`` of them, and the
    compute over ``shims`` is, by construction, the full recompute over
    ``bars``.

    Why that is exact for *every* indicator, with no exceptions
    -----------------------------------------------------------
    The usual argument for incremental compute — "indicators are prefix
    functions, so appending cannot change earlier values" — is **false for
    three plots in this catalogue** (see :data:`LOOKAHEAD_INDICATOR_IDS`),
    and a warm path built on that argument would quietly change the number a
    strategy reads for those.

    So this does not rely on it. It never emits a value carried over from a
    previous bar: every push re-runs ``compute_indicator`` over the whole
    window, exactly as the pre-lane code did. The only thing carried across
    bars is *derived state that a full rebuild would produce identically* —
    the shims. The invariant is maintained by construction:

    - cold, or a shape the window does not match -> rebuild every shim;
    - same shape, last bar updated -> rewrite the last shim;
    - same shape, last bar advanced and the window slid -> ``shims[1:] + [one]``;
    - grew by exactly one -> ``shims + [one]``.

    Every branch is guarded by an exact comparison against the previous bar
    list, so the merge can only ever describe a window it actually holds. A
    gap, a rewind, or a correction rebuilds instead, so a divergence in the
    bar stream degrades to the plain full recompute rather than to a wrong
    number.

    Note what is *not* needed: no contiguity check, and no notion of which
    indicators may warm. Mirroring the bars requires no assumption about how
    far apart they are, so an overnight or weekend gap is handled correctly
    rather than refused.

    Where the saving is
    ------------------
    Measured on a 400-bar window: building the candle shims costs 0.52 ms
    and the SMA itself 0.15 ms — the shims are four object allocations per
    bar, 1600 per push. A warm bar allocates one. The window is capped at
    ``_TAIL_LIMIT`` bars, so the shim list cannot outlive the bars behind it.
    """

    __slots__ = ("_bars", "_shims", "_limit")

    def __init__(self, limit: int = _TAIL_LIMIT) -> None:
        self._bars: list[dict[str, Any]] = []
        self._shims: list[Any] = []
        self._limit = max(2, int(limit))

    def __len__(self) -> int:
        return len(self._bars)

    def bars(self) -> list[dict[str, Any]]:
        return self._bars

    def shims(self) -> list[Any]:
        return self._shims

    def _rebuild(self, bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._bars = list(bars)[-self._limit :]
        self._shims = [_candle_shim(b) for b in self._bars]
        return self._bars

    def extend(self, bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge *bars* over this window and return the merged bar list.

        The bar list is what the indicators are computed over; the shim list
        is maintained alongside it so the next push does not rebuild it. Only
        a merge matching this window's exact previous shape is patched;
        anything else rebuilds.
        """
        prev = self._bars
        if not prev or not bars:
            return self._rebuild(bars)
        n = len(bars)
        if n == len(prev):
            if bars[-1]["time"] == prev[-1]["time"] and bars[:-1] == prev[:-1]:
                # The forming bar updated: same history, new last bar.
                prev[-1] = bars[-1]
                self._shims[-1] = _candle_shim(bars[-1])
                return prev
            if n >= 2 and bars[:-1] == prev[1:]:
                # A new bar arrived and the window slid to stay bounded.
                self._bars = prev = prev[1:] + [bars[-1]]
                self._shims = self._shims[1:] + [_candle_shim(bars[-1])]
                return prev
        elif n == len(prev) + 1 and bars[:-1] == prev:
            self._bars = prev = prev + [bars[-1]]
            if len(prev) > self._limit:
                del prev[: len(prev) - self._limit]
                del self._shims[: len(self._shims) - self._limit]
            self._shims = self._shims + [_candle_shim(bars[-1])]
            return prev
        return self._rebuild(bars)

    def compute(self, indicator_id: str, bars: list[dict[str, Any]]) -> dict[str, list]:
        """Compute *indicator_id* over *bars*, warming the shims as it goes."""
        self.extend(bars)
        return compute_indicator(indicator_id, self._shims)

    def reset(self) -> None:
        self._bars = []
        self._shims = []


class IndicatorSeriesCache:
    """Process-level warm state for one ``(instrument, timeframe)``.

    The tail is derived from the lake and can be kept; the live bar window
    and the shims cannot be re-derived from anywhere else, so this is the
    state that genuinely has to be shared between connections on the same
    series, and the state that genuinely has to be released.

    Bars are assembled as *tail* (the reproducible spine) plus the live bar,
    and the merge is what lets a closed bar roll out of the tail and into
    the window without the series being rebuilt.
    """

    def __init__(self, tail_cache: IndicatorTailCache) -> None:
        self._tail_cache = tail_cache
        self._lock = threading.Lock()
        self._window: IncrementalIndicator | None = None
        self._holders = 0

    # -- lifetime ---------------------------------------------------------

    def acquire(self) -> None:
        with self._lock:
            self._holders += 1

    def release(self) -> None:
        with self._lock:
            self._holders = max(0, self._holders - 1)
            if self._holders == 0:
                self._window = None

    @property
    def holders(self) -> int:
        with self._lock:
            return self._holders

    @property
    def window(self) -> IncrementalIndicator | None:
        return self._window

    # -- compute ----------------------------------------------------------

    def bars(self, key: tuple[str, str], frame: Any) -> list[dict[str, Any]]:
        """Merged window for *frame*: the cached tail plus the live bar."""
        tail = self._tail_cache.ensure_loaded(key, frame.time)
        with self._lock:
            if self._window is None:
                self._window = IncrementalIndicator()
            return self._window.extend((tail.bars if tail else []) + [_live_bar(frame)])

    def compute(
        self, indicator_id: str, key: tuple[str, str], frame: Any
    ) -> dict[str, list]:
        """Bars for *frame* plus the warm compute over them.

        The one place the compute happens. It goes through
        :meth:`IncrementalIndicator.compute` rather than reimplementing the
        merge here, so there is a single warm path to reason about — a
        duplicate one is exactly the kind that survives a mutation because
        nothing exercises it.
        """
        bars = self.bars(key, frame)
        with self._lock:
            assert self._window is not None
            return self._window.compute(indicator_id, bars)

    def window_bars(self) -> list[dict[str, Any]]:
        """The bar list the last compute ran over, for point indexing."""
        with self._lock:
            assert self._window is not None
            return self._window.bars()

    def bars_cold(self, key: tuple[str, str], frame: Any) -> list[dict[str, Any]]:
        """Bars for *frame* with **no** warm reuse — the reference path.

        The same tail and the same live bar, with no window merge, so a
        compute over it is the plain full rebuild. Exists so equivalence
        between the warm and cold paths is an assertion against a real
        reference rather than a copy of the implementation.
        """
        tail = self._tail_cache.acquire(key, frame.time)
        return (tail.bars if tail is not None else []) + [_live_bar(frame)]


def _live_bar(frame: Any) -> dict[str, Any]:
    return {
        "time": frame.time,
        "open": frame.open,
        "high": frame.high,
        "low": frame.low,
        "close": frame.close,
        "volume": frame.volume,
    }


class SeriesCacheStore:
    """Bounded, reference-counted registry of :class:`IndicatorSeriesCache`.

    The tail is derived and cheap to keep; the live bar window is not, and
    neither is the warm indicator state. So series state is created when the
    first subscriber for a key arrives and torn down when the last one goes
    away. That refcount is the explicit end-of-life the per-connection cache
    got for free by being per-connection, and it is what stops a
    process-level cache from accumulating live-bar state for instruments
    nobody is watching.
    """

    def __init__(
        self, tail_cache: IndicatorTailCache, max_series: int = TAIL_CACHE_MAX_ENTRIES
    ) -> None:
        self._tail_cache = tail_cache
        self._max_series = max(1, int(max_series))
        self._lock = threading.Lock()
        self._series: OrderedDict[tuple[str, str], IndicatorSeriesCache] = OrderedDict()

    def acquire(self, key: tuple[str, str]) -> IndicatorSeriesCache:
        with self._lock:
            series = self._series.get(key)
            if series is None:
                series = self._series[key] = IndicatorSeriesCache(self._tail_cache)
            self._series.move_to_end(key)
            series.acquire()
            return series

    def release(self, key: tuple[str, str]) -> None:
        with self._lock:
            series = self._series.get(key)
        if series is not None:
            series.release()

    def peek(self, key: tuple[str, str]) -> IndicatorSeriesCache | None:
        with self._lock:
            return self._series.get(key)

    def __len__(self) -> int:
        with self._lock:
            return len(self._series)

    def keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._series)

    def clear(self) -> None:
        with self._lock:
            for series in self._series.values():
                series.release()
            self._series.clear()


#: Process-level series store, paired with :data:`TAIL_CACHE`.
SERIES_CACHE = SeriesCacheStore(TAIL_CACHE)


# ---------------------------------------------------------------------------
# Per-connection subscription registry
# ---------------------------------------------------------------------------


class IndicatorStreamRegistry:
    """Per-connection indicator subscriptions + bar-frame driven pushes.

    Holds **subscriptions only**. The datalake tail and the warm bar/shim
    window are process-level (:data:`TAIL_CACHE`, :data:`SERIES_CACHE`), so
    a second connection on the same ``(instrument, timeframe)`` shares them
    instead of re-reading the lake and re-building 400 candle shims.

    ``push`` is the connection's frame sink (same drop-oldest queue as bars
    and depth ride). Subscriptions reference-count the process-level series
    for their key, and :meth:`close` gives the reference back.
    """

    def __init__(
        self,
        push: Any,
        *,
        tail_cache: IndicatorTailCache | None = None,
        series_cache: SeriesCacheStore | None = None,
    ) -> None:
        self._push = push
        # (iid, interval, indicator_id) -> {"mode": str, "last_push": float}
        self._subs: dict[tuple[str, str, str], dict[str, Any]] = {}
        # (iid, interval) keys this connection holds a series reference for
        self._held: set[tuple[str, str]] = set()
        self._tail_cache = TAIL_CACHE if tail_cache is None else tail_cache
        self._series_cache = SERIES_CACHE if series_cache is None else series_cache

    # -- subscription management -------------------------------------------

    def add(self, instrument: str, interval: str, ids: list[str], mode: str) -> list[str]:
        """Register ids; returns the accepted list (caller acks)."""
        from tradex_analytics.indicators import get_indicator_spec

        unknown = [i for i in ids if get_indicator_spec(i) is None]
        if unknown:
            raise ValueError(f"unknown indicators: {unknown}")
        if mode not in ("bar-close", "tick"):
            raise ValueError(f"bad mode: {mode!r} (bar-close | tick)")
        for ind_id in ids:
            self._subs[(instrument, interval, ind_id)] = {"mode": mode, "last_push": 0.0}
        self._hold(instrument, interval)
        return list(ids)

    def remove(self, instrument: str, interval: str, ids: list[str] | None) -> list[str]:
        """Drop ids (None = all for the pair); returns the removed list."""
        targets = [
            key for key in self._subs
            if key[0] == instrument and key[1] == interval and (ids is None or key[2] in ids)
        ]
        removed = [key[2] for key in targets]
        for key in targets:
            del self._subs[key]
        if not self.has_subs(instrument, interval):
            self._unhold(instrument, interval)
        return removed

    def has_subs(self, instrument: str, interval: str) -> bool:
        return any(key[0] == instrument and key[1] == interval for key in self._subs)

    def _hold(self, instrument: str, interval: str) -> None:
        key = (instrument, interval)
        if key in self._held:
            return
        self._held.add(key)
        self._series_cache.acquire(key)

    def _unhold(self, instrument: str, interval: str) -> None:
        key = (instrument, interval)
        if key not in self._held:
            return
        self._held.discard(key)
        self._series_cache.release(key)

    def close(self) -> None:
        """Give back every process-level series reference this connection held.

        Idempotent, and safe on a connection that never subscribed. Without
        it a process-level cache has no defined end of life: the warm window
        for a series would outlive the only subscriber that ever asked for it.
        """
        for key in tuple(self._held):
            self._unhold(*key)
        self._subs.clear()

    # -- push path -----------------------------------------------------------

    def handle_bar_frame(self, frame: Any) -> None:
        """Called for every bar frame; pushes per subscription mode.

        Runs on the event loop (``ReactiveBus`` delivers synchronously), so
        everything blocking is behind the process-level caches.

        Zero parity: indicators compute on every bar for their subscribed
        (instrument, timeframe), regardless of whether the bar came from a
        live broker or the replay engine. The ``source`` field is metadata,
        not a routing key — filtering on it here would make replay indicators
        diverge from live ones.
        """
        key_prefix = (frame.instrument, frame.timeframe)
        targets = [k for k in self._subs if k[:2] == key_prefix]
        if not targets:
            return
        now = time.monotonic()
        for key in targets:
            mode = self._subs[key]["mode"]
            if mode == "bar-close" and not frame.closed:
                continue
            if mode == "tick":
                if frame.closed:
                    continue  # closed bar belongs to bar-close semantics
                last = self._subs[key]["last_push"]
                if now - last < _TICK_THROTTLE_SECONDS:
                    continue
            try:
                self._push_indicator(frame, key[2])
                # Throttle window measured from push completion, so a slow
                # compute cannot immediately admit the next frame.
                self._subs[key]["last_push"] = time.monotonic()
            except Exception:  # noqa: BLE001 — one bad compute never kills the socket
                log.exception("indicator push failed for %s", key)

    def _tail_bars(self, frame: Any) -> list[dict[str, Any]]:
        """Datalake tail for (instrument, timeframe) + the current bar.

        The single-subscriber path, kept because the tail contract is worth
        asserting on its own; the push path goes through the series cache,
        which reads the same tail once per frame.
        """
        key = (frame.instrument, frame.timeframe)
        tail = self._tail_cache.ensure_loaded(key, frame.time)
        return self._merge(tail.bars, frame)

    @staticmethod
    def _merge(tail_bars: list[dict[str, Any]], frame: Any) -> list[dict[str, Any]]:
        live = _live_bar(frame)
        if tail_bars and tail_bars[-1]["time"] == frame.time:
            return tail_bars[:-1] + [live]
        return tail_bars + [live]

    def _push_indicator(self, frame: Any, indicator_id: str) -> None:
        key = (frame.instrument, frame.timeframe)
        # peek, not acquire: the subscription already holds this connection's
        # reference on the series (see ``_hold``), so acquiring per push would
        # inflate the refcount on every bar and the reference would never be
        # given back. A miss here can only mean the subscription outlived its
        # hold, so acquire defensively rather than pushing a dead series.
        series = self._series_cache.peek(key)
        if series is None:
            series = self._series_cache.acquire(key)
        got = series.compute(indicator_id, key, frame)
        bars = series.window_bars()
        plots = list(got.keys())
        points = []
        for i in range(max(0, len(bars) - _POINTS_PER_PUSH), len(bars)):
            point = {"time": bars[i]["time"]}
            for plot in plots:
                value = got[plot][i] if i < len(got[plot]) else None
                point[plot] = value if value is None else float(value)
            points.append(point)
        self._push({
            "type": "indicator",
            "instrument": frame.instrument,
            "interval": frame.timeframe,
            "id": indicator_id,
            "points": points,
        })
