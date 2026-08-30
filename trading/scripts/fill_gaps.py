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

from tradex_domain import Timeframe  # noqa: E402
from tradex_domain.market_calendar import NSE_HOLIDAYS_2026  # noqa: E402
from tradex_trading.runtime.live import build_broker_from_env  # noqa: E402
from tradex_trading.datalake.gap_detector import GapDetector  # noqa: E402
from tradex_trading.datalake.historical_sync import HistoricalSyncService, _series_to_frame  # noqa: E402
from tradex_trading.datalake.parallel_fetcher import ParallelHistoryFetcher  # noqa: E402
from tradex_trading.datalake.parquet_storage import ParquetStorage  # noqa: E402
from tradex_trading.datalake.universe import load_universe  # noqa: E402

import pandas as pd  # noqa: E402


def _fetch_batch_with_backoff(
    fetcher,
    batch,
    tf,
    c_start,
    c_end,
    max_retries: int = 6,
) -> dict:
    """Fetch one batch, retrying with backoff on rate-limit bursts.

    Distinguishes transient (quota) from permanent failures: if a batch
    makes zero progress across two consecutive attempts, the remaining
    symbols are treated as dead (e.g. not in the broker's registry) and
    given up rather than burning 15 min of backoff on them.
    """
    pending = list(batch)
    merged: dict = {}
    backoff = 30.0
    zero_progress_streak = 0
    for attempt in range(1, max_retries + 1):
        if not pending:
            break
        results = fetcher.fetch(pending, tf, c_start, c_end)
        merged.update(results)
        succeeded_ids = set(results.keys())
        still_pending = [
            inst for inst in pending
            if str(inst.instrument_id) not in succeeded_ids
        ]
        if not still_pending:
            break
        if len(still_pending) < len(pending):
            # Partial success — quota is flowing; keep backing off for rest.
            zero_progress_streak = 0
            backoff = 30.0
        else:
            zero_progress_streak += 1
            if zero_progress_streak >= 2:
                log.warning(
                    "batch made no progress twice — giving up on %d symbols "
                    "(permanent failure, e.g. unknown to broker)",
                    len(still_pending),
                )
                break
        log.warning(
            "batch rate-limited (attempt %d/%d) — sleeping %.0fs",
            attempt, max_retries, backoff,
        )
        import time
        time.sleep(backoff)
        backoff = min(backoff * 2, 480.0)
        pending = still_pending
    return merged


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Range-scoped gap filler with quota-backoff")
    p.add_argument("--yesterday", action="store_true",
                   help="Only fill gaps up to yesterday's close (skip today's live tail)")
    args = p.parse_args(argv)

    store = ParquetStorage(ROOT / "data")
    detector = GapDetector(store)
    instruments = load_universe("nifty500")
    tf = Timeframe("1m")
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
    gaps = detector.detect(instruments, start=start, end=end,
                           timeframe="1m", bar_freq="1min",
                           holidays=NSE_HOLIDAYS_2026, min_gap_stamps=15)
    log.info("%d gapped symbols", len(gaps))
    if not gaps:
        print("[fill-gaps] lake is clean — nothing to do")
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

    total_written = 0
    # Biggest clusters first: fills the most symbols earliest and pushes
    # dead singletons (permanent broker failures) to the very end.
    for (c_start, c_end), insts in sorted(
        clusters.items(), key=lambda kv: len(kv[1]), reverse=True
    ):
        # same-day spans must go to a same-day-capable broker (dhan);
        # older spans can use any (prefer upstox to spare dhan quota)
        is_today = c_end.date() >= now.date()
        pref = ["dhan"] if is_today else (
            ["upstox"] if "upstox" in brokers else ["dhan"])
        chosen = {n: brokers[n] for n in pref if n in brokers} or brokers
        fetcher = ParallelHistoryFetcher(chosen, max_workers=4)
        log.info("cluster %s -> %s (%d symbols, broker=%s)",
                 c_start.date(), c_end.date(), len(insts), list(chosen))

        for i in range(0, len(insts), 20):
            batch = insts[i:i + 20]
            results = _fetch_batch_with_backoff(fetcher, batch, tf, c_start, c_end)
            frames = []
            for inst_id, series in results.items():
                sym = inst_id.split(":")[-1] if ":" in inst_id else inst_id
                f = _series_to_frame(series, sym)
                if not f.empty:
                    frames.append(f)
            if frames:
                total_written += store.upsert(
                    pd.concat(frames, ignore_index=True))
        log.info("cluster done; total written so far=%d", total_written)

    # ---- verify ----
    remaining = detector.detect(instruments, start=start, end=end,
                                timeframe="1m", bar_freq="1min",
                                holidays=NSE_HOLIDAYS_2026, min_gap_stamps=15)
    log.info("verification: %d gapped symbols remain", len(remaining))
    print(f"[fill-gaps] DONE written={total_written} "
          f"gapped_remaining={len(remaining)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
