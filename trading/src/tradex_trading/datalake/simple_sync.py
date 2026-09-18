"""Simple sync — ponytail edition.

Replaces SyncOrchestrator + ParallelHistoryFetcher + fetch_with_backoff with
a direct fetch-convert-store loop. No backoff, no blacklisting, no failover.
"""
import logging
from datetime import datetime
from typing import Any

import pandas as pd
from tradex_domain import Timeframe

from tradex_trading.datalake.historical_sync import SyncResult, series_to_frame

log = logging.getLogger(__name__)


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
) -> SyncResult:
    """Simple sync: fetch in parallel, convert, store.

    ponytail: No backoff, no blacklisting, no failover. Just fetch and store.
    No-data (recent IPOs) is silently skipped — only real errors are failed.
    """
    from tradex_trading.datalake.simple_fetcher import fetch_history_chunked

    tf = Timeframe(timeframe) if isinstance(timeframe, str) else timeframe
    if not instruments:
        return SyncResult(0, 0, 0, [])

    to_fetch = instruments
    if skip_existing and gaps is not None:
        to_fetch, _ = _plan_gaps(instruments, tf, start, end, gaps)
        if not to_fetch:
            log.info("simple_sync: all %d symbols complete — nothing to fetch", len(instruments))
            return SyncResult(len(instruments), 0, 0, [])

    fetched = written = 0
    failed: list[str] = []
    batches = [to_fetch[i:i + batch_size] for i in range(0, len(to_fetch), batch_size)]

    for bi, batch in enumerate(batches, 1):
        log.info("simple_sync: batch %d/%d (%d symbols)", bi, len(batches), len(batch))

        # ponytail: flat executor — concurrency is exactly max_workers, not chunks*workers.
        results, fetch_errors = fetch_history_chunked(
            broker, batch, tf, start, end,
            max_days=90, max_workers=max_workers,
        )

        # Convert and store
        frames: list[pd.DataFrame] = []
        for inst in batch:
            inst_id = str(inst.instrument_id)
            series = results.get(inst_id)
            if series is None or not series.candles:
                # ponytail: no data = not listed (IPO), not a failure.
                # Only actual fetch errors (429/network) go to failed.
                if inst_id in fetch_errors:
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
