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

ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_domain.market_calendar import (  # noqa: E402 — sys.path setup above
    NSE_HOLIDAYS_2026,
)

from tradex_trading.datalake.gap_detector import GapDetector  # noqa: E402 — sys.path setup above
from tradex_trading.datalake.parquet_storage import (  # noqa: E402 — sys.path setup above
    ParquetStorage,
)
from tradex_trading.datalake.simple_sync import simple_sync  # noqa: E402 — sys.path setup above
from tradex_trading.datalake.universe import load_universe  # noqa: E402 — sys.path setup above

log = logging.getLogger("tradex.scripts.topup")


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
                   help="Only spans with a gap >= N stamps qualify; smaller "
                        "ones are counted and reported (default: 15)")
    p.add_argument("--include-open-stamps", action="store_true",
                   help="Also top up symbols whose only shortfall is the "
                        "09:15 session-open bar (repairable; the 15:30 close "
                        "is served by neither broker)")
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
    found = detector.scan(
        list(instruments.values()), start=start, end=end,
        timeframe=args.timeframe, bar_freq="1min",
        holidays=NSE_HOLIDAYS_2026, min_gap_stamps=args.min_gap_stamps,
        include_open_stamps=args.include_open_stamps,
    )
    gaps = found.gaps
    log.info("Gap detection: %d symbols need top-up (%.1fs)",
             len(gaps), time.perf_counter() - t0)
    if found.open_missing_symbols or found.close_missing_symbols:
        log.info("  session-edge bars: %d symbols missing the 09:15 open, %d the "
                 "15:30 close (classified, not gaps)",
                 found.open_missing_symbols, found.close_missing_symbols)
    if found.sub_threshold_stamps:
        log.info("  under the %d-stamp floor: %d stamps on %d symbols",
                 args.min_gap_stamps, found.sub_threshold_stamps,
                 len(found.sub_threshold_symbols))
    if not gaps:
        log.info("Nothing to top up.")
        return 0

    # Filler broker (default Upstox) — this is the residual/tail path, not
    # the Dhan sync path. Caller-owned ranges; no re-detect inside sync.
    from tradex_trading.config.env import load_env_file
    load_env_file(str(ROOT / ".env.local"))
    from tradex_trading.runtime.live import build_broker_from_env
    filler = build_broker_from_env(args.filler)
    filler.connect()

    targets = [instruments[inst.symbol] for inst, _ in gaps]
    ranges = {str(inst.instrument_id): r for inst, r in gaps}
    result = simple_sync(
        filler, store, targets, args.timeframe, start, end,
        ranges=ranges, max_workers=args.workers,
    )

    log.info("=" * 60)
    log.info(
        "Top-up complete: %d rows written, %d failed, %d skipped: %s",
        result.written, len(result.failed), len(result.skipped), result.failed[:5],
    )
    return 0 if not result.failed else 1


if __name__ == "__main__":
    sys.exit(main())
