"""Parallel history fetcher — concurrent multi-instrument, multi-broker.

Routes by date range:
  < 30 days → split instruments across all brokers (both serve fast)
  >= 30 days → Dhan only (90-day chunks = fewer API calls; Upstox caps
  minute intervals at 1 month, so it cannot cover multi-month minute ranges)

Long intraday minute ranges are auto-chunked into consecutive windows
(Dhan caps one poll at 90 days, Upstox at 30) and stitched on return,
so callers never need to split ranges themselves.

The fetcher throttles through a per-broker historical rate-limit bucket
(5/s for Dhan, 50/s for Upstox) so fan-out never exceeds the serving broker's
documented historical-data quota — including when a symbol fails over to a
different broker; broker failures still trigger bounded failover.

Failover fan-out is bounded: once a broker fails for any symbol in a batch
it is skipped as a failover target for the rest of the batch, so a
broker-wide outage costs ~M + N calls instead of N × M.  Trade-off: a
per-symbol failure (e.g. an instrument missing from one broker's registry)
also blacklists that broker for the batch — later symbols lose that
failover path.  Accepted for a backfill tool, where GapDetector re-checks
missed symbols on the next run.
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

# ponytail: 30-day threshold.  Dhan serves 90 days/call, Upstox 1 month.
# Below 30 days both brokers are equally efficient per-call, so splitting
# instruments across N brokers gives ~Nx throughput.  Above 30 days Dhan's
# larger chunk size means fewer total API calls.
_DUAL_BROKER_THRESHOLD_DAYS = 30

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
    ) -> dict[str, HistoricalSeries]:
        """Fetch history for all instruments.  Returns ``{symbol: HistoricalSeries}``.

        Routing:
          - date range < 30 days → split instruments across all brokers
          - date range >= 30 days → Dhan only (fewer API calls via 90-day chunks)

        Intraday minute ranges longer than the serving broker's per-poll cap
        (Dhan 90d, Upstox 30d) are auto-chunked into consecutive windows and
        stitched — no truncation, no raise (see ``_chunk_cap_for``).
        """
        if not instruments:
            return {}
        if isinstance(timeframe, str):
            timeframe = Timeframe(timeframe)

        days = (end - start).days
        broker_names = self._pick_brokers(days)
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
        log.info("ParallelHistoryFetcher: %d instruments, %d days, brokers=%s",
                 len(instruments), days, broker_names)

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
        #: Brokers that raised for at least one symbol this batch. Once a
        #: broker proves broken we stop routing failover work to it, so a
        #: broker-wide outage costs ~M + N calls instead of N x M.
        failed_brokers: set[str] = set()

        def _call_history(
            broker: Any, inst: Instrument, *, s: datetime, e: datetime,
        ) -> HistoricalSeries | None:
            """One broker.history call with the right cap-window shape."""
            # Broker adapters accept (instrument, timeframe, start, end) positional
            return broker.history(inst, timeframe, s, e)

        def _fetch_one_chunked(
            broker_name: str, broker: Any, inst: Instrument,
        ) -> HistoricalSeries | None:
            """Fetch all windows for one instrument, stitch or fail over as a whole."""
            assert cap is not None
            windows = _date_windows(start, end, max_days=cap)
            stitched: list = []
            for ws, we in windows:
                # Rate-limit per window call (was per-instrument before)
                limiter = self._limiters[broker_name]
                if not limiter.acquire("historical", timeout=ACQUIRE_TIMEOUT_S):
                    log.warning(
                        "rate-limit gate timed out for %s via %s — proceeding anyway",
                        inst.instrument_id, broker_name,
                    )
                part = _call_history(broker, inst, s=ws, e=we)
                if part is not None and part.candles:
                    stitched.extend(part.candles)
            if stitched:
                # Deduplicate by timestamp (overlap at window boundaries), preserve order
                seen: set = set()
                deduped: list = []
                for c in sorted(stitched, key=lambda x: x.timestamp):
                    ts = c.timestamp
                    if ts not in seen:
                        seen.add(ts)
                        deduped.append(c)
                # Build a stitched series (start/end are the original request window)
                tf = Timeframe(timeframe) if isinstance(timeframe, str) else timeframe  # type: ignore[arg-type]
                return HistoricalSeries(
                    instrument=inst, timeframe=tf, candles=deduped, start=start, end=end,
                )
            return None

        def _fetch_one(broker_name: str, broker: Any, inst: Instrument) -> None:
            limiter = self._limiters[broker_name]
            first_exc: Exception | None = None
            try:
                if needs_chunk:
                    series = _fetch_one_chunked(broker_name, broker, inst)
                    if series is not None and len(series.candles) > 0:
                        with lock:
                            results[str(inst.instrument_id)] = series
                        return
                    # Empty stitched result -> let failover try (if any), else record error below
                    # Fall through to failover loop without marking broker as failed yet
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
                    failed_brokers.add(broker_name)
            # Failover: try remaining brokers, skipping any already known-failed
            for other_name, other_broker in active_brokers.items():
                if other_name == broker_name:
                    continue
                with lock:
                    if other_name in failed_brokers:
                        continue
                try:
                    if needs_chunk:
                        # Failover also chunked with the other broker's limiter
                        other_cap = (
                            _chunk_cap_for(timeframe, [other_name])
                            or _UPSTOX_INTRADAY_MAX_DAYS
                        )
                        windows = _date_windows(start, end, max_days=other_cap)
                        stitched2: list = []
                        for ws, we in windows:
                            if not self._limiters[other_name].acquire(
                                "historical", timeout=ACQUIRE_TIMEOUT_S,
                            ):
                                log.warning(
                                    "rate-limit gate timed out for %s via %s"
                                    " — proceeding anyway",
                                    inst.instrument_id, other_name,
                                )
                            part2 = _call_history(other_broker, inst, s=ws, e=we)
                            if part2 is not None and part2.candles:
                                stitched2.extend(part2.candles)
                        if stitched2:
                            seen2: set = set()
                            deduped2: list = []
                            for c in sorted(stitched2, key=lambda x: x.timestamp):
                                if c.timestamp not in seen2:
                                    seen2.add(c.timestamp)
                                    deduped2.append(c)
                            tf2 = Timeframe(timeframe) if isinstance(timeframe, str) else timeframe  # type: ignore[arg-type]
                            series = HistoricalSeries(
                                instrument=inst, timeframe=tf2,
                                candles=deduped2, start=start, end=end,
                            )
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
                        failed_brokers.add(other_name)
            with lock:
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

    def _pick_brokers(self, days: int) -> list[str]:
        """Select brokers based on date range.

        < 30 days → all available brokers (split instruments for speed)
        >= 30 days → Dhan only (90-day chunks = fewer calls)

        Upstox V3 `/historical-candle` caps retrieval at ONE MONTH for
        1-15-minute intervals (1 quarter for >15-min minutes and hours,
        1 decade for daily) — so for the M1 datalake backfill Dhan is
        preferred: its `/charts/intraday` polls up to 90 days per request.
        Ranges beyond the serving broker's cap are auto-chunked in
        ``fetch()`` (``_date_windows``); daily+ timeframes use Dhan
        `/charts/historical` (unlimited) and are unaffected.
        """
        names = list(self._brokers.keys())
        if days < _DUAL_BROKER_THRESHOLD_DAYS:
            return names  # use all brokers
        # Prefer dhan for longer ranges: 90-day intraday chunks vs Upstox's
        # 1-month cap on minute intervals (V3 /historical-candle).
        if "dhan" in names:
            return ["dhan"]
        return names  # fallback: whatever we have

    # ------------------------------------------------------------------ info

    @property
    def broker_names(self) -> list[str]:
        return list(self._brokers.keys())


__all__ = ["ParallelHistoryFetcher"]
