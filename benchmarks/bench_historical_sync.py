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
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_domain import OHLC, Candle, Equity, Timeframe
from tradex_domain.market import HistoricalSeries
from tradex_domain.value_objects import Price, Quantity
from tradex_trading.datalake.historical_sync import SyncOrchestrator

N_LIST = [50, 250, 500]


def _insts(n):
    return [Equity.of("NSE", f"SYM{i}") for i in range(n)]


def _make_series(inst: Equity) -> HistoricalSeries:
    """Minimal 1-candle HistoricalSeries so the full conversion pipeline runs."""
    candle = Candle(
        instrument=inst, timeframe=Timeframe.D1,
        ohlc=OHLC(open=Price(Decimal("100")), high=Price(Decimal("101")),
                  low=Price(Decimal("99")), close=Price(Decimal("100"))),
        volume=Quantity(Decimal("1000")),
        timestamp=datetime(2026, 8, 1, 15, 30, tzinfo=UTC),
    )
    return HistoricalSeries(
        instrument=inst, timeframe=Timeframe.D1,
        candles=[candle],
        start=datetime(2026, 8, 1, tzinfo=UTC),
        end=datetime(2026, 8, 2, tzinfo=UTC),
    )


def run(n, cost):
    store = MagicMock()
    store.upsert.return_value = 0
    fetcher = MagicMock()
    def _fetch(instruments, tf, start, end, ranges=None):
        time.sleep(cost * len(instruments))
        return {str(i.instrument_id): _make_series(i) for i in instruments}, []
    fetcher.fetch.side_effect = _fetch
    svc = SyncOrchestrator(store, fetcher, None)
    instruments = _insts(n)
    start = datetime.now(UTC) - timedelta(days=1)
    end = datetime.now(UTC)
    t0 = time.monotonic()
    result = svc.sync(instruments, "1d", start, end, backoff_base=0)
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
