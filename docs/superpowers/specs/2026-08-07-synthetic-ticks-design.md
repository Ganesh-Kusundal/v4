# Design Spec: Synthetic 1-Second Ticks from M1 Candles

## Status
- **Date**: 2026-08-07
- **Owner**: replay/backtesting
- **Approver**: User
- **Scope**: ponytail-trimmed — generator + tests only

## Problem

Only bar data (1-minute candles) is available for backtesting, but strategies
may subscribe to `Quote` events (the live path publishes quotes from the
broker feed). There is no way to simulate tick-level quotes when only M1
candles exist.

## Solution: `SyntheticTickGenerator` (replay/synthetic_ticks.py)

A new event source — "just another market feed" — that reads an M1 `Candle`,
generates 60 synthetic 1-second `Quote` events, and publishes them onto the
`ReactiveBus`. Strategies subscribed to `Quote` work unchanged; the `FakeClock`
advances 1 second per tick so time-dependent logic stays deterministic.

### Algorithm: simple OHLC-anchored random walk (doc §2.3)

- Tick 0 = bar open.
- Tick N-1 = bar close (deterministic anchor).
- For the middle ticks: `next = prev + (close - prev) / remaining + noise`,
  clamped to `[low, high]`. The drift term pulls toward the close; the noise
  term (uniform, scaled by `(high - low) / 6`, shrinking as remaining ticks
  shrink) produces intra-bar movement that stays inside the real range.
- Volume: bar volume split with a U-shape (first/last ticks weighted 2x),
  summing exactly to the bar volume (last tick absorbs the Decimal rounding
  remainder).
- Spread: fixed proportion of price (`0.05%`, floor 0.01) for bid/ask — the
  doc's "simple function of price" option.
- Reproducible: optional `seed` seeds the RNG.

### Why not Brownian bridge (doc §2.2)?

The bridge adds variance-calibration machinery for a marginal realism gain on
a simulator with no real ticks to validate against. The anchored walk already
satisfies the invariants that matter: open→close, inside [low, high],
U-shaped volume. Upgrade path: swap `_walk()` internals — the seam is one
method.

### Why no config option, BacktestEngine wiring, or calibration tool (doc §6)?

- `tick_simulation: true` config: no consumer yet — the generator is
  constructed directly. Add the flag the day something feeds it bars.
- `BacktestEngine` wiring: `BacktestEngine.run()` feeds events to the strategy
  *directly*, not via the bus — the generator is a bus event source, so the
  natural host is the reactive replay path (`ReplayEngine`), not
  `BacktestEngine`. No consumer asks for it yet; wiring is a few lines later.
- Calibration tool: requires real tick data the repo does not have. YAGNI —
  "add when real ticks exist".

## Files

- `trading/src/tradex_trading/replay/synthetic_ticks.py` — generator (+ export
  from `replay/__init__.py`)
- `trading/tests/replay/test_synthetic_ticks.py` — behavior tests

## Test plan

- M1-only guard raises on non-M1 candles.
- 60 quotes per bar; timestamps = `candle.timestamp + i` seconds.
- First tick == open; last tick == close.
- Every ltp within `[low, high]` (clamp).
- Sum of tick volumes == bar volume (exact).
- Clock advanced exactly 60 seconds.
- Flat bar (open == high == low == close) emits all-flat ticks.
