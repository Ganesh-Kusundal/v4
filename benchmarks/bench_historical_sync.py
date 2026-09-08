#!/usr/bin/env python3
"""Benchmark historical sync orchestration with an in-memory broker/store.

Usage:
    python benchmarks/bench_historical_sync.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_domain import Equity
from tradex_trading.datalake.historical_sync import SyncOrchestrator

N_LIST = [50, 250, 500]


def _insts(n):
    return [Equity.of("NSE", f"SYM{i}") for i in range(n)]


def run(n, cost):
    store = MagicMock()
    store.upsert.return_value = 0
    fetcher = MagicMock()
    def _fetch(instruments, tf, start, end, ranges=None):
        time.sleep(cost * len(instruments))
        return {}, []
    fetcher.fetch.side_effect = _fetch
    svc = SyncOrchestrator(store, fetcher, None)
    instruments = _insts(n)
    start = datetime.now(UTC) - timedelta(days=1)
    end = datetime.now(UTC)
    t0 = time.monotonic()
    result = svc.sync(instruments, "1d", start, end)
    return time.monotonic() - t0, result


def main() -> int:
    print(f"{'N':>5} {'history cost/inst':>18} {'sync wall(s)':>12}")
    print("-" * 40)
    results = []
    cost = 20e-6
    for n in N_LIST:
        wall, result = run(n, cost)
        print(f"{n:>5} {cost * 1e6:>15.0f}us {wall:>12.3f}")
        results.append({
            "n_instruments": n,
            "history_cost_per_inst_us": cost * 1e6,
            "sync_wall_s": round(wall, 4),
            "requested": result.requested,
        })

    out = ROOT / ".benchmarks" / "historical_sync.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
