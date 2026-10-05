#!/usr/bin/env python3
"""Benchmark: per-symbol event routing vs broadcast-to-every-strategy.

Phase 1 (:class:`SubscriptionRouter`) routes each market event only to the
strategies subscribed to that instrument. The engine still invokes one wrapper
handler per strategy per event, but a strategy whose ``delivers`` check fails is
skipped before its ``on_bar``/``on_quote`` body runs. The win is therefore the
strategy evaluations *avoided*, traded against one cheap dict lookup each.

This script builds synthetic 1m candles for ``N`` symbols (NIFTY-500 by
default) and ``S`` strategies, then replays ``events`` through two models:

* **broadcast** — every strategy does its work for every event (pre-Phase 1).
* **routed** — every strategy pays a ``SubscriptionRouter.delivers`` check;
  only the subscribed ones do the work.

The work function is deterministic Decimal arithmetic over the candle's OHLC,
representative of the cheap per-bar work a strategy does before emitting a
signal. It is a model of the routing win, not a live-trading claim.

Stdlib only (plus the workspace packages): no broker, no datalake, no network.

Run::

    .venv/bin/python trading/benchmarks/bench_routing.py
    .venv/bin/python trading/benchmarks/bench_routing.py \
        --symbols 500 --strategies 100 --rounds 20
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _pkg in ("domain", "strategy"):
    _src = _REPO_ROOT / _pkg / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from tradex_domain import (  # noqa: E402
    OHLC,
    Candle,
    Equity,
    Price,
    Quantity,
    Timeframe,
)
from tradex_strategy.core.routing import (  # noqa: E402
    StrategySubscription,
    SubscriptionRouter,
    event_instrument_id,
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


def _strategy_work(candle: Candle) -> float:
    """Cheap deterministic per-bar work (Decimal, like real strategies)."""
    ohlc = candle.ohlc
    open_ = ohlc.open.value
    high = ohlc.high.value
    low = ohlc.low.value
    close = ohlc.close.value
    return float((high - low) / (close or 1) + (close - open_) * 2)


def _build_router(
    symbols: list[Equity],
    strategy_count: int,
    wildcard_fraction: float,
) -> tuple[SubscriptionRouter, list[str]]:
    """Disjoint symbol slices per strategy, plus a wildcard tail."""
    router = SubscriptionRouter()
    wildcard_count = int(strategy_count * wildcard_fraction)
    selective_count = strategy_count - wildcard_count
    strategy_ids = [f"strat-{i:04d}" for i in range(strategy_count)]

    window = max(1, len(symbols) // max(1, selective_count))
    for i, strategy_id in enumerate(strategy_ids[:selective_count]):
        start = i * window
        for instrument in symbols[start : start + window]:
            router.subscribe(
                StrategySubscription(Candle, instrument.instrument_id), strategy_id
            )
    for strategy_id in strategy_ids[selective_count:]:
        router.subscribe(StrategySubscription(Candle), strategy_id)
    return router, strategy_ids


def _events(
    symbols: list[tuple[Equity, list[Candle]]],
    rounds: int,
) -> list[Candle]:
    """Round-robin one candle per symbol per round."""
    events: list[Candle] = []
    for _ in range(rounds):
        for _, candles in symbols:
            events.append(candles[0])
    return events


def _broadcast_deliveries(events: list[Candle], strategy_ids: list[str]) -> int:
    """Delivery count: every strategy's work runs for every event."""
    delivered = 0
    for event in events:
        for _ in strategy_ids:
            _strategy_work(event)
            delivered += 1
    return delivered


def _routed_deliveries(
    events: list[Candle],
    strategy_ids: list[str],
    router: SubscriptionRouter,
) -> int:
    """Delivery count: only strategies the router delivers to do the work."""
    delivered = 0
    for event in events:
        instrument_id = event_instrument_id(event)
        for strategy_id in strategy_ids:
            if instrument_id is None or router.delivers(
                strategy_id, Candle, instrument_id
            ):
                _strategy_work(event)
                delivered += 1
    return delivered


def _lookup_seconds(
    events: list[Candle],
    strategy_ids: list[str],
    router: SubscriptionRouter,
) -> None:
    for event in events:
        instrument_id = event_instrument_id(event)
        for strategy_id in strategy_ids:
            router.delivers(strategy_id, Candle, instrument_id)


def _best_seconds(fn, repeats: int) -> float:
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=NIFTY_500)
    parser.add_argument("--strategies", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--wildcard-fraction", type=float, default=0.1)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    symbol_data = [
        make_candles(f"{SYMBOL_PREFIX}{i:04d}", 2) for i in range(args.symbols)
    ]
    instruments = [instrument for instrument, _ in symbol_data]
    router, strategy_ids = _build_router(
        instruments, args.strategies, args.wildcard_fraction
    )
    events = _events(symbol_data, args.rounds)
    checks = len(events) * args.strategies
    if checks == 0:
        parser.error(
            "degenerate benchmark: --symbols, --strategies and --rounds must "
            f"all be positive (got symbols={args.symbols}, "
            f"strategies={args.strategies}, rounds={args.rounds})"
        )

    lookup = _best_seconds(
        lambda: _lookup_seconds(events, strategy_ids, router), args.repeats
    )
    routed = _best_seconds(
        lambda: _routed_deliveries(events, strategy_ids, router), args.repeats
    )
    broadcast = _best_seconds(
        lambda: _broadcast_deliveries(events, strategy_ids), args.repeats
    )
    delivered = _routed_deliveries(events, strategy_ids, router)

    wildcard_count = int(args.strategies * args.wildcard_fraction)
    print("=== per-symbol routing vs broadcast ===")
    print(
        f"symbols={args.symbols} strategies={args.strategies} "
        f"events={len(events)} wildcard={wildcard_count}"
    )
    print(f"strategy checks:          {checks}")
    print(
        f"delivered:                {delivered} "
        f"({100.0 * delivered / checks:.1f}% of checks)"
    )
    print(
        f"router lookup (isolated): {lookup:.3f} s "
        f"({1e9 * lookup / checks:.0f} ns/check)"
    )
    print(f"routed (lookup + work):   {routed:.3f} s")
    print(f"broadcast (work for all): {broadcast:.3f} s")
    print(f"speedup:                  {broadcast / routed:.1f}x")


if __name__ == "__main__":
    main()
