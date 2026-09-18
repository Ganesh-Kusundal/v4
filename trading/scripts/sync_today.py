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
from tradex_trading.datalake.parquet_storage import ParquetStorage  # noqa: E402
from tradex_trading.datalake.simple_sync import simple_sync  # noqa: E402
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

    from datetime import UTC, datetime

    from tradex_domain.market_calendar import MARKET_OPEN, to_ist_naive

    primary, failover = brokers["dhan"], {k: v for k, v in brokers.items() if k != "dhan"}
    instruments = load_universe("nifty500")

    # sync_today's window: 09:15 to now, IST. Pre-open there is nothing to do.
    now = to_ist_naive(datetime.now(UTC)).replace(second=0, microsecond=0)
    day_start = now.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0)
    if now <= day_start:
        logging.info("pre-open — nothing to sync today")
        print("[today-topup] pre-open, nothing to do")
        return 0

    result = simple_sync(
        primary, store, instruments, "1m", day_start, now,
        skip_existing=True, gaps=GapDetector(store),
        failover_brokers=failover or None,
    )
    print(
        f"[today-topup] DONE fetched={result.fetched} written={result.written} "
        f"failed={len(result.failed)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
