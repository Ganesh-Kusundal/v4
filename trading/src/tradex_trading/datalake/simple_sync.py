"""Simple sync — the one datalake fill path.

Candidate 2 of the 2026-09-17 architecture review asked for the dual sync
paths (``simple_sync`` vs ``SyncOrchestrator``) to be unified. They were
reunited *under this name*, not by keeping the orchestrator: the entry point
stays a single function, but the fetch it performs is now
``ParallelHistoryFetcher``, so the simple path inherits the two properties
that made the orchestrator worth having — multi-broker failover and
clipped-tail repair (a 375-bar reply for a 375-bar window used to score
"success" and never fail over, leaving a permanent gap).

``simple_fetcher.py`` is deleted: it was ~155 LOC of chunking and threading
that ``ParallelHistoryFetcher.fetch`` already did, minus the failover.

SyncResult and series_to_frame moved here from historical_sync.py
(2026-09-21): this is the only sync entry point, so the shared types live
with it.
"""
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import batched
from typing import Any

import pandas as pd
from tradex_domain import Timeframe
from tradex_domain.market import HistoricalSeries

from tradex_trading.datalake.parallel_fetcher import ParallelHistoryFetcher

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    requested: int
    fetched: int
    written: int
    failed: list[str]


def series_to_frame(series: HistoricalSeries, symbol: str) -> pd.DataFrame:
    """Convert HistoricalSeries to storage DataFrame (tz-naive IST)."""
    if not series.candles:
        return pd.DataFrame()
    from tradex_domain.market_calendar import to_ist_naive

    candles = series.candles
    timestamps = [
        to_ist_naive(c.timestamp if c.timestamp.tzinfo is not None
                     else c.timestamp.replace(tzinfo=UTC))
        for c in candles
    ]
    return pd.DataFrame({
        "symbol": symbol,
        "exchange": [c.instrument.exchange.value if hasattr(c.instrument, "exchange") else "NSE"
                     for c in candles],
        "kind": "equity",
        "timeframe": str(series.timeframe.value),
        "timestamp": timestamps,
        "open": [float(c.ohlc.open.value) for c in candles],
        "high": [float(c.ohlc.high.value) for c in candles],
        "low": [float(c.ohlc.low.value) for c in candles],
        "close": [float(c.ohlc.close.value) for c in candles],
        "volume": [float(c.volume.value) if c.volume else 0.0 for c in candles],
    })


def _bar_freq(timeframe: Timeframe | str) -> str:
    """Map a Timeframe to a GapDetector bar_freq."""
    tf = str(timeframe.value if isinstance(timeframe, Timeframe) else timeframe)
    return {"1m": "1min", "5m": "5min", "15m": "15min"}.get(tf, "1min")


def simple_sync(
    broker: Any,
    store: Any,
    instruments: list[Any],
    timeframe: str,
    start: datetime,
    end: datetime,
    *,
    batch_size: int = 20,
    max_workers: int = 5,
    skip_existing: bool = False,
    gaps: Any = None,
    failover_brokers: dict[str, Any] | None = None,
) -> SyncResult:
    """Fetch in parallel, convert, store.

    One flat executor per batch: concurrency is exactly *max_workers*, never
    chunks*workers, so the rate-limiter bucket is not drained. No-data (a
    recent IPO) is silently skipped — only real errors (429/network) are
    failed.

    ``failover_brokers`` lets a caller hand in the other live brokers; an
    uncovered tail or a dead primary is then served from whichever broker can.
    """
    tf = Timeframe(timeframe) if isinstance(timeframe, str) else timeframe
    if not instruments:
        return SyncResult(0, 0, 0, [])

    to_fetch = instruments
    ranges: dict[str, list[tuple[datetime, datetime]]] | None = None
    if skip_existing and gaps is not None:
        to_fetch, ranges = _plan_gaps(instruments, tf, start, end, gaps)
        if not to_fetch:
            log.info("simple_sync: all %d symbols complete — nothing to fetch", len(instruments))
            return SyncResult(len(instruments), 0, 0, [])

    # The single fetch path: primary broker, plus any others for failover and
    # clipped-tail repair. Both are invisible to the caller, which still sees
    # (results, errors) — but the errors now exclude tails another broker filled.
    brokers: dict[str, Any] = {"primary": broker}
    if failover_brokers:
        brokers.update(failover_brokers)
    fetcher = ParallelHistoryFetcher(brokers, max_workers=max_workers)

    fetched = written = 0
    failed: list[str] = []
    batches = [to_fetch[i:i + batch_size] for i in range(0, len(to_fetch), batch_size)]

    for bi, batch in enumerate(batches, 1):
        log.info("simple_sync: batch %d/%d (%d symbols)", bi, len(batches), len(batch))

        results, fetch_errors = fetcher.fetch(
            batch, tf, start, end, ranges=ranges,
        )

        # ParallelHistoryFetcher reports an *empty* series as an error ("empty
        # stitched series"), but this path's contract is that no-data means the
        # instrument is not listed (a recent IPO), not that the fetch failed.
        # Transient failures (429/network/all-brokers-failed) stay failures;
        # the empty-series marker is downgraded to a skip so an IPO doesn't
        # inflate the failure list and trigger pointless retries.
        no_data_ids = {
            str(i.instrument_id) for i in batch
            if str(i.instrument_id) not in results
        }
        real_errors = [
            e for e in fetch_errors
            if not (any(iid in e for iid in no_data_ids) and "empty" in e)
        ]

        # Convert and store
        frames: list[pd.DataFrame] = []
        for inst in batch:
            inst_id = str(inst.instrument_id)
            series = results.get(inst_id)
            if series is None or not series.candles:
                # No data = not listed (IPO), not a failure.
                # Only real fetch errors (429/network) go to failed.
                if any(inst_id in e for e in real_errors):
                    failed.append(inst.symbol)
                else:
                    log.debug("simple_sync: no data for %s (skipped)", inst.symbol)
                continue

            sym = inst.symbol if hasattr(inst, "symbol") else inst_id.split(":")[-1]
            df = series_to_frame(series, sym)
            if df.empty:
                failed.append(inst.symbol)
                continue

            frames.append(df)
            fetched += 1

        if frames:
            written += store.upsert(pd.concat(frames, ignore_index=True))

        log.info("simple_sync: batch %d/%d done (fetched=%d written=%d failed=%d)",
                 bi, len(batches), fetched, written, len(failed))

    if failed:
        log.warning("simple_sync: %d failed: %s", len(failed), failed[:5])
    log.info("simple_sync: %d/%d fetched, %d rows written", fetched, len(instruments), written)

    return SyncResult(len(instruments), fetched, written, failed)


def _plan_gaps(
    instruments: list[Any],
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    gaps: Any,
) -> tuple[list[Any], dict[str, list[tuple[datetime, datetime]]] | None]:
    """Detect gaps and return (to_fetch, ranges)."""
    bar_freq = {"1m": "1min", "5m": "5min", "15m": "15min"}.get(str(timeframe.value), "1min")

    gap_results = gaps.detect(
        instruments, start, end,
        timeframe=str(timeframe.value),
        bar_freq=bar_freq,
    )

    if not gap_results:
        return [], None

    to_fetch = []
    ranges: dict[str, list[tuple[datetime, datetime]]] = {}

    for inst, inst_ranges in gap_results:
        if inst_ranges:
            to_fetch.append(inst)
            ranges[str(inst.instrument_id)] = inst_ranges

    return to_fetch, ranges if ranges else None


__all__ = ["simple_sync"]
