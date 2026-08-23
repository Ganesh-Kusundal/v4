#!/usr/bin/env python3
"""Backfill OHLCV history into the Hive-partitioned Parquet store.

Usage::

    # Live (requires authenticated broker sessions):
    python trading/scripts/backfill_parquet.py \\
        --timeframe 1m --months 3 --broker dhan

    # Dry-run with mock data:
    python trading/scripts/backfill_parquet.py --dry-run --limit 5

Design
------
Uses ``ParallelHistoryFetcher`` for concurrent multi-instrument fetch (with
smart dual-broker routing for < 30 day ranges), then upserts into
``ParquetStorage`` in batches to keep memory bounded.

Each batch upsert is idempotent: re-running silently replaces overlapping
rows and GapDetector skips already-complete symbols.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# Ensure project packages are importable
ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_domain import Timeframe  # noqa: E402 — sys.path setup above
from tradex_domain.market_calendar import (  # noqa: E402 — sys.path setup above
    NSE_HOLIDAYS_2026,
)

from tradex_trading.datalake.gap_detector import GapDetector  # noqa: E402 — sys.path setup above
from tradex_trading.datalake.parallel_fetcher import (  # noqa: E402 — sys.path setup above
    ParallelHistoryFetcher,
)
from tradex_trading.datalake.parquet_storage import (  # noqa: E402 — sys.path setup above
    ParquetStorage,
)
from tradex_trading.datalake.universe import load_universe  # noqa: E402 — sys.path setup above

log = logging.getLogger("tradex.scripts.backfill")


def _date_args(months: int) -> tuple[datetime, datetime]:
    end = datetime.now()
    start = end - timedelta(days=months * 30)
    return start, end


def _series_to_frame(series, symbol: str) -> pd.DataFrame:
    """Convert a HistoricalSeries to a DataFrame for ParquetStorage.upsert."""
    from tradex_domain.market_calendar import to_ist_naive

    rows = []
    for c in series.candles:
        # Convert tz-aware timestamps to IST (datalake contract: tz-naive IST)
        ts = to_ist_naive(c.timestamp)
        rows.append({
            "symbol": symbol,
            "exchange": str(c.instrument.exchange),
            "kind": "equity",  # ponytail: backfill is equity-only for now
            "timeframe": str(c.timeframe.value),
            "timestamp": ts,
            "open": float(c.ohlc.open.value),
            "high": float(c.ohlc.high.value),
            "low": float(c.ohlc.low.value),
            "close": float(c.ohlc.close.value),
            "volume": float(c.volume.value),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _build_brokers(args):
    """Build broker dict for ParallelHistoryFetcher. Uses auto-auth via env."""
    brokers = {}
    if args.dry_run:
        from tradex_brokers.paper.adapter import PaperBroker
        brokers["paper"] = PaperBroker()
        return brokers
    # Load credentials from .env.local
    from tradex_trading.config.env import load_env_file
    load_env_file(str(ROOT / ".env.local"))
    from tradex_trading.runtime.live import build_broker_from_env
    if args.broker in ("dhan", "both"):
        b = build_broker_from_env("dhan")
        b.connect()
        brokers["dhan"] = b
    if args.broker in ("upstox", "both"):
        b = build_broker_from_env("upstox")
        b.connect()
        brokers["upstox"] = b
    return brokers


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Backfill OHLCV into ParquetStorage")
    p.add_argument("--universe", default="nifty50",
                   choices=["nifty50", "nifty100", "nifty200", "nifty500"],
                   help="Universe CSV to load (default: nifty50)")
    p.add_argument("--timeframe", default="1m", help="Resolution (default: 1m)")
    p.add_argument("--months", type=int, default=3, help="Trailing months (default: 3)")
    p.add_argument("--broker", default="dhan", choices=["dhan", "upstox", "both", "paper"],
                   help="Broker adapter (default: dhan)")
    p.add_argument("--data-root", default=None,
                   help="Parquet base path (default: data/)")
    p.add_argument("--batch-size", type=int, default=20,
                   help="Symbols per batch (default: 20)")
    p.add_argument("--workers", type=int, default=4,
                   help="Concurrent fetch threads (default: 4)")
    p.add_argument("--backoff-base", type=float, default=60.0,
                   help="Initial sleep after a throttled batch, seconds (default: 60)")
    p.add_argument("--backoff-max", type=float, default=900.0,
                   help="Backoff ceiling, seconds (default: 900)")
    p.add_argument("--dry-run", action="store_true",
                   help="Use PaperBroker with synthetic data")
    p.add_argument("--limit", type=int, default=0,
                   help="Cap symbols processed (0 = no limit)")
    p.add_argument("--skip-existing", action="store_true",
                   help="Skip symbols with full coverage (gap-aware)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    start, end = _date_args(args.months)
    log.info("Backfill window: %s -> %s (%d months, %s)",
             start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), args.months, args.timeframe)

    # Load universe
    instruments = load_universe(args.universe)
    if args.limit > 0:
        instruments = instruments[:args.limit]
    log.info("Universe: %s (%d instruments)", args.universe, len(instruments))

    # Build broker(s) + fetcher + store
    brokers = _build_brokers(args)
    if not brokers:
        log.error("No brokers available")
        return 1
    fetcher = ParallelHistoryFetcher(brokers, max_workers=args.workers)
    data_root = Path(args.data_root) if args.data_root else ROOT / "data"
    store = ParquetStorage(data_root)
    gap_detector = GapDetector(store)

    # Gap-aware skip
    to_fetch = instruments
    if args.skip_existing:
        log.info("Detecting gaps...")
        gaps = gap_detector.detect(
            instruments, start=start, end=end,
            timeframe=args.timeframe, bar_freq="1min",
            holidays=NSE_HOLIDAYS_2026,
        )
        missing = {inst.symbol for inst, ranges in gaps if ranges}
        to_fetch = [inst for inst in instruments if inst.symbol in missing]
        log.info("Gap detection: %d need data, %d complete",
                 len(missing), len(instruments) - len(missing))

    if not to_fetch:
        log.info("Nothing to backfill.")
        return 0

    # Batch fetch -> upsert -> discard, with adaptive throttle backoff:
    # brokers (Dhan historical) enforce burst quotas — a batch with missing
    # symbols usually means 429s, so sleep exponentially and requeue it
    # instead of burning through the rest of the universe on failure spam.
    total_written = 0
    batches = [to_fetch[i:i + args.batch_size] for i in range(0, len(to_fetch), args.batch_size)]

    backoff_s = float(args.backoff_base)
    max_backoff_s = float(args.backoff_max)
    queue = list(enumerate(batches, 1))
    attempt: dict[int, int] = {}
    max_attempts = 6

    while queue:
        batch_idx, batch = queue.pop(0)
        t0 = time.perf_counter()
        try:
            results = fetcher.fetch(batch, Timeframe(args.timeframe), start, end)
        except Exception as exc:
            log.exception("Batch %d failed: %s", batch_idx, exc)
            results = {}

        n_expected = len(batch)
        if len(results) < n_expected:
            attempt[batch_idx] = attempt.get(batch_idx, 0) + 1
            if len(results) == 0:
                log.warning(
                    "Batch %d/%d: empty (%d/%d attempts) — broker throttling?",
                    batch_idx, len(batches), attempt[batch_idx], max_attempts,
                )
            else:
                log.warning(
                    "Batch %d/%d: partial %d/%d symbols (%d/%d attempts)",
                    batch_idx, len(batches), len(results), n_expected,
                    attempt[batch_idx], max_attempts,
                )
            if attempt[batch_idx] < max_attempts:
                # Requeue at the back so a different batch drains the
                # recovering quota window in the meantime.
                queue.append((batch_idx, batch))
                log.info("Backing off %.0fs before next batch", backoff_s)
                time.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, max_backoff_s)
            else:
                log.error(
                    "Batch %d/%d: giving up after %d attempts",
                    batch_idx, len(batches), max_attempts,
                )
                backoff_s = float(args.backoff_base)
            continue

        backoff_s = float(args.backoff_base)
        frames = []
        for inst_id, series in results.items():
            sym = inst_id.split(":")[-1] if ":" in inst_id else inst_id
            df = _series_to_frame(series, sym)
            if not df.empty:
                frames.append(df)

        if not frames:
            continue

        combined = pd.concat(frames, ignore_index=True)
        written = store.upsert(combined)
        total_written += written
        elapsed = time.perf_counter() - t0
        log.info("Batch %d/%d: %d rows in %.1fs (%.0f rows/s)",
                 batch_idx, len(batches), written, elapsed,
                 written / elapsed if elapsed > 0 else 0)

    log.info("=" * 60)
    log.info("Backfill complete: %d rows across %d batches", total_written, len(batches))
    return 0


if __name__ == "__main__":
    sys.exit(main())
