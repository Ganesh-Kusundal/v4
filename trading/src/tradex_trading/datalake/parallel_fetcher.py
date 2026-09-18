"""Parallel history fetcher — concurrent multi-instrument, multi-broker.

Routing: instruments are split across ALL configured brokers regardless of
range length; per-broker poll caps are handled by auto-chunking below.
(Live backfills showed Dhan enforcing burst walls well below its documented
5/s after ~100 calls, so concentrating a long range on Dhan alone was the
slowest possible routing — splitting halves each broker's quota pressure.)

Long intraday minute ranges are auto-chunked into consecutive windows
(Dhan caps one poll at 90 days, Upstox at 30) and stitched on return,
so callers never need to split ranges themselves.

**A non-empty response is not evidence of a complete one.** Dhan's NSE 1m
series stops at 15:14 and Upstox's at 15:29, so a request for 09:15–15:30 came
back short while still holding 375 candles — scored a success, never failed
over, and the missing tail reappeared as a permanent gap that every re-fetch
answered the same way. Responses are now checked against the window they were
asked for; an uncovered tail is fetched from another broker and merged, with
the bars already in hand kept as-is (`_clipped_tail`, `_merge_candles`).

The fetcher throttles through a per-broker historical rate-limit bucket
(5/s for Dhan, 50/s for Upstox) so fan-out never exceeds the serving broker's
documented historical-data quota — including when a symbol fails over to a
different broker; broker failures still trigger bounded failover.

Failover fan-out is bounded: a broker is blacklisted for the batch only
after K distinct symbols fail on it (default K=3), so a broker-wide
outage still costs ~M + N calls instead of N x M, while a per-instrument
miss (e.g. one symbol missing from a broker's registry) no longer kills
failover for the rest of the batch.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

from tradex_brokers.common.resilience import (
    MultiBucketRateLimiter,
    limiter_for_provider,
)
from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Instrument
from tradex_domain.market import HistoricalSeries
from tradex_domain.market_calendar import MARKET_CLOSE, MARKET_OPEN, to_ist_naive
from tradex_domain.timeframe import DHAN_INTRADAY as _DHAN_INTRADAY_TIMEFRAMES
from tradex_domain.timeframe import bucket_seconds as _bucket_seconds

log = logging.getLogger(__name__)

#: Rate-limiter acquire timeout (seconds) — single-sourced [REF-10a].
ACQUIRE_TIMEOUT_S = 30.0

# ponytail: cap kept small — log already truncates to 5; 100 is enough for
# postmortem context. Grow only if ops asks for full failure lists.
ERROR_LOG_CAP = 100

# ponytail: poll caps + auto-chunking — Dhan 90d on intraday, Upstox 30d on
# minute.  Timeframe sets are single-sourced (domain/timeframe.py) to close
# SMELL-02.  Long minute ranges are now auto-chunked (sequential windows per
# instrument, concat+dedupe) instead of fail-loud, so callers need not split.
_DHAN_INTRADAY_MAX_DAYS = 90
_UPSTOX_INTRADAY_MAX_DAYS = 30

#: How far short of a window's end a response may fall before it counts as
#: clipped.  Two bars absorb the closing-stamp convention (Upstox's last 1m bar
#: is 15:29 for a session closing at 15:30) and a live fetch landing one bar
#: behind "now".  Dhan's NSE 1m series ends 15 minutes early — 15x this
#: tolerance, so the two cases do not blur together.
_CLIP_TOLERANCE_BARS = 2


def _clipped_tail(
    series: HistoricalSeries,
    windows: list[tuple[datetime, datetime]],
    timeframe: Timeframe,
) -> tuple[datetime, datetime] | None:
    """Span a non-empty response failed to cover, or ``None`` if it covers it.

    Two shapes count, and the second one is the one that matters for repair:

    - the response stops short of the window's end (a whole-session request
      answered only through 15:14);
    - the response holds *nothing inside* the window at all, which is what a
      tail-only gap fetch gets back from a broker that widens every request to
      the full day — a day's worth of candles, none of them the ones asked
      for. Empty-for-the-window is not "covered just because non-empty".

    Only a window inside a single session can be judged this way: across a
    multi-day span the last bar legitimately precedes the window end
    (weekends, holidays), and per-day completeness is GapDetector's job.
    Candle stamps are normalized to tz-naive IST before comparison — brokers
    return aware UTC while the windows here are IST wall clock.

    The window is clamped to session hours first, because the callers that
    repair gaps ask in *day* terms — ``topup``/``fill_gaps`` clusters end at
    23:59:59 — and a series that stops at 15:29 would otherwise look 8 hours
    short on every such request, costing a pointless second fetch per symbol.
    """
    if timeframe not in _DHAN_INTRADAY_TIMEFRAMES or not series.candles:
        return None
    tolerance = timedelta(seconds=_bucket_seconds(timeframe) * _CLIP_TOLERANCE_BARS)
    stamps = [to_ist_naive(c.timestamp) for c in series.candles]
    norm_windows = [(to_ist_naive(ws), to_ist_naive(we)) for ws, we in windows]
    for ws, we in norm_windows:
        # A multi-day span (≥2 calendar days) is not judgeable per-day: the
        # last bar legitimately precedes the end across weekends/holidays.
        # But a single calendar day expressed midnight-to-midnight — the
        # shape a gap detector emits for a fully-empty cluster day — crosses
        # the date boundary while still asking for exactly one session. Judge
        # the session within it; skip only genuinely multi-day windows.
        span_days = (we.date() - ws.date()).days
        if span_days >= 2:
            continue
        cur = ws.date()
        while cur <= we.date():
            lo = max(ws, datetime.combine(cur, MARKET_OPEN))
            hi = min(we, datetime.combine(cur, MARKET_CLOSE))
            cur += timedelta(days=1)
            if hi <= lo:
                continue  # this day sits outside session hours
            served = [t for t in stamps if lo <= t <= hi]
            if not served:
                return (lo, hi)  # crossed the wire, but not for this day
            last = max(served)
            if last >= hi - tolerance:
                continue
            return (last, hi)
    return None


def _merge_candles(
    base: HistoricalSeries,
    extra: HistoricalSeries,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    instrument: Instrument,
) -> HistoricalSeries:
    """Overlay ``extra`` on ``base``: base keeps its own bars, extra fills gaps.

    The bars already in hand are real, so a clipped window changes broker only
    for what was missing — no wholesale rewrite of a day just because one
    broker cannot reach its close.
    """
    merged = {to_ist_naive(c.timestamp): c for c in extra.candles}
    merged.update({to_ist_naive(c.timestamp): c for c in base.candles})
    return HistoricalSeries(
        instrument=instrument, timeframe=timeframe,
        candles=[merged[k] for k in sorted(merged)], start=start, end=end,
    )


def _date_windows(
    start: datetime, end: datetime, *, max_days: int,
) -> list[tuple[datetime, datetime]]:
    """Split [start, end] into consecutive windows of at most max_days."""
    if max_days <= 0:
        return [(start, end)]
    out: list[tuple[datetime, datetime]] = []
    cur = start
    while cur < end:
        nxt = min(end, cur + timedelta(days=max_days))
        out.append((cur, nxt))
        cur = nxt
    if not out:
        return [(start, end)]
    return out


def _chunk_cap_for(timeframe: Timeframe | str, broker_names: list[str]) -> int | None:
    """Return max window days if this brokerage+timeframe must be chunked, else None.

    ponytail: caps key off the *known* providers only.  Unknown broker names
    (custom test doubles) get the conservative Upstox-style 30-day cap when
    they are the sole broker — a truncated-looking window is better than a
    silently-truncated full range.  D1 is never capped.
    """
    tf = Timeframe(timeframe) if isinstance(timeframe, str) else timeframe
    if tf not in _DHAN_INTRADAY_TIMEFRAMES:
        return None  # D1/W1/M30 have no per-poll cap
    if broker_names == ["dhan"]:
        return _DHAN_INTRADAY_MAX_DAYS
    if "dhan" in broker_names:
        return _DHAN_INTRADAY_MAX_DAYS  # dhan serving → its own cap applies
    # upstox-only or unknown-provider-only: conservative minute cap
    return _UPSTOX_INTRADAY_MAX_DAYS


def _split(items: list, n: int) -> list[list]:
    """Split *items* into *n* roughly-equal chunks."""
    if n <= 0:
        return [items]
    k, m = divmod(len(items), n)
    return [items[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n)]


def _default_workers(broker_names: list[str]) -> int:
    """Provider-aware concurrency: Dhan burst walls sit below 5/s after ~100 calls."""
    if "dhan" in broker_names:
        return 2
    return 4


def fetch_with_backoff(
    fetcher: ParallelHistoryFetcher,
    batch: list[Instrument],
    tf: Timeframe | str,
    c_start: datetime,
    c_end: datetime,
    *,
    batch_size: int = 20,
    max_retries: int = 6,
    ranges: dict[str, list[tuple[datetime, datetime]]] | None = None,
    dead_symbols: set[str] | None = None,
) -> dict[str, HistoricalSeries]:
    """Fetch a batch with exponential backoff on rate-limit bursts.

    ``dead_symbols`` tracks symbols that failed permanently (empty series,
    unknown to broker) so they are skipped on subsequent retries and
    clusters. Only transient failures (429, limiter timeout) trigger sleep.
    """
    pending = list(batch)
    merged: dict[str, HistoricalSeries] = {}
    backoff = 30.0
    zero_progress_streak = 0
    if dead_symbols is None:
        dead_symbols = set()
    for attempt in range(1, max_retries + 1):
        if not pending:
            break
        pending = [
            i for i in pending
            if str(i.instrument_id) not in dead_symbols
        ]
        if not pending:
            break
        results, errors = fetcher.fetch(
            pending, tf, c_start, c_end, ranges=ranges,
        )
        merged.update(results)
        succeeded_ids = set(results.keys())
        still_pending = [
            inst for inst in pending
            if str(inst.instrument_id) not in succeeded_ids
        ]
        if not still_pending:
            break
        if len(still_pending) < len(pending):
            zero_progress_streak = 0
            backoff = 30.0
        else:
            zero_progress_streak += 1
            if zero_progress_streak >= 2:
                log.warning(
                    "fetch_with_backoff: no progress twice — giving up on "
                    "%d symbol(s)",
                    len(still_pending),
                )
                break
        if not _has_transient_errors(errors):
            log.warning(
                "fetch_with_backoff: permanent failures only — "
                "giving up on %d symbol(s)",
                len(still_pending),
            )
            dead_symbols.update(str(i.instrument_id) for i in still_pending)
            break
        log.warning(
            "fetch_with_backoff: rate-limited (attempt %d/%d) — sleeping %.0fs",
            attempt, max_retries, backoff,
        )
        time.sleep(backoff)
        backoff = min(backoff * 2, 480.0)
        pending = still_pending
    return merged


def _is_transient_error(error_msg: str) -> bool:
    """Classify a fetch error as transient (retry may help) or permanent.

    Permanent errors: empty series, symbol not in broker registry.
    Transient errors: HTTP 429, rate limiter timeout, network issues.
    """
    permanent_markers = ("empty stitched", "empty series", "empty failover")
    if any(m in error_msg for m in permanent_markers):
        return False
    return True


def _has_transient_errors(errors: list[str]) -> bool:
    """True if any error looks retryable."""
    return any(_is_transient_error(e) for e in errors)


def _provider_for(name: str) -> str:
    """Map a broker key to the provider name used for rate-limit tables.

    The fetcher's broker keys are ``"dhan"``/``"upstox"``/``"paper"``,
    matching provider names.  Unknown keys fall back to ``"paper"`` (the
    unthrottled table) so custom test brokers are never rate-limited by a
    wrong provider's budget — loudly, because that fallback means no
    throttling at all.
    """
    if name in ("dhan", "upstox", "paper"):
        return name
    log.warning(
        "ParallelHistoryFetcher: broker %r is not a known provider — "
        "using the paper (unthrottled) rate-limit table",
        name,
    )
    return "paper"


# ponytail: threshold=3 — one per-instrument miss shouldn't kill the broker
# for the rest of a 500-symbol batch, but 10 distinct misses is a strong
# signal the broker is down. The previous threshold of 3 was too aggressive —
# rate-limit timeouts on 3 symbols would blacklist the broker prematurely.
# Increased to 10 to tolerate transient rate-limit exhaustion.
BROKER_HEALTH_THRESHOLD = 10


class _BrokerHealth:
    """Per-batch broker health: blacklist a broker only after K distinct
    instrument failures on it.  Per-instrument misses (e.g. instrument
    missing from one broker's registry) no longer kill failover for the
    rest of the batch.
    """

    def __init__(self, threshold: int = BROKER_HEALTH_THRESHOLD) -> None:
        self._threshold = threshold
        self._failed_symbols: dict[str, set[str]] = {}
        self._blacklisted: set[str] = set()

    def record_failure(self, broker_name: str, symbol: str) -> None:
        if broker_name in self._blacklisted:
            return
        self._failed_symbols.setdefault(broker_name, set()).add(symbol)
        if len(self._failed_symbols[broker_name]) >= self._threshold:
            self._blacklisted.add(broker_name)

    def is_blacklisted(self, broker_name: str) -> bool:
        return broker_name in self._blacklisted


class ParallelHistoryFetcher:
    """Fetch historical data for many instruments concurrently.

    Parameters
    ----------
    brokers : dict[str, Any]
        Named broker adapters, e.g. ``{"dhan": dhan_broker, "upstox": upstox_broker}``.
        Each must implement ``history(instrument, timeframe, start, end)``.
    max_workers : int
        Total concurrent fetch threads (default 4 — matches Dhan DATA quota
        of 5/s with one slot free for gate overhead).
    rate_limiter : MultiBucketRateLimiter | None
        Optional shared rate limiter applied to EVERY broker (default
        ``None`` → one limiter per broker, built from the broker key's
        provider table via ``limiter_for_provider``).  ``acquire("historical",
        ...)`` is called before every ``history()`` call so fan-out throttles
        to the serving broker's historical-data rate.
    """

    def __init__(
        self,
        brokers: dict[str, Any],
        max_workers: int = 4,
        rate_limiter: MultiBucketRateLimiter | None = None,
    ) -> None:
        self._brokers = brokers
        self._max_workers = max_workers
        # Per-broker limiters: fan-out throttles to the serving broker's
        # documented historical rate instead of firing N workers unbounded.
        # An explicit override is shared across ALL brokers (single limiter is
        # the common case); otherwise each broker key maps to its own
        # provider-tuned limiter so a failover call is throttled by the
        # limiter of the broker actually serving it.
        self._rate_limiter = rate_limiter
        self._explicit_limiter = rate_limiter is not None
        self._limiters = {}
        self._prefetch_gate: dict[str, bool] = {}
        for name, broker in brokers.items():
            if rate_limiter is not None:        # explicit override (kept for tests)
                self._limiters[name] = rate_limiter
                self._prefetch_gate[name] = True
                continue
            shared = getattr(broker, "rate_limiter", None)
            if shared is not None:              # production path: read from broker
                self._limiters[name] = shared
                self._prefetch_gate[name] = False
                continue
            # ponytail: test doubles / non-standard brokers — fall back to a
            # provider-tuned limiter so existing test isolation is preserved.
            self._limiters[name] = limiter_for_provider(_provider_for(name))
            self._prefetch_gate[name] = True

    def _maybe_acquire(
        self, broker_name: str, broker: Any, inst_id: str,
    ) -> None:
        if not self._prefetch_gate.get(broker_name, True):
            return
        limiter = self._limiters[broker_name]
        if not limiter.acquire("historical", timeout=ACQUIRE_TIMEOUT_S):
            log.warning(
                "ParallelHistoryFetcher: rate-limit gate timed out for "
                "%s via %s — proceeding anyway",
                inst_id, broker_name,
            )

    # ------------------------------------------------------------------ public

    def fetch(
        self,
        instruments: list[Instrument],
        timeframe: Timeframe | str,
        start: datetime,
        end: datetime,
        ranges: dict[str, list[tuple[datetime, datetime]]] | None = None,
    ) -> tuple[dict[str, HistoricalSeries], list[str]]:
        """Fetch history for all instruments.

        Returns ``(results, errors)`` — results keyed by instrument_id and
        a list of error messages for failed symbols (used by fetch_with_backoff
        to classify transient vs permanent failures).

        Routing: instruments split across all configured brokers; per-broker
        poll caps handled by auto-chunking (see ``_pick_brokers``).

        Intraday minute ranges longer than the serving broker's per-poll cap
        (Dhan 90d, Upstox 30d) are auto-chunked into consecutive windows and
        stitched — no truncation, no raise (see ``_chunk_cap_for``).

        ``ranges`` optionally maps ``str(instrument_id)`` to missing
        sub-windows (from GapDetector); listed instruments are fetched ONLY
        for those windows — incremental top-up instead of re-pulling the
        whole trailing window.  Result envelopes still report start/end.
        """
        if not instruments:
            return {}, []
        if not self._brokers:
            raise ValueError("ParallelHistoryFetcher requires at least one broker")
        if isinstance(timeframe, str):
            timeframe = Timeframe(timeframe)
        self._timeframe: Timeframe = timeframe
        self._req_start, self._req_end = start, end

        days = (end - start).days
        broker_names = self._pick_brokers()
        # Auto-chunk long minute ranges (SMELL-12): the poll caps mean one
        # request would truncate, so split into consecutive windows and stitch
        # on return.  D1 and non-minute intraday ranges are unlimited.
        cap = _chunk_cap_for(timeframe, broker_names)
        needs_chunk = cap is not None and days > cap
        if needs_chunk:
            log.info(
                "ParallelHistoryFetcher: auto-chunking %d-day %s into %d-day windows",
                days, timeframe, cap,
            )
        log.info("ParallelHistoryFetcher: %d instruments (%d ranged), %d days, brokers=%s",
                 len(instruments), len(ranges or {}), days, broker_names)

        # Per-instrument poll windows: explicit GapDetector ranges when given
        # (each still chunked to the broker cap), else the full [start, end]
        # span when it exceeds the cap, else None (single unwindowed call).
        def _windows_for(
            inst: Instrument, max_days: int | None,
        ) -> list[tuple[datetime, datetime]] | None:
            inst_ranges = (ranges or {}).get(str(inst.instrument_id))
            if inst_ranges is not None:
                md = max_days or 0  # <=0 -> one window per range, uncapped
                return [
                    w for r_start, r_end in inst_ranges
                    for w in _date_windows(r_start, r_end, max_days=md)
                ]
            if max_days:
                return _date_windows(start, end, max_days=max_days)
            return None

        # Split instruments across selected brokers
        active_brokers = {n: self._brokers[n] for n in broker_names}
        chunks = _split(instruments, len(active_brokers))
        broker_assignments: list[tuple[str, Any, list[Instrument]]] = []
        for i, name in enumerate(broker_names):
            if i < len(chunks) and chunks[i]:
                broker_assignments.append((name, active_brokers[name], chunks[i]))

        # Parallel fetch
        results: dict[str, HistoricalSeries] = {}
        lock = threading.Lock()
        errors: list[str] = []
        #: Per-batch broker health: blacklist a broker only after K distinct
        #: instrument failures on it, so a per-instrument miss (e.g. one
        #: symbol missing from a broker's registry) no longer kills
        #: failover for the rest of the batch.
        broker_health = _BrokerHealth()

        def _call_history(
            broker: Any, inst: Instrument, *, s: datetime, e: datetime,
        ) -> HistoricalSeries | None:
            """One broker.history call with the right cap-window shape."""
            # Broker adapters accept (instrument, timeframe, start, end) positional
            return broker.history(inst, timeframe, s, e)

        def _try_broker(
            broker_name: str, broker: Any, inst: Instrument,
        ) -> HistoricalSeries:
            """Serve one instrument, or raise. Never returns an empty series.

            Also rejects a *clipped* one: as far as accept/reject goes, "I got
            375 bars but not the 15 you asked for" is the same outcome as an
            empty response, and the caller can only fail over if it hears one.
            """
            windows = _windows_for(
                inst, _chunk_cap_for(self._timeframe, [broker_name])
            )
            if windows is not None:
                series = self._stitch_windows(broker_name, broker, inst, windows)
                if series is None or not series.candles:
                    raise RuntimeError(
                        f"{broker_name}: empty stitched series for {inst.instrument_id}"
                    )
            else:
                self._maybe_acquire(broker_name, broker, str(inst.instrument_id))
                series = _call_history(broker, inst, s=start, e=end)
                if series is None or not series.candles:
                    raise RuntimeError(
                        f"{broker_name}: empty series for {inst.instrument_id}"
                    )
                windows = [(start, end)]
            return self._complete_shortfall(
                broker_name, broker, inst, series, windows, active_brokers,
                broker_health, lock,
            )

        def _fetch_one(broker_name: str, broker: Any, inst: Instrument) -> None:
            first_exc: Exception | None = None
            skip_primary = False
            with lock:
                if broker_health.is_blacklisted(broker_name):
                    skip_primary = True
            try:
                if skip_primary:
                    raise RuntimeError(
                        f"{broker_name}: blacklisted for batch after K distinct failures"
                    )
                series = _try_broker(broker_name, broker, inst)
                with lock:
                    results[str(inst.instrument_id)] = series
                return
            except Exception as exc:
                first_exc = exc
                with lock:
                    broker_health.record_failure(broker_name, str(inst.instrument_id))
            # Failover: try remaining brokers, skipping any already known-failed
            for other_name, other_broker in active_brokers.items():
                if other_name == broker_name:
                    continue
                with lock:
                    if broker_health.is_blacklisted(other_name):
                        continue
                try:
                    series = _try_broker(other_name, other_broker, inst)
                    with lock:
                        results[str(inst.instrument_id)] = series
                    return
                except Exception as exc:
                    first_exc = first_exc or exc
                    with lock:
                        broker_health.record_failure(other_name, str(inst.instrument_id))
            with lock:
                if len(errors) < ERROR_LOG_CAP:
                    errors.append(f"{inst.instrument_id}: all brokers failed ({first_exc})")

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = []
            for broker_name, broker, chunk in broker_assignments:
                for inst in chunk:
                    futures.append(pool.submit(_fetch_one, broker_name, broker, inst))
            for f in as_completed(futures):
                f.result()  # propagate unexpected exceptions

        if errors:
            log.warning("ParallelHistoryFetcher: %d failures: %s", len(errors), errors[:5])
        log.info("ParallelHistoryFetcher: %d/%d succeeded", len(results), len(instruments))
        return results, errors

    # ------------------------------------------------------------------ routing

    def _stitch_windows(
        self,
        broker_name: str,
        broker: Any,
        inst: Instrument,
        windows: list[tuple[datetime, datetime]],
    ) -> HistoricalSeries | None:
        """Fetch each window with per-call rate limiting, stitch + dedup by timestamp.

        Returns ``None`` if every window returned no candles.  Raises whatever
        the broker raises (caller decides failover vs. blacklist).
        """
        stitched: list = []
        for ws, we in windows:
            self._maybe_acquire(broker_name, broker, str(inst.instrument_id))
            part = broker.history(inst, self._timeframe, ws, we)
            if part is not None and part.candles:
                stitched.extend(part.candles)
        if not stitched:
            return None
        seen: set = set()
        deduped: list = []
        for c in sorted(stitched, key=lambda x: x.timestamp):
            if c.timestamp not in seen:
                seen.add(c.timestamp)
                deduped.append(c)
        return HistoricalSeries(
            instrument=inst, timeframe=self._timeframe, candles=deduped,
            start=self._req_start, end=self._req_end,
        )

    def _complete_shortfall(
        self,
        broker_name: str,
        broker: Any,
        inst: Instrument,
        series: HistoricalSeries,
        windows: list[tuple[datetime, datetime]],
        active_brokers: dict[str, Any],
        broker_health: _BrokerHealth,
        lock: threading.Lock,
    ) -> HistoricalSeries:
        """Fetch a clipped window's tail from another broker and merge it in.

        A clipped response is not a failure — the bars it did return are real —
        so the partial series is kept and only the uncovered tail changes
        broker. This is the step whose absence let a 15-minute hole survive
        every ``fill_gaps`` run: the fetch looked successful, so no other
        broker was ever asked for the 15:15–15:29 bars. The clipped broker is
        deliberately NOT marked unhealthy — it served correctly, it just
        cannot reach the close.
        """
        shortfall = _clipped_tail(series, windows, self._timeframe)
        if shortfall is None:
            return series
        tail_start, tail_end = shortfall
        for other_name, other_broker in active_brokers.items():
            if other_name == broker_name:
                continue
            with lock:
                if broker_health.is_blacklisted(other_name):
                    continue
            try:
                self._maybe_acquire(other_name, other_broker, str(inst.instrument_id))
                part = other_broker.history(inst, self._timeframe, tail_start, tail_end)
            except Exception as exc:  # another broker may still cover it
                log.warning(
                    "ParallelHistoryFetcher: shortfall fetch for %s via %s "
                    "failed (%s)", inst.instrument_id, other_name, exc,
                )
                continue
            if part is None or not part.candles:
                continue
            return _merge_candles(
                series, part, self._timeframe,
                self._req_start, self._req_end, inst,
            )
        log.warning(
            "ParallelHistoryFetcher: %s returned data only through %s "
            "(asked for %s) and no other broker covered the tail",
            inst.instrument_id, tail_start, tail_end,
        )
        return series

    def _pick_brokers(self) -> list[str]:
        """Select serving brokers.

        All configured brokers are used: instruments are split across them,
        and per-broker poll caps (Dhan 90d intraday, Upstox 30d minute) are
        handled by auto-chunking in ``fetch()``.  Splitting halves each
        broker's quota pressure — live backfills showed Dhan's
        /charts/intraday enforcing burst walls well below its documented
        5/s after ~100 calls, so concentrating a long range on Dhan alone
        was the slowest possible routing.
        """
        return list(self._brokers.keys())

    # ------------------------------------------------------------------ info

    @property
    def broker_names(self) -> list[str]:
        return list(self._brokers.keys())


__all__ = [
    "ParallelHistoryFetcher",
    "_default_workers",
    "fetch_with_backoff",
]
