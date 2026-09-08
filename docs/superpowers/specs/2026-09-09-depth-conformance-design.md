# Depth stream conformance with openalgo-charts 2.1.0 — design

Date: 2026-09-09. Scope: conformance test + wire-shape alignment, no new features.

## Contract

Engine `MarketDepth` (openalgo-charts/src/feed/types.ts:28-42):
`{bids: [{price, qty, orders?}], asks: [...], ltp, ltq?}` — object entries,
numeric, ltp required. Optional `opts.depthLevel` request hint (5/20/30/50).

## Backend change

`_send_depth` in `trading/src/tradex_trading/interface/routes/stream.py`
emits the engine shape: `{type: "depth", instrument, bids: [{price, qty}],
asks: [{price, qty}], ltp}` with float values. LTP comes from the broker
`Depth` payload when present; fallback `bids[0].price` mirrors the engine's
own OpenAlgo adapter (openalgo-ws.ts:363). `levels` (redundant count) is
dropped; no legacy shim — the wire is free to change while no host exists.

## Testing

`trading/tests/interface/test_depth_conformance.py`:
- depth frames parse as engine MarketDepth (numeric, ltp present, bids
  descending / asks ascending, every level price>0 qty>0);
- `depth: "off"` silences depth frames while bars keep flowing;
- module docstring pins types.ts:28 as the contract source.

Existing stream tests stay green (they assert the old shape only where the
shape is theirs to change — any broken assertion is updated in the same
commit, since the wire is deliberately changed).
