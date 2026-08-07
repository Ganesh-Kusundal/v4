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

### Brownian bridge (doc §2.2) — shipped as an option

`method="bridge"` (constructor param, default `"anchored"`) generates a
zero-drift Gaussian walk pinned at both ends:
`X(t) = open + (close - open)·t/T + W(t) − (t/T)·W(T)`, so X(0) == open and
X(T) == close exactly. Per-step volatility is calibrated from the bar's
high-low range — `step_vol = 2·(high − low) / (3·√T)` — so the bridge's
mid-bar standard deviation is a third of the range and typical excursions
track the real bar. Final clamp to [low, high] keeps the range contract on
outlier paths; flat bars (range 0) degenerate to all-flat ticks.

The anchored walk stays the default: it is simpler, its noise needs no
interpretation, and both methods satisfy the invariants that matter
(open→close, inside [low, high], U-shaped volume). Choosing between them is
a per-generator decision; a calibration study against real ticks would be
needed to prefer one over the other.

### Wiring, config, calibration (doc §6) — status

- `ReplayEngine` wiring: **done**. `ReplayEngine(events, synthetic_ticks=True,
  seed=N)` expands each M1 candle into synthetic quotes on the bus (the
  reactive replay path, per doc §3); registered strategies still receive the
  candle directly via `on_bar`. A non-M1 candle in synthetic mode is recorded
  as a bus-publish error rather than silently skipped.
- `tick_simulation: true` app config: still deferred — `AppConfig` gains the
  flag the day a session-level consumer asks for it.
- `BacktestEngine` wiring: still deferred — `BacktestEngine.run()` feeds
  events to the strategy *directly*, not via the bus, so the generator (a bus
  event source) has no seam there.
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
