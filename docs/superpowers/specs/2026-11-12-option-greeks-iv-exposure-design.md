# Option Greeks & IV Exposure on the Option-Chain Route — Design

Date: 2026-11-12
Status: Approved (brainstorm)

## Goal

Expose option greeks and implied volatility (IV) on the option-chain response of
both serving apps (gateway `tradex-gateway` and interfaces `tradex_interfaces`),
using data the brokers already return on their option-chain REST calls. No new
upstream calls, no computation, no WebSocket exposure.

## Problem

Both brokers return greeks + IV on their option-chain endpoints, and both
parsers discard them at parse time:

- Dhan `POST /optionchain`: `oc[strike].ce/pe` carry `greeks{delta, theta,
  gamma, vega}`, `implied_volatility`, plus `last_price, oi, volume,
  previous_oi, previous_volume, top_bid_price/quantity, top_ask_price/quantity,
  security_id, average_price`. The parser (`brokers/.../dhan/_marketdata.py`)
  reads only `security_id`.
- Upstox `GET /option/chain`: `call_options`/`put_options` carry
  `option_greeks{vega, theta, gamma, delta, iv, pop?}` and `market_data{ltp,
  volume, oi, close_price, bid_price, bid_qty, ask_price, ask_qty, prev_oi}`,
  plus per-row `pcr` and `underlying_spot_price`. The parser
  (`brokers/.../upstox/_marketdata.py`) reads only `instrument_key`.
- Interfaces `/option-chain/{underlying}?live=true` attaches greeks to
  `call_live`/`put_live` legs, but `OptionChainResponse` (contracts.py,
  `extra="ignore"`, fields `underlying, expiry, strikes, enriched`) strips
  `live` and `expiries` from the payload — 3 tests are red
  (`test_option_chain_live_nfo_attaches_greeks_and_oi`,
  `test_option_chain_live_mcx_keeps_ltp_window_no_batch`,
  `test_option_chain_endpoint_resolves_and_serializes`).
- Gateway `/v1/option-chain/{underlying}?live=true` enriches legs with only
  `ltp`/`oi`/`volume`; `quote_payload` drops `Quote.metadata` (which is where
  WS-derived greeks would ride). Out of scope for this work.

## Decisions (from brainstorm)

- Scope: gateway + interfaces end-to-end.
- Data source: parse the option-chain responses we already call. No
  `option-greek` dedicated endpoint, no WS exposure, no quote-path exposure.
- Domain: add leg metadata on `OptionPair` (not a new typed leg, not a side
  channel).
- Leg payload: normalized full OC leg dict, best-effort.
- MCX unchanged (LTP-only fallback stays); paper broker leaves legs `None`.

## Design

### 1. Domain (`domain/src/tradex_domain/options.py`)

`OptionPair` gains two optional fields (frozen dataclass, existing
constructors unaffected):

```python
call_leg: dict[str, object] | None = None
put_leg:  dict[str, object] | None = None
```

`Serializable` round-trip: legs are plain JSON-able dicts, emitted only when
set (existing serialization behavior for all current chains is unchanged).

### 2. Normalized leg dict (both brokers)

| normalized key   | Dhan source                             | Upstox source                          |
|------------------|-----------------------------------------|----------------------------------------|
| `ltp`            | `last_price`                            | `market_data.ltp`                      |
| `oi`             | `oi`                                    | `market_data.oi`                       |
| `volume`         | `volume`                                | `market_data.volume`                   |
| `previous_oi`    | `previous_oi`                           | `market_data.prev_oi`                  |
| `previous_volume`| `previous_volume`                       | — (omitted)                            |
| `bid` / `ask`    | `top_bid_price` / `top_ask_price`       | `market_data.bid_price` / `ask_price`  |
| `bid_qty` / `ask_qty` | `top_bid_quantity` / `top_ask_quantity` | `market_data.bid_qty` / `ask_qty`  |
| `iv`             | `implied_volatility`                    | `option_greeks.iv`                     |
| `greeks`         | `greeks{delta, theta, gamma, vega}`     | `option_greeks{delta, theta, gamma, vega}` |
| `rho`            | — (omitted)                             | — (omitted; endpoint has none)         |
| `pop`            | —                                       | `option_greeks.pop`                    |
| `provider`       | `"dhan"`                                | `"upstox"`                             |

Rules:

- Best-effort: any unparseable field is skipped; a leg with no parseable
  market data becomes `None`. Never raises into the chain call.
- `None`/absent values are omitted from the dict (no `"iv": null` keys).
- Numbers stay as native floats from the broker payload (no Decimal
  conversion — greeks/IV are not money, per decimal-money ADR scope).
- MCX: unchanged — Dhan OC legs will carry data when the broker is Dhan;
  Upstox OC is unavailable on MCX, so legs are `None`.

### 3. Broker parsers

- Dhan `_marketdata.py::get_option_chain` — while iterating `raw_strikes`,
  build the normalized leg dict for `call_leg`/`put_leg` from the `ce`/`pe`
  payloads and pass them into `OptionPair(...)`.
- Upstox `_marketdata.py::get_option_chain` — same from
  `row["call_options"]` / `row["put_options"]`.
- Paper adapter (`brokers/src/tradex_brokers/paper/adapter.py`) — unchanged;
  legs stay `None`.

### 4. Serializers

Gateway `serialize_chain` (`gateway/src/tradex_gateway/market.py`): inside
each strike's `ce`/`pe` entry, when `pair.call_leg`/`pair.put_leg` is set,
emit `"leg": <normalized dict>` alongside `instrument_id`/`lot_size`. When
`None`, key omitted — existing output byte-compatible.

Interfaces `serialize_option_chain` (`interfaces/.../routes/_market_helpers.py`)
— same: each `pairs[]` entry gains `"call_leg"`/`"put_leg"` keys when set.

### 5. Interfaces route + contract

- `OptionChainResponse` (`interfaces/src/tradex_interfaces/contracts.py`) —
  replace the narrow fields with the actual live/static envelope:
  `underlying: str | None`, `live: bool = False`, `expiry: str | None`,
  `expiries: list[dict[str, Any]] | None`, `strikes: list[dict[str, Any]] | None`,
  `enriched: bool = False`. Keep `extra="ignore"`.
- `enrich_chain_live` — prefer OC-attached legs: if `p.call_leg` / `p.put_leg`
  is set by the broker parse, copy it into the pair output (merged: leg dict
  provides greeks/iv/oi/volume; live batch quotes may still override `ltp`/
  `oi`/`volume` with fresher values — OC values are the baseline). When
  absent, keep current behavior (batch quotes, MCX LTP window).

### 6. Gateway live enrichment

`market.py::_enrich` — unchanged except: when the static OC leg was already
attached (`serialize_chain` emitted `leg`), the live merge (ltp/oi/volume
from batch quotes) overlays those keys only. No greeks/IV from quotes
(batch `/market-quote/quotes` is not a reliable greeks source).

### 7. Tests

- New broker-parser unit tests (fixture payloads, no network):
  Dhan OC with greeks/iv → `call_leg`/`put_leg` populated; Upstox OC with
  `option_greeks`/`market_data` → same, including `pop` passthrough.
- Gateway `serialize_chain` test: strike entry carries `"leg"` when set;
  byte-compatible shape when not.
- Interfaces: 3 red tests (`test_fastapi_app.py::option_chain*`) go green
  without weakening their assertions.
- `test_fastapi_app` NFO greeks test asserts `greeks` present in
  `call_live`/`put_live` — now satisfied by OC-attached legs even if the
  batch quote has no metadata.
- Full existing suite stays green.

### 8. Non-goals / out of scope

- WS `/v1/stream` greeks/IV; `quote_payload` metadata passthrough;
  Upstox `GET /v3/market-quote/option-greek` dedicated endpoint;
  IV solving (Black-Scholes) — nothing is computed anywhere;
  `quote_batch` greeks dependency remains best-effort.

## Error handling & limits

- OC rate limits unchanged (Dhan 1 req/3s; Upstox per-key) — we parse what we
  already fetch. Gateway keeps its 10s TTL cache for `/v1/option-chain`.
- Malformed legs degrade to `None`; the chain route never fails because of
  leg parsing.
- Contract change (`OptionChainResponse`) is additive — gateway consumers
  unaffected.

## Risks

- Live repo has uncommitted changes across gateway/interfaces files this
  design touches — implementation must rebase/merge carefully and re-run the
  existing suite.
- `OptionPair` is referenced in tests that construct it positionally
  (`OptionPair(call=…, put=…, strike=…)`) — new kwargs with defaults are safe.
- Upstox OC requires explicit expiry — unchanged behavior.
