"""Simple parallel history fetcher — ponytail edition.

Replaces the complex ParallelHistoryFetcher with direct ThreadPoolExecutor usage.
No failover, no blacklisting, no backoff — just parallel requests with rate limiting.
"""
import concurrent.futures
import logging
from datetime import datetime, timedelta
from typing import Any

from tradex_domain.instruments import Instrument
from tradex_domain.market import HistoricalSeries
from tradex_domain.timeframe import Timeframe

log = logging.getLogger(__name__)


def fetch_history_parallel(
    broker: Any,
    instruments: list[Instrument],
    timeframe: Timeframe | str,
    start: datetime,
    end: datetime,
    *,
    max_workers: int = 5,
) -> tuple[dict[str, HistoricalSeries], set[str]]:
    """Fetch history for all instruments in parallel.

    Returns (results_dict, error_ids).
    ponytail: no-data (recent IPOs) is separate from errors (429/network).
    """
    if isinstance(timeframe, str):
        timeframe = Timeframe(timeframe)

    results: dict[str, HistoricalSeries] = {}
    errors: set[str] = set()

    def fetch_one(inst: Instrument) -> tuple[str, HistoricalSeries | None, bool]:
        try:
            series = broker.history(inst, timeframe, start, end)
            if series and series.candles:
                return str(inst.instrument_id), series, False
            return str(inst.instrument_id), None, False  # no data (IPO)
        except Exception as e:
            log.warning("Failed to fetch %s: %s", inst.instrument_id, e)
            return str(inst.instrument_id), None, True  # error

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_one, inst) for inst in instruments]
        for future in concurrent.futures.as_completed(futures):
            inst_id, series, is_error = future.result()
            if series is not None:
                results[inst_id] = series
            elif is_error:
                errors.add(inst_id)

    log.info("Fetched %d/%d symbols (%d errors)", len(results), len(instruments), len(errors))
    return results, errors


def fetch_history_chunked(
    broker: Any,
    instruments: list[Instrument],
    timeframe: Timeframe | str,
    start: datetime,
    end: datetime,
    *,
    max_days: int = 90,
    max_workers: int = 5,
) -> tuple[dict[str, HistoricalSeries], set[str]]:
    """Fetch history with auto-chunking for long ranges.

    ponytail: ONE flat executor for all (symbol, chunk) pairs.  The old code
    had nested executors (outer=chunks, inner=symbols) which created
    chunks*workers concurrent threads and drained the rate-limiter bucket.
    Now concurrency is exactly *max_workers* no matter how many chunks.

    Returns (results_dict, error_ids).
    ponytail: no-data (recent IPOs) is NOT an error — only exceptions are.
    """
    if isinstance(timeframe, str):
        timeframe = Timeframe(timeframe)

    days = (end - start).days
    if days <= max_days:
        return fetch_history_parallel(broker, instruments, timeframe, start, end, max_workers=max_workers)

    # Build chunks
    chunks: list[tuple[datetime, datetime]] = []
    cur = start
    while cur < end:
        nxt = min(end, cur + timedelta(days=max_days))
        chunks.append((cur, nxt))
        cur = nxt

    log.info("Splitting %d-day range into %d chunks of %d days", days, len(chunks), max_days)

    # Flat task list: (instrument, chunk_start, chunk_end)
    # 20 symbols x 4 chunks = 80 tasks, ONE executor with max_workers=5.
    tasks: list[tuple[Instrument, datetime, datetime]] = [
        (inst, cs, ce) for inst in instruments for cs, ce in chunks
    ]

    chunk_results: dict[str, list[tuple[datetime, HistoricalSeries]]] = {}
    chunk_errors: set[str] = set()

    def fetch_task(inst: Instrument, cs: datetime, ce: datetime) -> tuple[str, datetime, HistoricalSeries | None, bool]:
        try:
            series = broker.history(inst, timeframe, cs, ce)
            if series and series.candles:
                return str(inst.instrument_id), cs, series, False
            return str(inst.instrument_id), cs, None, False  # no data
        except Exception as e:
            log.warning("Failed to fetch %s [%s..%s]: %s", inst.instrument_id, cs.date(), ce.date(), e)
            return str(inst.instrument_id), cs, None, True  # error

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_task, inst, cs, ce) for inst, cs, ce in tasks]
        for future in concurrent.futures.as_completed(futures):
            inst_id, cs, series, is_error = future.result()
            if series is not None:
                chunk_results.setdefault(inst_id, []).append((cs, series))
            elif is_error:
                chunk_errors.add(inst_id)

    # Stitch chunks per instrument (sort by chunk_start, dedup candles)
    stitched: dict[str, HistoricalSeries] = {}
    for inst_id, pairs in chunk_results.items():
        if not pairs:
            continue
        pairs.sort(key=lambda p: p[0])
        merged_candles: list[Any] = []
        seen: set[datetime] = set()
        for _, series in pairs:
            for candle in series.candles:
                if candle.timestamp not in seen:
                    seen.add(candle.timestamp)
                    merged_candles.append(candle)
        merged_candles.sort(key=lambda c: c.timestamp)

        template = pairs[0][1]
        stitched[inst_id] = HistoricalSeries(
            instrument=template.instrument,
            timeframe=template.timeframe,
            candles=merged_candles,
            start=merged_candles[0].timestamp if merged_candles else start,
            end=merged_candles[-1].timestamp if merged_candles else end,
        )

    log.info("Stitched %d symbols from %d chunks (%d tasks, %d workers, %d errors)",
             len(stitched), len(chunks), len(tasks), max_workers, len(chunk_errors))
    return stitched, chunk_errors


__all__ = ["fetch_history_parallel", "fetch_history_chunked"]
