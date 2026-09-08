#!/usr/bin/env python3
"""Same-day top-up: fetch only TODAY's bars (gap-aware)."""

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")

from tradex_trading.config.env import load_env_file  # noqa: E402

load_env_file(str(ROOT / ".env.local"))

from tradex_trading.datalake.gap_detector import GapDetector  # noqa: E402
from tradex_trading.datalake.historical_sync import SyncOrchestrator  # noqa: E402
from tradex_trading.datalake.parallel_fetcher import ParallelHistoryFetcher  # noqa: E402
from tradex_trading.datalake.parquet_storage import ParquetStorage  # noqa: E402
from tradex_trading.datalake.universe import load_universe  # noqa: E402
from tradex_trading.runtime.live import build_broker_from_env  # noqa: E402


def main() -> int:
    store = ParquetStorage(ROOT / "data")
    brokers = {}
    for name in ("dhan", "upstox"):
        try:
            b = build_broker_from_env(name)
            b.connect()
            brokers[name] = b
            logging.info("connected: %s", name)
        except Exception as e:
            logging.warning("skip %s: %s", name, e)

    if not brokers:
        logging.error("no brokers available")
        return 1

    svc = SyncOrchestrator(store, ParallelHistoryFetcher(brokers), GapDetector(store))
    instruments = load_universe("nifty500")
    result = svc.sync_today(instruments, "1m")
    print(
        f"[today-topup] DONE fetched={result.fetched} written={result.written} "
        f"failed={len(result.failed)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
