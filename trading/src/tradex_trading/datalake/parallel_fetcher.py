"""Parallel history fetcher — concurrent multi-instrument, multi-broker.

Routes by date range:
  < 30 days → split instruments across all brokers (both serve fast)
  >= 30 days → Dhan only (90-day chunks = fewer API calls; Upstox caps
  minute intervals at 1 month, so it cannot cover multi-month minute ranges)

Dhan intraday minute ranges (> 90 days) fail loud instead of silently
truncating, since the fetcher does not chunk across the 90-day per-poll cap.

The fetcher throttles through a per-broker historical rate-limit bucket
(5/s for Dhan/Upstox) so fan-out never exceeds the serving broker's
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
from datetime import datetime
from typing import Any

from tradex_brokers.common.resilience import (
    MultiBucketRateLimiter,
    limiter_for_provider,
)
from tradex_domain.enums import Timeframe
from tradex_domain.errors import SDKError
from tradex_domain.instruments import Instrument
from tradex_domain.market import HistoricalSeries

log = logging.getLogger(__name__)

# ponytail: 30-day threshold.  Dhan serves 90 days/call, Upstox 1 month.
# Below 30 days both brokers are equally efficient per-call, so splitting
# instruments across N brokers gives ~Nx throughput.  Above 30 days Dhan's
# larger chunk size means fewer total API calls.
_DUAL_BROKER_THRESHOLD_DAYS = 30

# Dhan v2 `/charts/intraday` polls a MAXIMUM of 90 days per request — the
# official docs say "only 90 days of data can be polled at once" for minute
# intervals. The older CLAUDE.md note ("last 5 trading days") does NOT match
# the current v2 API: this repo's own M1 datalake spans 68 trading days of
# Dhan-sourced bars. Ranges beyond 90 days need chunking, which the fetcher
# does not do, so they would otherwise be silently truncated.
_DHAN_INTRADAY_MAX_DAYS = 90

# Timeframes DhanBroker routes to `/charts/intraday` (1m/5m/15m/1h →
# interval 1/5/15/60). Only D1 routes to `/charts/historical` (data back to
# inception, no per-poll cap); M30/W1 are unsupported by Dhan's `requested_timeframe`
# map and raise ValueError, so they are not covered by this fetcher.
_DHAN_INTRADAY_TIMEFRAMES = frozenset({
    Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1,
})


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

        A Dhan-only intraday range longer than 90 days raises ``SDKError``
        instead of silently returning truncated data (see ``_pick_brokers``).
        """
        if not instruments:
            return {}
        if isinstance(timeframe, str):
            timeframe = Timeframe(timeframe)

        days = (end - start).days
        broker_names = self._pick_brokers(days)
        # Fail loud when Dhan is the sole broker for an intraday range that
        # exceeds the API's 90-day per-poll window: the fetcher does not
        # chunk, so the request would otherwise silently truncate. Daily+
        # timeframes use /charts/historical (unlimited), so they skip this.
        if (
            timeframe in _DHAN_INTRADAY_TIMEFRAMES
            and days > _DHAN_INTRADAY_MAX_DAYS
            and broker_names == ["dhan"]
        ):
            raise SDKError(
                f"Dhan intraday history limited to {_DHAN_INTRADAY_MAX_DAYS} days "
                f"per request (requested {days}); the fetcher does not chunk — "
                f"split the date range into chunks of {_DHAN_INTRADAY_MAX_DAYS} days "
                "or fewer (backfill_parquet.py also passes the full window to fetch, "
                "so it hits the same guard)"
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

        def _fetch_one(broker_name: str, broker: Any, inst: Instrument) -> None:
            limiter = self._limiters[broker_name]
            first_exc: Exception | None = None
            try:
                if not limiter.acquire("historical", timeout=30.0):
                    log.warning(
                        "ParallelHistoryFetcher: rate-limit gate timed out for "
                        "%s via %s — proceeding anyway",
                        inst.instrument_id, broker_name,
                    )
                series = broker.history(inst, timeframe, start, end)
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
                    if not self._limiters[other_name].acquire("historical", timeout=30.0):
                        log.warning(
                            "ParallelHistoryFetcher: rate-limit gate timed out for "
                            "%s via %s — proceeding anyway",
                            inst.instrument_id, other_name,
                        )
                    series = other_broker.history(inst, timeframe, start, end)
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
        1 decade for daily) — so for the M1 datalake backfill Upstox cannot
        serve a multi-month range without truncation. Dhan's `/charts/intraday`
        polls up to 90 days per request, making it the only broker that covers
        >= 30-day minute ranges. Long Dhan-only minute ranges (> 90 days) raise
        in ``fetch()`` (the fetcher does not chunk); daily+ timeframes use Dhan
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
