# Scanner: lake-first with live fallback, equity + NIFTY/BANKNIFTY derivatives

Small change. Status: awaiting approval.

## Intent
The scanner reads only the equity datalake today, so it cannot scan option
contracts or futures, and an equity symbol missing from the lake silently
scores nothing. Make one scanner path work for all three kinds: read the
lake if the bars are there, otherwise fetch live from the broker.

## Decisions (yours)
- **Read-only lake.** The scanner never writes to the datalake. No cache, no
  insert, for any kind.
- **Equity:** datalake first; if the symbol or window is missing, go live.
- **Options and futures:** always live (no lake data exists for them).
- **Live contracts only** (today's tradable contracts, no expired ones).
- **Universe for options: NIFTY and BANKNIFTY on NSE.**

## Goals / non-goals
Goals: scan equity, NIFTY/BANKNIFTY options, and futures through the same
`ScannerEngine`; equity keeps its current speed when the lake has the data.
Non-goals: persisting live bars; expired contracts; Greeks/IV/OI conditions;
other underlyings; changing the lake layout or sync/gap tools.
Anti-goal: a broker outage must not fail an equity scan that the lake can
serve.

## Approaches (picked: read-only, no cache)
- A: extend the equity lake layout to contracts. Rejected: breaks assumptions
  in sync, gap detection and audits; you asked for no inserts.
- B: separate derivatives cache root. Rejected: you asked for no caching.
- **C (chosen): read-only provider.** A `LakeFirstMarketProvider` wraps the
  existing `BulkPrefetchMarketProvider` and a broker. `history()` returns lake
  bars when present, else `broker.history(...)`. `ScannerEngine` is unchanged
  (it already only calls `market.history`).

## Design
1. `market_data`: `LakeFirstMarketProvider(lake, broker)`; equity lookups hit
   the lake, empty result or non-equity instrument goes to the broker. Broker
   errors degrade that symbol to an empty series (the engine already does this).
2. Universe builder (`strategy/core/universe.py`):
   `option_universe(broker, underlying, expiries=2, strikes=10, rights=("CE","PE"))`
   uses `broker.get_option_chain` and `Expiry` pairs around the reference
   price; `future_universe(...)` for the nearest expiries. Underlying is
   restricted to `{"NIFTY","BANKNIFTY"}` on NSE; anything else is a 422.
3. `POST /api/charts/scanner/run` accepts, besides a registered `id`, a
   definition: `{kind: equity|option|future, underlying?, expiries?,
   strikes?, conditions, timeframe, limit}`. Result rows add `expiry`,
   `strike`, `right` when the instrument has them. The datalake prefetch is
   only done for equity symbols.
4. Rate limiting: live reads reuse the engine's existing paced limiter;
   a 2-expiry x 21-strike x CE/PE scan is about 84 live history calls.

## My calls (change any)
- Futures included for the same two underlyings (you named options only).
- Default broker for live bars: Upstox (full 09:15-15:29 session; Dhan stops
  at 15:14). Overridable per request.
- Defaults: nearest 2 expiries, 10 strikes each side of spot.
- No automated live-broker test; tests use a fake broker.

## Left to the builder
Exact request-model names, error text, and the fake-broker fixtures.

## Test plan
Provider: lake hit does not call the broker; lake miss does; broker error
degrades to empty; nothing is ever written. Universe: chain expansion count,
strike band, rejected underlyings. Endpoint: option scan with a fake broker,
equity scan unchanged (existing `TestScannerEndpoint`).
