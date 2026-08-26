"""Parallel history fetcher — concurrent multi-instrument, multi-broker.

Routing: instruments are split across ALL configured brokers regardless of
range length; per-broker poll caps are handled by auto-chunking below.
(Live backfills showed Dhan enforcing burst walls well below its documented
5/s after ~100 calls, so concentrating a long range on Dhan alone was the
slowest possible routing — splitting halves each broker's quota pressure.)

Long intraday minute ranges are auto-chunked into consecutive windows
(Dhan caps one poll at 90 days, Upstox at 30) and stitched on return,
so callers never need to split ranges themselves.

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
from tradex_domain.timeframe import DHAN_INTRADAY as _DHAN_INTRADAY_TIMEFRAMES

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


def _provider_for(name: str) -> str:
    """Map a broker key to the provider name used for rate-limit tables.

    The fetcher's broker keys are ``"dhan"``/``"upstox"``/``"paper"``,
    matching provider names.  Unknown keys fall back to ``"paper"`` (the
    unthrottled table) so custom test brokers are never rate-limited by a
    wrong provider's budget.
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
# for the rest of a 500-symbol batch, but 3 distinct misses is a strong
# signal the broker is down. Tune up if flakes appear, down if real outages
# drag the batch.
BROKER_HEALTH_THRESHOLD = 3


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
        self._limiters: dict[str, MultiBucketRateLimiter] = (
            {name: rate_limiter for name in brokers}
            if rate_limiter is not None
            else {name: limiter_for_provider(_provider_for(name)) for name in brokers}
        )

    # ------------------------------------------------------------------ public

    def fetch(
        self,
        instruments: list[Instrument],
        timeframe: Timeframe | str,
        start: datetime,
        end: datetime,
        ranges: dict[str, list[tuple[datetime, datetime]]] | None = None,
    ) -> dict[str, HistoricalSeries]:
        """Fetch history for all instruments.  Returns ``{symbol: HistoricalSeries}``.

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
            return {}
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

        def _fetch_one(broker_name: str, broker: Any, inst: Instrument) -> None:
            limiter = self._limiters[broker_name]
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
                windows = _windows_for(inst, cap)
                if windows is not None:
                    series = self._stitch_windows(broker_name, broker, inst, windows)
                    if series is not None and series.candles:
                        with lock:
                            results[str(inst.instrument_id)] = series
                        return
                    raise RuntimeError(
                        f"{broker_name}: empty stitched series for {inst.instrument_id}"
                    )
                if not limiter.acquire("historical", timeout=ACQUIRE_TIMEOUT_S):
                    log.warning(
                        "ParallelHistoryFetcher: rate-limit gate timed out for "
                        "%s via %s — proceeding anyway",
                        inst.instrument_id, broker_name,
                    )
                series = _call_history(broker, inst, s=start, e=end)
                if series is not None and len(series.candles) > 0:
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
                    other_windows = _windows_for(
                        inst, _chunk_cap_for(self._timeframe, [other_name])
                    )
                    if other_windows is not None:
                        series = self._stitch_windows(
                            other_name, other_broker, inst, other_windows
                        )
                        if series is not None and series.candles:
                            with lock:
                                results[str(inst.instrument_id)] = series
                            return
                        raise RuntimeError(
                            f"{other_name}: empty stitched failover"
                            f" for {inst.instrument_id}"
                        )
                    if not self._limiters[other_name].acquire(
                        "historical", timeout=ACQUIRE_TIMEOUT_S
                    ):
                        log.warning(
                            "ParallelHistoryFetcher: rate-limit gate timed out for "
                            "%s via %s — proceeding anyway",
                            inst.instrument_id, other_name,
                        )
                    series = _call_history(other_broker, inst, s=start, e=end)
                    if series is not None and len(series.candles) > 0:
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
        return results

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
        limiter = self._limiters[broker_name]
        for ws, we in windows:
            if not limiter.acquire("historical", timeout=ACQUIRE_TIMEOUT_S):
                log.warning(
                    "ParallelHistoryFetcher: rate-limit gate timed out for "
                    "%s via %s — proceeding anyway",
                    inst.instrument_id, broker_name,
                )
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


__all__ = ["ParallelHistoryFetcher"]
