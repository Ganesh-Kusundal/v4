#!/usr/bin/env python3
"""Range-scoped gap filler — fetches ONLY each detected missing span.

Instead of re-requesting the whole 90-day window per symbol, this groups
GapDetector's missing ranges into day-clusters and fetches each cluster once
for its affected symbols. An EOD tail top-up drops from ~35 min to ~3.
"""

import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")
log = logging.getLogger("fill-gaps")

from tradex_trading.config.env import load_env_file  # noqa: E402

load_env_file(str(ROOT / ".env.local"))

from tradex_domain.market_calendar import NSE_HOLIDAYS_2026  # noqa: E402

from tradex_trading.datalake.gap_detector import GapDetector  # noqa: E402
from tradex_trading.datalake.parquet_storage import ParquetStorage  # noqa: E402
from tradex_trading.datalake.simple_sync import simple_sync  # noqa: E402
from tradex_trading.datalake.universe import load_universe  # noqa: E402
from tradex_trading.runtime.live import build_broker_from_env  # noqa: E402


def _scan(detector, instruments, start, end, *, min_gap_stamps, include_open_stamps,
          tail_days):
    """One gap scan with this run's floor, edge policy and scan window.

    ``tail_days`` bounds the scan to each symbol's recent data, which is right
    for the daily top-up and wrong for a repair: with the default 7 a hole from
    three weeks ago is never detected, let alone filled.
    """
    return detector.scan(
        instruments, start=start, end=end, timeframe="1m", bar_freq="1min",
        holidays=NSE_HOLIDAYS_2026, min_gap_stamps=min_gap_stamps,
        max_workers=8, tail_days=tail_days or None,
        include_open_stamps=include_open_stamps,
    )


def _log_scan(found, min_gap_stamps: int) -> None:
    """Report what the floor and the edge classification are holding back.

    Without this the run prints only the gaps it acted on, so a hole sitting
    under the floor (a 14-stamp tail under a 15-stamp floor, on 2026-08-31)
    reads as "nothing to do" while the lake stays short.
    """
    log.info("scan: %d gapped, %d complete, %d edge-only",
             found.gapped_symbols, found.complete_symbols, len(found.edge_only))
    if found.open_missing_symbols or found.close_missing_symbols:
        log.info("  session-edge bars: %d symbols missing the 09:15 open, %d the "
                 "15:30 close (classified, not gapped; pass "
                 "--include-open-stamps to fetch the open bars)",
                 found.open_missing_symbols, found.close_missing_symbols)
    if found.sub_threshold_stamps:
        log.info("  under the %d-stamp floor: %d stamps on %d symbols "
                 "(lower --min-gap-stamps to fetch them)",
                 min_gap_stamps, found.sub_threshold_stamps,
                 len(found.sub_threshold_symbols))


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Range-scoped gap filler with quota-backoff")
    p.add_argument("--yesterday", action="store_true",
                   help="Only fill gaps up to yesterday's close (skip today's live tail)")
    p.add_argument("--min-gap-stamps", type=int, default=15,
                   help="Only holes of at least N stamps are fetched; smaller "
                        "ones are counted and reported (default: 15)")
    p.add_argument("--include-open-stamps", action="store_true",
                   help="Also fetch the missing 09:15 session-open bars, which "
                        "are otherwise classified rather than treated as holes "
                        "(the 15:30 close is served by neither broker)")
    p.add_argument("--tail-days", type=int, default=7,
                   help="Scan only each symbol's last N days (0 = the whole "
                        "window; use 0 to repair older holes) (default: 7)")
    args = p.parse_args(argv)

    store = ParquetStorage(ROOT / "data")
    detector = GapDetector(store)
    instruments = load_universe("nifty500")
    now = datetime.now().replace(second=0, microsecond=0)
    start = (now - timedelta(days=90)).replace(hour=0, minute=0,
                                               second=0, microsecond=0)

    # Window end: today (live tail included) or yesterday close (EOD-complete).
    if args.yesterday:
        from tradex_domain.market_calendar import MARKET_CLOSE
        yesterday = (now - timedelta(days=1)).date()
        end = min(
            datetime.combine(yesterday, MARKET_CLOSE),
            now,
        )
        if end < start:
            print("[fill-gaps] yesterday is before the 90-day window")
            return 0
    else:
        end = now

    log.info("detecting gaps (window %s -> %s)...", start.date(), end.date())
    found = _scan(detector, instruments, start, end,
                  min_gap_stamps=args.min_gap_stamps,
                  include_open_stamps=args.include_open_stamps,
                  tail_days=args.tail_days)
    _log_scan(found, args.min_gap_stamps)
    gaps = found.gaps
    if not gaps:
        print(f"[fill-gaps] nothing at or above {args.min_gap_stamps} stamps "
              f"(edge-only={len(found.edge_only)}, "
              f"sub-threshold={found.sub_threshold_stamps} stamps)")
        return 0

    # ---- cluster missing ranges by contiguous day-spans ----
    clusters: dict[tuple[datetime, datetime], list] = defaultdict(list)
    for inst, ranges in gaps:
        for gs, ge in ranges:
            key = (datetime.combine(gs.date(), datetime.min.time()),
                   datetime.combine(ge.date(), datetime.min.time())
                   + timedelta(days=1))
            clusters[key].append(inst)
    log.info("%d gap clusters", len(clusters))

    # ---- broker: prefer whichever serves this span (same-day -> dhan) ----
    brokers = {}
    for name in ("dhan", "upstox"):
        try:
            b = build_broker_from_env(name)
            b.connect()
            brokers[name] = b
        except Exception as e:
            log.warning("%s unavailable: %s", name, e)
    if not brokers:
        return 1

    # The primary serves the window; the other broker is handed to the fetcher
    # as failover, so a clipped tail or a dead primary falls back instead of
    # leaving a permanent gap. One path, no separate orchestrator.
    primary, failover = brokers["dhan"], {k: v for k, v in brokers.items() if k != "dhan"}

    total_written = 0
    # Biggest clusters first: fills the most symbols earliest and pushes
    # dead singletons (permanent broker failures) to the very end.
    for (c_start, c_end), insts in sorted(
        clusters.items(), key=lambda kv: len(kv[1]), reverse=True
    ):
        log.info("cluster %s -> %s (%d symbols)", c_start.date(), c_end.date(), len(insts))
        result = simple_sync(
            primary, store, insts, "1m", c_start, c_end,
            skip_existing=True, gaps=detector,
            failover_brokers=failover or None,
        )
        total_written += result.written
        log.info("cluster done; total written so far=%d", total_written)

    # ---- verify ----
    remaining = _scan(detector, instruments, start, end,
                      min_gap_stamps=args.min_gap_stamps,
                      include_open_stamps=args.include_open_stamps,
                      tail_days=args.tail_days)
    log.info("verification: %d gapped symbols remain", remaining.gapped_symbols)
    _log_scan(remaining, args.min_gap_stamps)
    print(f"[fill-gaps] DONE written={total_written} "
          f"gapped_remaining={remaining.gapped_symbols}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
