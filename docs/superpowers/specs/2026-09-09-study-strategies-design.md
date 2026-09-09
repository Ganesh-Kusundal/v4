# Chart-study signal strategies — design

Date: 2026-09-09. Five classic strategies reusing the verified 102-indicator
backend; signals-only (enter/exit), long/short emitted, long-only gate stays
the caller's job (same contract as `SmaCrossStrategy`).

## Strategies (each a `Strategy`-protocol class, on_bar state machine)

- `macd_cross` — BUY on MACD line crossing above signal, SELL on reverse.
  Params: fast 12 / slow 26 / signal 9.
- `rsi_reversal` — BUY when RSI crosses up through oversold (30), SELL when
  RSI crosses down through overbought (70). Params: length 14, levels 70/30.
- `bollinger_breakout` — BUY on close breaking above the upper band, SELL
  on close breaking below the lower band. Params: length 20, stdDev 2.
- `supertrend_flip` — BUY on direction flip to +1, SELL on flip to -1.
  Params: period 10, multiplier 3.
- `ema_ribbon_pullback` — BUY on pullback touch of the fastest EMA while
  the ribbon (4 EMAs) stays bullish (each above the next); SELL mirrored
  for the bearish ribbon. Params: lengths 9/21/50/100.

## Implementation shape

Each strategy keeps its own rolling close series and calls the analytics
compute functions (`macd`, `rsi`, `bollinger`, `supertrend`, `_sma_seeded_ema`)
on the tail window — one source of truth with the REST/WS indicator path.
State machines track previous vs current values; signals carry
`reason` strings naming the trigger. Modules live beside `sma_cross.py`;
`strategies/__init__.py` gains the five instances. Params validated in
`__init__` (loud, like `fast >= slow`).

## Testing

`trading/tests/strategy/test_study_strategies.py`:
- each strategy emits BUY/SELL at the expected synthetic-ramp/series
  inflections (constructed series per strategy, not random);
- param validation errors loud;
- protocol conformance via the extensions auto-discovery run
  (`extensions/__init__.py` already validates `__all__` against the
  protocol — suite green proves registration);
- signal bookkeeping (`signals` list grows; strength 1.0).
