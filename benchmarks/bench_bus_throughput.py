#!/usr/bin/env python3
"""Benchmark: ReactiveBus publish/subscribe throughput.

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

from tradex_domain import Equity
from tradex_domain.events import OrderPlaced
from tradex_trading.reactive.bus import ReactiveBus


def bench(n: int = 10000) -> None:
    bus = ReactiveBus()
    eq = Equity.of("NSE", "RELIANCE")
    received: list = []
    bus.of_type(OrderPlaced).subscribe(on_next=lambda e: received.append(e))

    latencies: list[float] = []
    for i in range(n):
        t0 = time.perf_counter()
        bus.publish(OrderPlaced(order=None))
        latencies.append(time.perf_counter() - t0)

    latencies.sort()
    print(f"bench_bus_throughput (n={n})")
    print(f"  median: {statistics.median(latencies) * 1e6:.2f} µs")
    print(f"  p95:    {latencies[int(n * 0.95)] * 1e6:.2f} µs")
    print(f"  max:    {max(latencies) * 1e6:.2f} µs")
    print(f"  received: {len(received)} events")


if __name__ == "__main__":
    bench()
