#!/usr/bin/env python3
"""One-shot live sync runner: nifty500 -> lake, real brokers, logged."""

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))
sys.path.insert(0, str(ROOT / "services" / "duckdb-analytics" / "src"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")

from tradex_trading.config.env import load_env_file  # noqa: E402
load_env_file(str(ROOT / ".env.local"))

from tradex_trading.runtime.live import build_broker_from_env  # noqa: E402
from tradex_trading.datalake.historical_sync import HistoricalSyncService  # noqa: E402

brokers = {}
for name in ("dhan", "upstox"):
    try:
        b = build_broker_from_env(name)
        b.connect()
        brokers[name] = b
        print(f"[sync-runner] {name} connected")
    except Exception as e:
        print(f"[sync-runner] {name} unavailable: {e}")

if not brokers:
    sys.exit(1)

svc = HistoricalSyncService()
result = svc.sync(
    universe="nifty500", timeframe="1m", months=3,
    brokers=brokers, filler_broker="upstox",
    batch_size=20, workers=4, min_gap_stamps=15, verify=True,
)
print(f"[sync-runner] DONE requested={result.requested} fetched={result.fetched} "
      f"written={result.written} gaps_remaining={result.gaps_remaining}")
if result.failed:
    print(f"[sync-runner] still gapped: {result.failed[:10]}")
