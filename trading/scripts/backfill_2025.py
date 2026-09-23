#!/usr/bin/env python3
"""Backfill the 2025 calendar year for NIFTY-500, quarter by quarter.

Uses the standard ParallelHistoryFetcher + ParquetStorage pipeline. The limiter
is configured so Dhan serializes through its token bucket (burst=1, ~5/s —
inside its measured clean zone) and any 429 is absorbed (wait_cooldown) rather
than failing the batch, so each quarter can be fetched in a few large batches
instead of many tiny ones.

RESUMABLE: completed quarters are recorded in data/backfill_2025.json, so a
killed or partial run picks up where it left off. ParquetStorage.upsert is
idempotent, so re-running a completed quarter is a cheap no-op.

Usage:
    python trading/scripts/backfill_2025.py            # run remaining quarters
    python trading/scripts/backfill_2025.py Q1 Q3      # run specific quarters
    python trading/scripts/backfill_2025.py --reset    # clear progress, start over
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_trading.datalake.paths import datalake_root  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
#: Progress state follows the lake, not the repo, so a run pointed at an
#: override root ($TRADEX_DATALAKE_ROOT) resumes against that root's own book.
PROGRESS_FILE = Path(datalake_root()) / "backfill_2025.json"
BATCH_SIZE = 120  # safe now that the limiter serializes Dhan at 5/s

QUARTERS = {
    "Q1": (datetime(2025, 1, 1, tzinfo=IST), datetime(2025, 3, 31, 23, 59, 59, tzinfo=IST)),
    "Q2": (datetime(2025, 4, 1, tzinfo=IST), datetime(2025, 6, 30, 23, 59, 59, tzinfo=IST)),
    "Q3": (datetime(2025, 7, 1, tzinfo=IST), datetime(2025, 9, 30, 23, 59, 59, tzinfo=IST)),
    "Q4": (datetime(2025, 10, 1, tzinfo=IST), datetime(2025, 12, 31, 23, 59, 59, tzinfo=IST)),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("backfill-2025")

from tradex_trading.config.env import load_env_file  # noqa: E402

load_env_file(str(ROOT / ".env.local"))

import pandas as pd  # noqa: E402

from tradex_trading.datalake.simple_sync import series_to_frame  # noqa: E402
from tradex_trading.datalake.parallel_fetcher import (  # noqa: E402
    ParallelHistoryFetcher,
)
from tradex_trading.datalake.parquet_storage import ParquetStorage  # noqa: E402
from tradex_trading.datalake.universe import load_universe  # noqa: E402
from tradex_trading.runtime.live import build_broker_from_env  # noqa: E402


def _load_progress() -> set[str]:
    if PROGRESS_FILE.exists():
        return set(json.loads(PROGRESS_FILE.read_text()).get("done", []))
    return set()


def _save_progress(done: set[str]) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(
        json.dumps({"done": sorted(done)}, indent=2), encoding="utf-8"
    )


def main(argv: list[str]) -> int:
    store = ParquetStorage(Path(datalake_root()))
    instruments = load_universe("nifty500")
    brokers = {}
    for name in ("dhan", "upstox"):
        try:
            b = build_broker_from_env(name)
            b.connect()
            brokers[name] = b
            log.info("connected: %s", name)
        except Exception as e:
            log.warning("skip %s: %s", name, e)
    if not brokers:
        log.error("no brokers available")
        return 1

    fetcher = ParallelHistoryFetcher(brokers, max_workers=4)
    done = _load_progress()

    if "--reset" in argv:
        done = set()
        _save_progress(done)
        log.info("progress reset")

    quarters = [a for a in argv[1:] if not a.startswith("--")] or list(QUARTERS)

    for q in quarters:
        if q not in QUARTERS:
            log.error("unknown quarter %r (use Q1..Q4)", q)
            return 1
        if q in done:
            log.info("===== %s  already done — skipping =====", q)
            continue
        start, end = QUARTERS[q]
        log.info("===== %s  %s -> %s =====", q, start.date(), end.date())

        total_written = 0
        total_fetched = 0
        batches = [
            instruments[i:i + BATCH_SIZE]
            for i in range(0, len(instruments), BATCH_SIZE)
        ]
        t0 = time.monotonic()
        for bi, batch in enumerate(batches, 1):
            results, errors = fetcher.fetch(batch, "1m", start, end)
            frames = []
            for inst_id, series in results.items():
                df = series_to_frame(series, inst_id.split(":")[-1])
                if not df.empty:
                    frames.append(df)
            if frames:
                total_written += store.upsert(pd.concat(frames, ignore_index=True))
            total_fetched += len(results)
            log.info(
                "batch %d/%d: %d ok / %d err  (written=%d)",
                bi, len(batches), len(results), len(errors), total_written,
            )
        dt = time.monotonic() - t0
        log.info(
            "%s complete: %d/%d symbols, %d rows in %.0fs",
            q, total_fetched, len(instruments), total_written, dt,
        )
        done.add(q)
        _save_progress(done)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
