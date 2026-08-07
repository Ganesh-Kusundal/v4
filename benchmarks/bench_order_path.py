#!/usr/bin/env python3
"""Benchmark: paper-session trade.submit latency (order spine end-to-end).

Stdlib-only. Prints median/p95/max over N runs.
"""

from __future__ import annotations

import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "domain" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brokers" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "trading" / "src"))

from tradex_domain import Equity, OrderRequest, OrderSide, OrderType, Price, Quantity
from tradex_brokers.paper.adapter import PaperBroker
from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.reactive.bus import ReactiveBus


def bench(n: int = 1000) -> None:
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())
    eq = Equity.of("NSE", "RELIANCE")
    req = OrderRequest(
        instrument=eq,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("100")),
    )

    latencies: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        engine.submit(req)
        latencies.append(time.perf_counter() - t0)

    latencies.sort()
    print(f"bench_order_path (n={n})")
    print(f"  median: {statistics.median(latencies) * 1e6:.1f} µs")
    print(f"  p95:    {latencies[int(n * 0.95)] * 1e6:.1f} µs")
    print(f"  max:    {max(latencies) * 1e6:.1f} µs")


if __name__ == "__main__":
    bench()
