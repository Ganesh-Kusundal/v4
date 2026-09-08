# WS indicator push (Tier-2 subscribe) — design

Date: 2026-09-09. Hybrid model: bar-close default, tick whitelist.

## Contract (new messages on /ws/stream)

Subscribe: `{type: "indicator-sub", indicators: [{instrument, interval,
ids: [id...], mode: "bar-close" | "tick"}]}`. Ack
`{type: "indicator-subscribed", indicators: [...]}`; unknown id or bad mode
is a loud `{type: "error"}` ack (subscription request rejected whole).
Unsubscribe: `{type: "indicator-unsub", indicators: [{instrument,
interval, ids?}]}` (ids omitted = all for that pair). Push frames:
`{type: "indicator", instrument, interval, id, points: [{time, ...plots}]}`
— last 2 points, engine Tier-2 point shape (time UTC-seconds of IST wall
clock, null padding, never NaN).

## Semantics

- `bar-close` (default): recompute + push when the connection's bar frame
  for that (instrument, interval) arrives with `closed: true`.
- `tick`: recompute on forming-bar frames, throttled to >= 1 s between
  pushes per (instrument, id). For provisional values only.
- Compute window: datalake tail (last ~400 bars of that instrument/interval
  ending at the previous closed bar) + the current bar appended. Datalake
  miss (new symbol, no data) degrades to computing on the live bar alone.
- Recompute is `compute_indicator` (the same seam the REST compute endpoint
  uses), so REST and WS can never drift.

## Structure

New module `trading/src/tradex_trading/interface/routes/stream_indicators.py`:
subscription registry (per connection), datalake-tail loader with bar-close
invalidation, and a `handle_bar_frame(frame)` entry the existing bar-frame
path calls. `stream.py` wires subscribe/unsubscribe message types and the
bar-frame hook (~30 lines). Frames ride the existing ticks queue
(drop-oldest), same as bars/depth.

## Testing

`trading/tests/interface/test_ws_indicator_push.py`:
- bar-close push fires only on closed bars;
- tick mode throttles (two forming updates inside 1 s -> one push);
- unknown id -> error ack, nothing registered;
- indicator-unsub (with and without ids) silences;
- pushed points are finite-or-null and match compute_indicator output.
Session-less TestClient + ReactiveBus pattern (same as depth conformance).
