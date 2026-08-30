#!/usr/bin/env python3
"""Same-day top-up: fetch only TODAY's missing bars via a same-day-capable broker."""

import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")

from tradex_trading.config.env import load_env_file  # noqa: E402
load_env_file(str(ROOT / ".env.local"))

from tradex_domain.market_calendar import NSE_HOLIDAYS_2026  # noqa: E402
from tradex_trading.runtime.live import build_broker_from_env  # noqa: E402
from tradex_trading.datalake.gap_detector import GapDetector  # noqa: E402
from tradex_trading.datalake.historical_sync import HistoricalSyncService  # noqa: E402
from tradex_trading.datalake.parallel_fetcher import ParallelHistoryFetcher  # noqa: E402
from tradex_trading.datalake.parquet_storage import ParquetStorage  # noqa: E402
from tradex_trading.datalake.universe import load_universe  # noqa: E402


def main() -> int:
    store = ParquetStorage(ROOT / "data")
    detector = GapDetector(store)
    instruments = load_universe("nifty500")
    now = datetime.now().replace(second=0, microsecond=0)
    day_start = now.replace(hour=9, minute=0)

    gaps = detector.detect(
        instruments, start=day_start, end=now,
        timeframe="1m", bar_freq="1min",
        holidays=NSE_HOLIDAYS_2026, min_gap_stamps=15,
    )
    targets = [inst for inst, _ in gaps]
    print(f"[today-topup] {len(targets)}/{len(instruments)} symbols need today's bars")
    if not targets:
        return 0

    brokers: dict = {}
    for name in ("dhan", "upstox"):
        try:
            b = build_broker_from_env(name)
            b.connect()
            brokers[name] = b
        except Exception as exc:
            print(f"[today-topup] {name} unavailable: {exc}")
    if not brokers:
        print("[today-topup] no brokers available")
        return 1
    fetcher = ParallelHistoryFetcher(brokers, max_workers=4)

    svc = HistoricalSyncService(store=store, detector=detector)
    written = svc._fetch_and_upsert(fetcher, targets, "1m", day_start, now, batch_size=20)
    print(f"[today-topup] DONE written={written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
