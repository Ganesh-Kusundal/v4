"""Simple sync — the one datalake fill path (Dhan-only).

Caller owns the gap plan: scan once, pass ``ranges=``. Sync never
re-detects. One broker; no failover / clip-merge. Empty series →
``skipped``; 429/network → ``failed``. Dhan's ~15:14 ceiling is not a
sync error — Upstox tail repair is a separate script.
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
    skipped: list[str]


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
    ranges: dict[str, list[tuple[datetime, datetime]]] | None = None,
) -> SyncResult:
    """Fetch in parallel, convert, store.

    Concurrency is exactly *max_workers*. Empty/no-data lands in
    ``skipped``; only real errors (429/network) land in ``failed``.
    """
    tf = Timeframe(timeframe) if isinstance(timeframe, str) else timeframe
    if not instruments:
        return SyncResult(0, 0, 0, [], [])

    # ponytail: key is always "dhan" so rate-limit tables resolve correctly;
    # dry-run paper broker still works under this name (provider fallback).
    fetcher = ParallelHistoryFetcher({"dhan": broker}, max_workers=max_workers)

    fetched = written = 0
    failed: list[str] = []
    skipped: list[str] = []
    for bi, batch in enumerate(batched(instruments, batch_size), 1):
        log.info("simple_sync: batch %d (%d symbols)", bi, len(batch))

        results, fetch_errors = fetcher.fetch(
            list(batch), tf, start, end, ranges=ranges,
        )

        frames: list[pd.DataFrame] = []
        for inst in batch:
            inst_id = str(inst.instrument_id)
            series = results.get(inst_id)
            if series is None or not series.candles:
                if any(inst_id in e and "empty" not in e for e in fetch_errors):
                    failed.append(inst.symbol)
                else:
                    skipped.append(inst.symbol)
                    log.debug("simple_sync: no data for %s (skipped)", inst.symbol)
                continue
            frames.append(series_to_frame(series, inst.symbol))
            fetched += 1

        if frames:
            written += store.upsert(pd.concat(frames, ignore_index=True))

        log.info(
            "simple_sync: batch %d done (fetched=%d written=%d failed=%d skipped=%d)",
            bi, fetched, written, len(failed), len(skipped),
        )

    if failed:
        log.warning("simple_sync: %d failed: %s", len(failed), failed[:5])
    if skipped:
        log.info("simple_sync: %d skipped (no data)", len(skipped))
    log.info(
        "simple_sync: %d/%d fetched, %d rows written",
        fetched, len(instruments), written,
    )
    return SyncResult(len(instruments), fetched, written, failed, skipped)


__all__ = ["SyncResult", "simple_sync", "series_to_frame"]
