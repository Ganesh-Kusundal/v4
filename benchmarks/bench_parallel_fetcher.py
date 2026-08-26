#!/usr/bin/env python3
"""Benchmark ParallelHistoryFetcher — single vs dual broker throughput.

Simulates API latency with mock brokers to measure the parallel fetch
speedup from splitting instruments across Dhan + Upstox.  Routing is
always-split (per-broker poll caps handled by auto-chunking), so both
range scenarios below exercise the same split; only chunk count differs.

Two regimes are measured:
* latency-bound (--latency-ms 200 default): wall time == worker pool bound,
  speedup plateaus at ``max_workers`` regardless of broker count.
* quota-bound (``bench_quota_bound``): tiny latency, enough calls that the
  per-broker historical buckets bind — a second broker ~doubles sustained
  call rate.  This is the regime the always-split routing targets.

Usage:
    python benchmarks/bench_parallel_fetcher.py [--instruments 10 50 100 250 500]

Ponytail: no external deps, stdlib only, self-contained.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

# Ensure project packages are importable
ROOT = Path(__file__).resolve().parent.parent
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_domain import OHLC, Candle, Equity, Timeframe
from tradex_domain.market import HistoricalSeries
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.datalake.parallel_fetcher import ParallelHistoryFetcher

# --------------------------------------------------------------------------- #
# Mock broker with configurable latency
# --------------------------------------------------------------------------- #

BASE = datetime(2026, 8, 1, 9, 15, tzinfo=UTC)


def _make_series(inst: Equity) -> HistoricalSeries:
    candles = [
        Candle(
            instrument=inst, timeframe=Timeframe.M1,
            ohlc=OHLC(open=Price(Decimal("100")), high=Price(Decimal("101")),
                      low=Price(Decimal("99")), close=Price(Decimal("100"))),
            volume=Quantity(Decimal("1000")),
            timestamp=BASE + timedelta(minutes=i),
        )
        for i in range(10)
    ]
    return HistoricalSeries(
        instrument=inst, timeframe=Timeframe.M1,
        candles=candles, start=BASE, end=BASE + timedelta(minutes=9),
    )


def _make_broker(name: str, latency_ms: float) -> MagicMock:
    """Mock broker with fixed latency per history() call."""
    broker = MagicMock()
    latency = latency_ms / 1000.0

    def _history(inst, tf, start, end):
        time.sleep(latency)
        return _make_series(inst)

    broker.history = MagicMock(side_effect=_history)
    broker.name = name
    return broker


# --------------------------------------------------------------------------- #
# Benchmark scenarios
# --------------------------------------------------------------------------- #

def bench_single_broker_sequential(instruments, latency_ms, days):
    """Baseline: one broker, one call at a time (simulated)."""
    broker = _make_broker("dhan", latency_ms)
    end = BASE + timedelta(days=days)
    t0 = time.monotonic()
    for inst in instruments:
        broker.history(inst, Timeframe.M1, BASE, end)
    wall = time.monotonic() - t0
    return wall, len(instruments)


def bench_single_broker_parallel(instruments, latency_ms, days):
    """One broker, 4 parallel workers."""
    broker = _make_broker("dhan", latency_ms)
    fetcher = ParallelHistoryFetcher({"dhan": broker}, max_workers=4)
    end = BASE + timedelta(days=days)
    t0 = time.monotonic()
    results = fetcher.fetch(instruments, Timeframe.M1, BASE, end)
    wall = time.monotonic() - t0
    return wall, len(results)


def bench_dual_broker_parallel(instruments, latency_ms, days):
    """Two brokers, 4 parallel workers, instruments split across both."""
    dhan = _make_broker("dhan", latency_ms)
    upstox = _make_broker("upstox", latency_ms)
    fetcher = ParallelHistoryFetcher({"dhan": dhan, "upstox": upstox}, max_workers=4)
    end = BASE + timedelta(days=days)
    t0 = time.monotonic()
    results = fetcher.fetch(instruments, Timeframe.M1, BASE, end)
    wall = time.monotonic() - t0
    # Count calls per broker
    return wall, len(results), dhan.history.call_count, upstox.history.call_count


QUOTA_N = 40          # calls; dhan bucket = cap 10 @ 5/s -> ~6s solo
QUOTA_LATENCY_MS = 2  # small enough that buckets dominate wall time


def bench_quota_bound():
    """Rate-limit-bound regime: dhan's 5/s bucket binds, upstox's doesn't.

    Fresh limiters per fetcher instance make this deterministic.  Solo Dhan
    drains its own bucket (~6s for QUOTA_N calls); split across two brokers
    each side serves half, so Dhan's bucket refills while Upstox runs —
    roughly half the wall time.  This is the win the split routing exists for.
    """
    instruments = [Equity.of("NSE", f"QB{i:03d}") for i in range(QUOTA_N)]
    end = BASE + timedelta(days=7)

    solo = _make_broker("dhan", QUOTA_LATENCY_MS)
    t0 = time.monotonic()
    ParallelHistoryFetcher({"dhan": solo}, max_workers=4).fetch(
        instruments, Timeframe.M1, BASE, end)
    t_single = time.monotonic() - t0

    dhan = _make_broker("dhan", QUOTA_LATENCY_MS)
    upstox = _make_broker("upstox", QUOTA_LATENCY_MS)
    t0 = time.monotonic()
    ParallelHistoryFetcher({"dhan": dhan, "upstox": upstox}, max_workers=4).fetch(
        instruments, Timeframe.M1, BASE, end)
    t_dual = time.monotonic() - t0

    return (t_single, t_dual,
            dhan.history.call_count, upstox.history.call_count)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(description="Benchmark ParallelHistoryFetcher")
    p.add_argument("--instruments", type=int, nargs="+", default=[10, 50, 100, 250, 500],
                   help="Instrument counts to benchmark")
    p.add_argument("--latency-ms", type=float, default=200.0,
                   help="Simulated API latency per call in ms (default: 200)")
    p.add_argument("--out", default=None,
                   help="Output JSON path (default: .benchmarks/parallel_fetch.json)")
    args = p.parse_args()

    latency = args.latency_ms
    print(f"=" * 72)
    print(f"ParallelHistoryFetcher Benchmark  (API latency: {latency:.0f}ms/call)")
    print(f"=" * 72)

    results: list[dict] = []

    for n in args.instruments:
        instruments = [Equity.of("NSE", f"BENCH{i:04d}") for i in range(n)]

        # --- Short range (7 days): split routing, no chunking ---
        print(f"\n--- {n} instruments, 7 days (split across brokers) ---")

        t_seq, n_seq = bench_single_broker_sequential(instruments, latency, 7)
        print(f"  Sequential (1 broker):  {t_seq:.2f}s  ({n_seq} calls)")

        t_sp, n_sp = bench_single_broker_parallel(instruments, latency, 7)
        speedup_sp = t_seq / t_sp if t_sp > 0 else 0
        print(f"  Parallel (1 broker, 4w): {t_sp:.2f}s  ({n_sp} results, {speedup_sp:.1f}x)")

        t_dp, n_dp, dhan_calls, upstox_calls = bench_dual_broker_parallel(instruments, latency, 7)
        speedup_dp = t_seq / t_dp if t_dp > 0 else 0
        print(f"  Parallel (2 brokers, 4w): {t_dp:.2f}s  ({n_dp} results, {speedup_dp:.1f}x)")
        print(f"    Dhan: {dhan_calls} calls, Upstox: {upstox_calls} calls")

        results.append({
            "scenario": "latency_bound",
            "n_instruments": n, "days": 7, "latency_ms": latency,
            "sequential_s": round(t_seq, 3),
            "single_parallel_s": round(t_sp, 3),
            "dual_parallel_s": round(t_dp, 3),
            "dual_speedup": round(speedup_dp, 2),
            "dhan_calls": dhan_calls, "upstox_calls": upstox_calls,
        })

        # --- Long range (90 days): split + auto-chunked windows ---
        print(f"\n--- {n} instruments, 90 days (split, auto-chunked) ---")

        t_seq90, _ = bench_single_broker_sequential(instruments, latency, 90)
        t_dp90, n_dp90, dhan90, upstox90 = bench_dual_broker_parallel(instruments, latency, 90)
        speedup_90 = t_seq90 / t_dp90 if t_dp90 > 0 else 0
        print(f"  Sequential: {t_seq90:.2f}s")
        print(f"  Parallel (split, 4w): {t_dp90:.2f}s  ({speedup_90:.1f}x)")
        print(f"    Dhan: {dhan90} calls, Upstox: {upstox90} calls (chunked)")

        results.append({
            "scenario": "latency_bound",
            "n_instruments": n, "days": 90, "latency_ms": latency,
            "sequential_s": round(t_seq90, 3),
            "dual_parallel_s": round(t_dp90, 3),
            "dual_speedup": round(speedup_90, 2),
            "dhan_calls": dhan90, "upstox_calls": upstox90,
        })

    # --- Quota-bound regime: where the split actually wins ---
    print(f"\n--- quota-bound: {QUOTA_N} instruments @ {QUOTA_LATENCY_MS:.0f}ms "
          f"(per-broker buckets bind) ---")
    q_single, q_dual, q_dhan, q_upstox = bench_quota_bound()
    q_speedup = q_single / q_dual if q_dual > 0 else 0
    print(f"  Single broker (Dhan bucket binds): {q_single:.2f}s")
    print(f"  Split across both brokers:         {q_dual:.2f}s  ({q_speedup:.1f}x)")
    print(f"    Dhan: {q_dhan} calls, Upstox: {q_upstox} calls")
    results.append({
        "scenario": "quota_bound",
        "n_instruments": QUOTA_N, "days": 7,
        "latency_ms": QUOTA_LATENCY_MS,
        "single_parallel_s": round(q_single, 3),
        "dual_parallel_s": round(q_dual, 3),
        "dual_speedup": round(q_speedup, 2),
        "dhan_calls": q_dhan, "upstox_calls": q_upstox,
    })

    # --- Summary ---
    print(f"\n{'=' * 72}")
    print("SUMMARY (latency-bound ranges — speedup caps at worker count there)")
    print(f"{'=' * 72}")
    print(f"{'N':>5} {'Days':>4} {'Seq(s)':>8} {'Par(s)':>8} {'Speedup':>8} {'Route':>12}")
    print("-" * 50)
    for r in results:
        if r.get("scenario") != "latency_bound":
            continue
        print(f"{r['n_instruments']:>5} {r['days']:>4} "
              f"{r['sequential_s']:>8.2f} {r['dual_parallel_s']:>8.2f} "
              f"{r['dual_speedup']:>7.1f}x {'split':>12}")
    print(f"\nQuota-bound: single={q_single:.2f}s  split={q_dual:.2f}s  "
          f"speedup={q_speedup:.1f}x  <- the win split routing targets")

    # --- Write JSON ---
    out_path = Path(args.out) if args.out else ROOT / ".benchmarks" / "parallel_fetch.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
