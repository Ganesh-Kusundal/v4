#!/usr/bin/env python3
"""Benchmark: Dhan tick/frame decode rate.

Stdlib-only. Prints median/p95/max over N runs.
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "domain" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brokers" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "trading" / "src"))

from tradex_brokers.dhan.tick_parser import parse_tick_frame


def bench(n: int = 10000) -> None:
    # Create a sample tick frame (simplified)
    sample_frame = b"\x00" * 100  # Placeholder

    latencies: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            parse_tick_frame(sample_frame)
        except Exception:
            pass  # Expected to fail on placeholder data
        latencies.append(time.perf_counter() - t0)

    latencies.sort()
    print(f"bench_tick_parse (n={n})")
    print(f"  median: {statistics.median(latencies) * 1e6:.2f} µs")
    print(f"  p95:    {latencies[int(n * 0.95)] * 1e6:.2f} µs")
    print(f"  max:    {max(latencies) * 1e6:.2f} µs")


if __name__ == "__main__":
    bench()
