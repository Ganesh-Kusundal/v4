#!/usr/bin/env python3
"""Benchmark: shared indicator computation vs per-strategy recomputation.

Phase 2 (:class:`AnalyticsRuntime` / :class:`IndicatorCache`) computes each
indicator once per ``(instrument, timeframe, indicator, params)`` and shares the
result with every strategy subscribed to that instrument. Before Phase 2 each
strategy computed its own indicators, so ``S`` strategies over one symbol cost
``S`` computations instead of one.

This script builds synthetic 1m bars for ``N`` symbols (NIFTY-500 by default)
and compares two ways the same ``S`` strategies could obtain the same indicator:

* **per-strategy** — every strategy calls ``compute_indicator`` directly.
* **shared** — every strategy reads
  ``AnalyticsRuntime.view(...).indicator(...)``; the first read computes, the
  rest are cache hits keyed on the bar-store version.

Stdlib only (plus the workspace packages): no broker, no datalake, no network.
The numbers are a floor for the shared-analytics win, not a live-trading claim.

Run::

    .venv/bin/python trading/benchmarks/bench_shared_analytics.py
    .venv/bin/python trading/benchmarks/bench_shared_analytics.py \
        --symbols 500 --bars 200 --strategies 20 --indicator rsi
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _pkg in ("domain", "analytics"):
    _src = _REPO_ROOT / _pkg / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from tradex_analytics.indicators import compute_indicator  # noqa: E402
from tradex_analytics.runtime import AnalyticsRuntime  # noqa: E402
from tradex_domain import (  # noqa: E402
    OHLC,
    Candle,
    Equity,
    Price,
    Quantity,
    Timeframe,
)

NIFTY_500 = 500
SYMBOL_PREFIX = "SYN"


def make_candles(symbol: str, count: int) -> tuple[Equity, list[Candle]]:
    """Deterministic synthetic 1m bars — no broker, no datalake."""
    instrument = Equity.of("NSE", symbol)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    price = Decimal("100")
    candles: list[Candle] = []
    for i in range(count):
        close = price + Decimal(i % 7) - 3
        candles.append(
            Candle(
                instrument=instrument,
                timeframe=Timeframe.M1,
                ohlc=OHLC(
                    open=Price(value=close - 1),
                    high=Price(value=close + 1),
                    low=Price(value=close - 2),
                    close=Price(value=close),
                ),
                volume=Quantity(value=Decimal(1000 + i)),
                timestamp=base + timedelta(minutes=i),
            )
        )
        price = close
    return instrument, candles


def _per_strategy_seconds(
    symbols: list[str],
    bars: dict[str, tuple[Equity, list[Candle]]],
    strategies: int,
    indicator: str,
    params: dict,
) -> float:
    start = time.perf_counter()
    for _ in range(strategies):
        for symbol in symbols:
            compute_indicator(indicator, bars[symbol][1], params)
    return time.perf_counter() - start


def _shared_seconds(
    symbols: list[str],
    bars: dict[str, tuple[Equity, list[Candle]]],
    strategies: int,
    indicator: str,
    params: dict,
) -> tuple[float, float]:
    runtime = AnalyticsRuntime()

    ingest_start = time.perf_counter()
    for symbol in symbols:
        for candle in bars[symbol][1]:
            runtime.ingest(candle)
    ingest = time.perf_counter() - ingest_start

    compute_start = time.perf_counter()
    for _ in range(strategies):
        for symbol in symbols:
            instrument, _ = bars[symbol]
            runtime.view(instrument.instrument_id, Timeframe.M1).indicator(
                indicator, **params
            )
    return ingest, time.perf_counter() - compute_start


def _best(fn, repeats: int):
    best = float("inf")
    result = None
    for _ in range(repeats):
        outcome = fn()
        if isinstance(outcome, tuple):
            elapsed = outcome[0] + outcome[1]
        else:
            elapsed = outcome
        if elapsed < best:
            best = elapsed
            result = outcome
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=NIFTY_500)
    parser.add_argument("--bars", type=int, default=200)
    parser.add_argument("--strategies", type=int, default=20)
    parser.add_argument("--indicator", default="rsi")
    parser.add_argument("--length", type=int, default=14)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    symbols = [f"{SYMBOL_PREFIX}{i:04d}" for i in range(args.symbols)]
    bars = {symbol: make_candles(symbol, args.bars) for symbol in symbols}
    params = {"length": args.length}

    compute_indicator(args.indicator, bars[symbols[0]][1], params)

    per_strategy = _best(
        lambda: _per_strategy_seconds(
            symbols, bars, args.strategies, args.indicator, params
        ),
        args.repeats,
    )
    shared = _best(
        lambda: _shared_seconds(
            symbols, bars, args.strategies, args.indicator, params
        ),
        args.repeats,
    )
    ingest, shared_compute = shared
    shared_total = ingest + shared_compute

    computations = args.symbols * args.strategies
    print("=== shared vs per-strategy analytics ===")
    print(
        f"symbols={args.symbols} bars={args.bars} strategies={args.strategies} "
        f"indicator={args.indicator} params={params}"
    )
    print(f"computations (per-strategy path): {computations}")
    print(f"per-strategy total:  {per_strategy:.3f} s")
    print(f"shared ingest:       {ingest:.3f} s (one-time, excluded from speedup)")
    print(
        f"shared compute:      {shared_compute:.3f} s "
        f"({args.symbols} computes + {computations - args.symbols} cache hits)"
    )
    print(f"shared total:        {shared_total:.3f} s")
    print(f"speedup (compute):   {per_strategy / shared_compute:.1f}x")
    print(f"speedup (incl. ingest): {per_strategy / shared_total:.1f}x")


if __name__ == "__main__":
    main()
