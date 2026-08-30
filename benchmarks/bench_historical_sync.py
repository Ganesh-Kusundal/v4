#!/usr/bin/env python3
"""Benchmark HistoricalSyncService — gap-detect cost and phase-2 scope.

Mock detector/store/fetcher (no I/O) to measure:
1. Phase-1 gap-detection scaling with universe size.
2. Phase-2 scope win: re-detecting only phase-1 symbols vs the full
   universe when most symbols were already clean.

Usage:
    python benchmarks/bench_historical_sync.py
Ponytail: stdlib only, self-contained, mock detector costs O(n).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_domain import Equity
from tradex_trading.datalake.historical_sync import HistoricalSyncService

N_LIST = [50, 250, 500]
GAPPED_FRACTION = 0.05  # a healthy lake: only ~5% of symbols need data


def _insts(n):
    return [Equity.of("NSE", f"SYM{i}") for i in range(n)]


def _detector(n, gapped_n, cost_per_call):
    """Mock detector: records universe size per call, sleeps per instrument."""
    calls = []

    def detect(instruments, **kwargs):
        calls.append(len(instruments))
        time.sleep(cost_per_call * len(instruments))
        insts = instruments[:gapped_n]
        return [(i, [(datetime.now(UTC), datetime.now(UTC))]) for i in insts]

    det = MagicMock()
    det.detect = detect
    return det, calls


def _service(n, gapped_n, cost):
    det, calls = _detector(n, gapped_n, cost)
    svc = HistoricalSyncService.__new__(HistoricalSyncService)
    svc._store = MagicMock()
    svc._store.upsert.return_value = 1
    svc._detector = det
    svc._fetcher = MagicMock()  # unused: brokers injected per-call
    svc._blacklist_path = Path(ROOT / ".benchmarks" / ".bench_blacklist.json")
    svc._blacklist_path.unlink(missing_ok=True)  # keep runs independent
    svc._holidays = frozenset()
    fetcher = MagicMock()
    fetcher.fetch.return_value = {}
    return svc, fetcher, calls


def run(n, gapped_n, cost):
    svc, _fetcher, calls = _service(n, gapped_n, cost)
    t0 = time.monotonic()
    with patch_universe(n):
        svc.sync(brokers={"dhan": MagicMock()}, filler_broker=None)
    wall = time.monotonic() - t0
    return wall, calls


def patch_universe(n):
    from unittest.mock import patch
    insts = _insts(n)
    return patch(
        "tradex_trading.datalake.historical_sync.load_universe",
        return_value=insts,
    )


def main() -> int:
    print(f"{'N':>5} {'gapped':>7} {'detect cost/inst':>16} "
          f"{'sync wall(s)':>12} {'detect calls (sizes)':>30}")
    print("-" * 78)
    results = []
    # per-instrument detect cost small enough to be measurable, large enough
    # to show the scope difference: 20us per instrument scanned.
    cost = 20e-6
    for n in N_LIST:
        gapped = max(1, int(n * GAPPED_FRACTION))
        wall, calls = run(n, gapped, cost)
        print(f"{n:>5} {gapped:>7} {cost * 1e6:>13.0f}us "
              f"{wall:>12.3f} {calls!s:>30}")
        results.append({
            "n_instruments": n, "gapped": gapped,
            "detect_cost_per_inst_us": cost * 1e6,
            "sync_wall_s": round(wall, 4),
            "detect_call_sizes": calls,
        })

    # Scope win: phase-2/3 detection is O(gapped) instead of O(universe).
    n, gapped = 500, 25
    full_cost = sum(c for c in [500, 500, 500]) * cost  # old: 3 x O(universe)
    det_sizes = results[-1]["detect_call_sizes"]
    scoped_cost = sum(det_sizes) * cost
    print(f"\nPhase-2/3 scope (N={n}, gapped={gapped}):")
    print(f"  old-style (3 x full-universe detect): ~{full_cost * 1000:.1f}ms")
    print(f"  scoped  (sizes {det_sizes}):          ~{scoped_cost * 1000:.1f}ms")
    print(f"  saving: {100 * (1 - scoped_cost / full_cost):.0f}%")

    out = ROOT / ".benchmarks" / "historical_sync.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
