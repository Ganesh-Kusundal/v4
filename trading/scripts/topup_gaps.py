#!/usr/bin/env python3
"""Top-up gap ranges that the primary broker cannot serve.

Detects residual gaps (> min_gap_stamps) across a universe, then re-fetches
only the affected date span for those symbols from a designated filler
broker (default: upstox) and upserts. Idempotent: overlapping rows replace,
existing rows stay.

Usage::

    python trading/scripts/topup_gaps.py --universe nifty500 \
        --filler upstox --months 3 --timeframe 1m
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

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

log = logging.getLogger("tradex.scripts.topup")


def _series_to_frame(series, symbol: str) -> pd.DataFrame:
    """Convert a HistoricalSeries to a DataFrame for ParquetStorage.upsert."""
    from tradex_domain.market_calendar import to_ist_naive

    rows = []
    for c in series.candles:
        rows.append({
            "symbol": symbol,
            "exchange": str(c.instrument.exchange),
            "kind": "equity",
            "timeframe": str(c.timeframe.value),
            "timestamp": to_ist_naive(c.timestamp),
            "open": float(c.ohlc.open.value),
            "high": float(c.ohlc.high.value),
            "low": float(c.ohlc.low.value),
            "close": float(c.ohlc.close.value),
            "volume": float(c.volume.value),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fill residual gaps via filler broker")
    p.add_argument("--universe", default="nifty500",
                   choices=["nifty50", "nifty100", "nifty200", "nifty500"])
    p.add_argument("--timeframe", default="1m")
    p.add_argument("--months", type=int, default=3)
    p.add_argument("--filler", default="upstox", choices=["upstox", "dhan"],
                   help="Broker used to serve the gapped spans (default: upstox)")
    p.add_argument("--data-root", default=None)
    p.add_argument("--min-gap-stamps", type=int, default=15,
                   help="Only spans with a gap >= N stamps qualify (default: 15)")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    start = datetime.now() - timedelta(days=args.months * 30)
    end = datetime.now()

    instruments = {i.symbol: i for i in load_universe(args.universe)}
    data_root = Path(args.data_root) if args.data_root else ROOT / "data"
    store = ParquetStorage(data_root)
    detector = GapDetector(store)

    log.info("Detecting qualifying gaps (%d instruments)...", len(instruments))
    t0 = time.perf_counter()
    gaps = detector.detect(
        list(instruments.values()), start=start, end=end,
        timeframe=args.timeframe, bar_freq="1min",
        holidays=NSE_HOLIDAYS_2026, min_gap_stamps=args.min_gap_stamps,
    )
    log.info("Gap detection: %d symbols need top-up (%.1fs)",
             len(gaps), time.perf_counter() - t0)
    if not gaps:
        log.info("Nothing to top up.")
        return 0

    # Fetch each gapped symbol's full span from the filler broker — its own
    # poll caps are handled by the fetcher's auto-chunking.
    from tradex_trading.config.env import load_env_file
    load_env_file(str(ROOT / ".env.local"))
    from tradex_trading.runtime.live import build_broker_from_env
    filler = build_broker_from_env(args.filler)
    filler.connect()
    fetcher = ParallelHistoryFetcher(
        {args.filler: filler}, max_workers=args.workers,
    )

    targets = [instruments[inst.symbol] for inst, _ in gaps]
    batch_size = 25
    total_written = 0
    batches = [targets[i:i + batch_size]
               for i in range(0, len(targets), batch_size)]
    for idx, batch in enumerate(batches, 1):
        t0 = time.perf_counter()
        results = fetcher.fetch(batch, Timeframe(args.timeframe), start, end)
        frames = []
        for inst_id, series in results.items():
            sym = inst_id.split(":")[-1] if ":" in inst_id else inst_id
            frames.append(_series_to_frame(series, sym))
        frames = [f for f in frames if not f.empty]
        if not frames:
            log.warning("Batch %d/%d: nothing served", idx, len(batches))
            continue
        written = store.upsert(pd.concat(frames, ignore_index=True))
        total_written += written
        log.info("Batch %d/%d: %d rows in %.1fs",
                 idx, len(batches), written, time.perf_counter() - t0)

    log.info("=" * 60)
    log.info("Top-up complete: %d rows across %d batches", total_written, len(batches))
    return 0


if __name__ == "__main__":
    sys.exit(main())
