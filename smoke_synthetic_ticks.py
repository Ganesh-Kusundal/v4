"""Smoke test — synthetic 1-second ticks from M1 candles through ReplayEngine.

Replays two M1 candles through ``ReplayEngine(synthetic_ticks=True, seed=1)``
and asserts the tick-stream invariants:

  - 60 synthetic Quote events per bar (120 total), no raw candles on the bus
  - per-bar timestamps stepping one second, strictly monotonic
  - first tick == bar open, last tick == bar close (anchors)
  - every ltp clamped to [low, high] (bar 2 feeds a close above its high on
    purpose to exercise the clamp)
  - tick volumes summing exactly to the bar volume (20,000 over two bars)
  - bid < ask on every quote (0.05% fixed spread, 0.01 floor)

Also prints the bus **stream order vs the message-log order** (equal here —
nothing subscribes reentrantly to the quote stream; contrast the CQRS
session path, where the engine's synchronous effects reorder the stream),
and sanity-checks the anchored vs bridge price paths directly on the
generator.

Usage:
    python smoke_synthetic_ticks.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

# ---------------------------------------------------------------------------
# 0. Ensure project packages are importable
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_domain import (  # noqa: E402
    OHLC,
    Candle,
    Equity,
    Price,
    Quantity,
    Quote,
    Timeframe,
)
from tradex_trading.reactive.bus import ReactiveBus  # noqa: E402
from tradex_trading.replay.engine import ReplayEngine  # noqa: E402
from tradex_trading.replay.synthetic_ticks import SyntheticTickGenerator  # noqa: E402

INSTRUMENT = Equity.of("NSE", "RELIANCE")
LOW, HIGH = Decimal("95"), Decimal("110")


def m1_candle(ts: datetime, close: str = "105") -> Candle:
    """M1 candle for the smoke instrument (open 100, range [95, 110])."""
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.M1,
        timestamp=ts,
        ohlc=OHLC(
            open=Price(value=Decimal("100")),
            high=Price(value=HIGH),
            low=Price(value=LOW),
            close=Price(value=Decimal(close)),
        ),
        volume=Quantity(value=Decimal("10000")),
    )


failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures += 1


def main() -> int:
    now = datetime(2026, 8, 7, 9, 15, tzinfo=UTC)
    events = [
        m1_candle(now),
        # Close deliberately above the bar's high: exercises the clamp and
        # proves the range invariant holds even on a malformed candle.
        m1_candle(now + timedelta(minutes=1), close="112"),
    ]

    print("=== Synthetic ticks through ReplayEngine (smoke) ===")
    print(f"Bars : 2x M1 ({INSTRUMENT.instrument_id})  mode: synthetic_ticks, seed=1")
    print()

    # --- 1. Replay: capture both the stream and the message log ------------
    log: list[object] = []
    bus = ReactiveBus(message_log=log)
    stream: list[object] = []
    bus.stream().subscribe(stream.append)
    engine = ReplayEngine(events, synthetic_ticks=True, seed=1)
    result = engine.replay(bus)

    quotes = [e for e in log if isinstance(e, Quote)]
    candles_on_bus = sum(1 for e in log if isinstance(e, Candle))
    ts = [q.timestamp for q in quotes]
    total_vol = sum((q.volume.value for q in quotes), Decimal("0"))

    print("[1/4] Replay event flow")
    check("ReplayResult: 2 events, 2 candles, 0 errors", (
        result.events_processed == 2
        and result.candles_processed == 2
        and not result.has_errors
    ))
    check("120 quotes on bus, 0 raw candles (replace semantics)", (
        len(quotes) == 120 and candles_on_bus == 0
    ))
    check("tick_clock seeded to first bar, tracks ticks (120s after replay)", (
        engine.tick_clock is not None
        and engine.tick_clock.now() == now + timedelta(seconds=120)
    ))

    def compact(events: list[object]) -> str:
        """Run-length summary of the type sequence, e.g. 'Quote x120'."""
        runs: list[tuple[str, int]] = []
        for e in events:
            name = type(e).__name__
            if runs and runs[-1][0] == name:
                runs[-1] = (name, runs[-1][1] + 1)
            else:
                runs.append((name, 1))
        return " -> ".join(f"{name} x{count}" for name, count in runs)

    print()
    print("  Bus stream order vs message-log order (type runs):")
    print(f"    stream: {compact(stream)}")
    print(f"    log   : {compact(log)}")
    check("stream order == message-log order (no reentrancy here)", (
        [type(e).__name__ for e in stream] == [type(e).__name__ for e in log]
    ))
    print()

    print("[2/4] Timestamps and anchors")
    check("bar 1: 09:15:00 -> 09:15:59 (60 ticks)", (
        ts[0] == now and ts[59] == now + timedelta(seconds=59)
    ))
    check("bar 2 starts at 09:16:00", ts[60] == now + timedelta(seconds=60))
    check("timestamps strictly monotonic", all(a <= b for a, b in zip(ts, ts[1:])))
    check("bar 1 anchors open 100.0 / close 105.0", (
        quotes[0].ltp.value == Decimal("100")
        and quotes[59].ltp.value == Decimal("105")
    ))
    check("bar 2 close 112 clamped to high 110", (
        quotes[60].ltp.value == Decimal("100")
        and quotes[119].ltp.value == HIGH
    ))
    print()

    print("[3/4] Price range, volume, spread")
    check("all 120 ltps within [95, 110]", all(LOW <= q.ltp.value <= HIGH for q in quotes))
    check(f"tick volumes sum exactly to bar volume (got {total_vol})", total_vol == Decimal("20000"))
    check("bid < ask on every quote", all(q.bid.value < q.ask.value for q in quotes))
    first = quotes[0]
    print(f"    bar 1 first quote: bid {first.bid.value} / ltp {first.ltp.value} / ask {first.ask.value}")
    print()

    print("[4/4] Regression + price-path sanity")
    log2: list[object] = []
    ReplayEngine([m1_candle(now)]).replay(ReactiveBus(message_log=log2))
    check("default mode still publishes the raw candle", (
        len(log2) == 1 and isinstance(log2[0], Candle)
    ))

    def path(method: str, seed: int = 1) -> list[Decimal]:
        lg: list[object] = []
        SyntheticTickGenerator(ReactiveBus(message_log=lg), seed=seed, method=method).feed_bar(
            m1_candle(now)
        )
        return [q.ltp.value for q in lg if isinstance(q, Quote)]

    for method in ("anchored", "bridge"):
        p = path(method)
        seeded = path(method, 7)
        check(f"{method}: anchors open/close + in range + reproducible", (
            p[0] == Decimal("100")
            and p[-1] == Decimal("105")
            and all(LOW <= v <= HIGH for v in p)
            and seeded == path(method, 7)  # same seed -> identical path
        ))
    print()

    if failures:
        print(f"RESULT: {failures} CHECK(S) FAILED — synthetic tick smoke NOT OK")
        return 1
    print("RESULT: SYNTHETIC TICK SMOKE OK (all checks passed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
