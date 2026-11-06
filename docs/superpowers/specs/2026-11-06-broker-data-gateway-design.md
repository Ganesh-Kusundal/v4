# Broker Data Gateway (`tradex_gateway`) — Design

**Date:** 2026-11-06
**Status:** Approved for implementation
**Consumer:** `/Users/apple/Documents/amt-institutional-order-flow-scalper` (index, spot, options; historical + live)

## 1. Problem

The platform already has a broker-agnostic `BaseBroker` interface (Dhan, Upstox,
Paper) with per-broker rate limiting, retries, circuit breakers and WebSocket
streaming, plus a single-broker FastAPI app in `tradex_interfaces`. Nothing
holds both live brokers at once, there is no cross-broker failover, no REST
market-depth surface, no consumer-side rate limiting, and the datalake only
holds NSE spot equity 1-minute bars.

The scalper app needs one stable HTTP/WS contract for:

- historical OHLCV for indices, spot equities, futures and options;
- a live feed of quotes/ticks and (optionally) market depth;
- market-depth snapshots;
- option chains and instrument lookup.

## 2. Decisions (locked)

| Decision | Choice |
|---|---|
| API contract | Fresh contract defined here |
| Deployment | New workspace member `gateway/` (`tradex_gateway`), data-only service |
| Live failover | Cold switch: one active upstream feed, switch on failure, emit status + gap |
| History source | Datalake-first for NSE spot equity 1m; broker-direct otherwise; TTL cache |
| Process model | One process owns both brokers; `workers > 1` refused |
| Order operations | Never; brokers built with `allow_order_operations=False` |

## 3. Architecture

```
scalper app ──REST/WS──▶ tradex_gateway
                          ├─ InstrumentDirectory   canonical ID ↔ per-broker registry
                          ├─ MarketDataRouter      capability + health aware selection
                          ├─ HistoryService        datalake-first, cache, batch, single-flight
                          ├─ Quote/Depth/Chain services
                          ├─ LiveHub               refcounted subscriptions, fan-out, failover
                          └─ BrokerRuntime × 2     Dhan + Upstox: broker, master, health
```

- `BrokerRuntime` owns one adapter built through the standard path
  (`tradex_runtime.live.build_broker_from_env(provider, allow_order_operations=False)`),
  its instrument master/registry, connection state, capability table and a
  per-capability `BrokerHealth`.
- Broker-native limiters, retries and circuit breakers stay in the adapters and
  are the enforcement layer. The gateway does not re-implement them; it adds
  admission control, failover and caching above them.
- All synchronous broker calls run in worker threads; FastAPI handlers remain
  async and never block the event loop.

## 4. Package layout and import boundaries

```
gateway/
  pyproject.toml
  src/tradex_gateway/
    __init__.py, __main__.py, cli.py
    config.py        GatewayConfig (env)
    auth.py          API-key dependency (REST header, WS query)
    errors.py        GatewayError hierarchy + envelope handlers
    brokers.py       BrokerRuntime, BrokerHealth
    directory.py     InstrumentDirectory (canonical IDs, resolution, search, nearest expiry)
    router.py        MarketDataRouter (selection + failover + error mapping)
    history.py       HistoryService (datalake-first, broker, cache, batch)
    market.py        Quote/Depth/Chain/Futures services
    live.py          LiveHub, BrokerFeed, WS frame models
    models.py        Pydantic request/response models
    app.py           create_app(), lifespan, REST + WS routes
  tests/
```

Root changes:

- add `gateway` to the uv workspace members and `[tool.uv.sources]`;
- add `gateway/src` to root `pythonpath`, `gateway/tests` to `testpaths`;
- add `tradex_gateway` to import-linter `root_packages` and a `gateway layer`
  contract forbidding `tradex_interfaces`, `tradex_execution`,
  `tradex_strategy`, `tradex_replay`, `tradex_application`, `duck_analytics`;
- update `tests/test_import_boundaries.py` `_SRC` and `_ALLOWED`.

Allowed imports: `tradex_domain`, `tradex_brokers`, `tradex_config`,
`tradex_market_data`, `tradex_runtime`, `tradex_observability`.
`tradex_interfaces` is deliberately not a dependency; the gateway implements
its own small API-key auth instead of importing the app layer.

## 5. External contract

Base path `/v1`. Auth: `X-API-Key` header (REST) or `?api_key=` (WS).

### 5.1 Instrument IDs

Canonical `tradex_domain.value_objects.InstrumentId` strings:

- spot equity: `NSE:RELIANCE`, `BSE:XYZ`
- index: `IDX:NIFTY`, `IDX:BANKNIFTY`, `IDX:FINNIFTY` (`IDX` is the canonical
  exchange; adapters map it to `IDX_I` / `NSE_INDEX` internally)
- future: `NFO:NIFTY:20261112:FUT`
- option: `NFO:NIFTY:20261112:25000:CE`

The gateway parses `IDX:` IDs as indices explicitly; other IDs use
`InstrumentId.parse`. Per-broker resolution prefers the broker's registry
(`broker.registry.resolve(str(id))`), then falls back to
`instrument_from_id`. A broker is reported in `brokers` only when that
broker's own registry holds metadata for the canonical ID; mere parseability
does not mean the broker knows the instrument.

### 5.2 REST endpoints

| Method + path | Request | Response |
|---|---|---|
| `GET /v1/health` | — | `{status, ready, brokers:[{name, connected, ready, capabilities, health}]}` |
| `GET /v1/brokers` | — | capabilities, limits, health, active primary per capability |
| `GET /v1/instruments/search?q=&asset_class=&limit=` | — | `[{instrument_id, symbol, exchange, asset_class, expiry, strike, right, brokers:[...]}]` |
| `GET /v1/instruments/resolve?q=&asset_class=` | — | same candidates, best match first |
| `GET /v1/history/{instrument_id}?timeframe=1m&from=&to=&source=auto\|datalake\|broker&broker=auto\|dhan\|upstox&fill_gaps=false` | ISO-8601 `from`/`to` | `{instrument, timeframe, source, broker, bars:[{t,o,h,l,c,v,oi?}], coverage:{requested, returned, complete}, degraded, cached}` |
| `POST /v1/history/batch` | `{requests:[{instrument_id, timeframe, from, to}], fill_gaps?}` ≤ 200 | per-item result or `{error}` |
| `GET /v1/quote/{instrument_id}` | — | `{instrument, ltp, bid, ask, volume, oi, ohlc, ts, broker}` |
| `POST /v1/quotes` | `{instruments:[...]}` ≤ 500 | `{quotes:{id:…}, errors:{id:…}}` |
| `GET /v1/depth/{instrument_id}?levels=5\|20\|30` | — | `{instrument, requested_levels, delivered_levels, bids, asks, ts, broker, source, degraded}` |
| `GET /v1/option-chain/{underlying}?expiry=YYYY-MM-DD&live=true` | — | `{underlying, expiry, expiries:[...], strikes:[{strike, ce:{…}, pe:{…}}], broker, cached}` |
| `GET /v1/futures/{underlying}` | — | `{underlying, futures:[{instrument_id, expiry}]}` |

`t` and `ts` are **epoch milliseconds UTC**. Bars are ascending by `t`.

Semantics:

- History `from`/`to` (REST and batch) are ISO-8601 and must include a UTC
  offset (`Z` or `±HH:MM`); naive timestamps are rejected with
  `400 invalid_request`. Bar timestamps (`t`) remain epoch milliseconds UTC.
- History `source=auto`: NSE spot equity reads the datalake first;
  index/future/option and non-NSE equity go broker-direct. `source=datalake`
  never calls a broker; `source=broker` skips the datalake.
- `fill_gaps=true` fetches the missing tail of a datalake window from a broker
  and merges it (dedupe by timestamp). Default false.
- Broker bars are clipped to the requested window. Venues may widen an
  intraday request to the whole day; a bar is kept when its span (timestamp …
  timestamp + timeframe) intersects `[from, to]`. Verified live against Dhan.
- Timeframes: `1m,5m,15m,1h,1d` on Dhan; `1m,5m,15m,30m,1h,1d,1w` on Upstox.
  If neither broker supports the requested timeframe, fetch 1m and resample
  with `HistoricalSeries.resample`.
- Batch history groups requests by selected broker and uses
  `ParallelHistoryFetcher` per group (one broker per fetcher, by construction).
- REST quotes use `broker.quote_batch` / `ltp_batch` (never per-instrument
  loops when the broker supports batching).
- Depth: broker REST exposes 5 levels. For `levels > 5`, serve the latest
  book from the live depth cache when a depth subscription is active;
  otherwise return 5 levels with `degraded=true`. Never silently claim more
  levels than delivered.
- Option chain: `expiry` defaults to the nearest non-expired expiry derived
  from the instrument master; the gateway never fans out across all expiries
  implicitly. `live=true` enriches `ce`/`pe` legs with batch quotes for the
  nearest expiry only.
- Futures are master-derived (`broker.future_chain`) and never hit a REST
  endpoint.

### 5.3 WebSocket `/v1/stream`

Client → server:

```json
{"op":"subscribe","instruments":["IDX:NIFTY","NFO:NIFTY:20261112:25000:CE"],"mode":"quote","levels":5}
{"op":"unsubscribe","subscription_id":"..."}
{"op":"ping"}
```

`mode`: `quote` (ltp/bid/ask/volume/oi) or `depth` (book). `levels` applies to
depth; the gateway subscribes the deepest stream the selected broker supports
(Dhan depth-20, Upstox full_d30) and trims frames to the requested level.

Server → client:

```json
{"op":"ack","subscription_id":"...","instruments":[...],"mode":"quote","levels":5,"broker":"dhan"}
{"op":"quote","instrument":"IDX:NIFTY","ltp":...,"bid":...,"ask":...,"volume":...,"oi":...,"ts":...}
{"op":"depth","instrument":"...","bids":[[price,qty],...],"asks":[[...]],"levels":5,"ts":...}
{"op":"status","state":"ready|degraded|switching|halted","broker":"dhan","capabilities":{...}}
{"op":"gap","instrument":"...","from_ms":...,"to_ms":...,"reason":"broker_switch"}
{"op":"pong","ts":...}
{"op":"error","code":"...","message":"..."}
```

- One upstream subscription per `(instrument, mode)` regardless of client
  count; per-client fan-out with drop-oldest tick queues and a separate
  control queue (never evicted; overflow closes the socket with 1013).
- Server heartbeat every 20 s; clients may `ping` at any time.
- Subscription limit per client: `GATEWAY_MAX_SUBSCRIPTIONS` (default 1000).

## 6. Routing, health and failover

`MarketDataRouter` selects brokers per capability (`live`, `quotes`,
`history`, `depth`, `chain`, `instruments`).

Defaults (all configurable):

| Capability | Primary | Fallback | Reason |
|---|---|---|---|
| live quotes/depth | Dhan | Upstox | 1000-instrument cap, 20-level depth socket |
| REST quotes | Upstox | Dhan | 25/s vs Dhan 1/s |
| history | Upstox | Dhan | 50/s vs Dhan 4/s |
| option chain | Dhan | Upstox | all-expiry list; Upstox requires expiry |

REST selection order: explicit `broker=` override (validated) → healthy
primary → healthy secondary → primary in half-open probe → `503
no_healthy_broker`.

Failover triggers: `RateLimitError`, `BrokerUnavailableError`,
`ConnectionTimeoutError`, retry exhaustion, mapped 5xx, `AuthenticationError`,
`CapabilityNotSupportedError`, and resolution failure. Do not fail over on
caller errors (`ValueError` from bad timeframe is handled by the resample
fallback, not by another broker unless the other supports it).

Health bookkeeping is per broker + capability:

- success resets consecutive failures;
- `RateLimitError` → cooldown 30 s (the adapter's own cooldown applies inside);
- `BrokerUnavailableError` / timeout / retry exhaustion → 15 s;
- `AuthenticationError` → 60 s;
- unknown exception → 5 s;
- after cooldown, the next request probes; a success restores `ok`.

Live failover is cold switch:

- one active `BrokerFeed`; a monitor detects disconnect or a stale tape
  (`no tick for stale_after`, default 15 s);
- switch: close/unsubscribe active feed, subscribe the wanted set on the other
  broker, emit `status: switching` then `ready`, and one `gap` frame per
  instrument;
- depth level degradation is explicit (`delivered_levels` / `degraded`);
- no automatic switch-back mid-session; if the secondary also fails, state is
  `halted` and a retry loop continues until one feed recovers.

## 7. Rate limiting, concurrency and caching

Two layers:

1. **Broker-native** (unchanged, per process): token bucket + min interval +
   rolling windows + cooldown + circuit breaker + retries inside each adapter.
2. **Gateway admission**:
   - per API key (and per client IP when no key) token bucket for REST
     (`GATEWAY_REST_RATE_PER_SECOND`, default 20; burst 40); `429` with
     `Retry-After`;
   - bounded per-broker worker pool (`GATEWAY_BROKER_WORKERS`, default 8);
     sync broker calls run via `asyncio.to_thread` on a sized limiter;
   - single-flight for identical history/chain/quote-batch keys;
   - short-window cache reuse for quotes (250 ms) and chains (10 s);
   - history: closed ranges cached for the session; today's range 5 s;
   - option expiry lists: 300 s.

Single process only: startup refuses `workers > 1`. Two processes would hold
independent broker limiters and multiply the vendor quota.

## 8. Authentication and configuration

Environment (loaded from `.env.local` via `tradex_config.env.load_env_file`
when present):

| Variable | Default | Meaning |
|---|---|---|
| `GATEWAY_HOST` | `127.0.0.1` | bind address |
| `GATEWAY_PORT` | `8100` | port |
| `GATEWAY_API_KEY` | unset | required unless anon is allowed on loopback |
| `GATEWAY_ALLOW_ANON` | `false` | allow unauthenticated loopback clients |
| `GATEWAY_BROKERS` | `dhan,upstox` | enabled brokers |
| `GATEWAY_PRIMARY_LIVE` | `dhan` | live primary |
| `GATEWAY_PRIMARY_QUOTES` | `upstox` | REST quote primary |
| `GATEWAY_PRIMARY_HISTORY` | `upstox` | history primary |
| `GATEWAY_PRIMARY_CHAIN` | `dhan` | option-chain primary |
| `GATEWAY_REST_RATE_PER_SECOND` | `20` | consumer REST rate |
| `GATEWAY_REST_BURST` | `40` | consumer REST burst |
| `GATEWAY_MAX_SUBSCRIPTIONS` | `1000` | WS instrument cap per client |
| `GATEWAY_STALE_AFTER_SECONDS` | `15` | live feed staleness threshold |
| `GATEWAY_MASTER_CACHE_DIR` | runtime default | master cache directory |

API key comparison is constant-time. Binding a non-loopback host requires an
API key and at least one configured broker.

## 9. Errors and observability

Envelope: `{"error":{"code","message","broker","retryable","retry_after"}}`.

Codes: `unauthorized` 401, `forbidden` 403, `instrument_not_found` 404,
`invalid_request` 400, `unsupported_timeframe` 422,
`capability_not_supported` 422, `consumer_rate_limited` 429,
`broker_rate_limited` 503, `broker_unavailable` 503, `no_healthy_broker` 503,
`feed_unavailable` 503, `internal_error` 500.

`/metrics` (Prometheus text): request counts/latency by endpoint and broker,
broker failures, failovers, feed state, queue depth, cache hit rate. Structured
logs carry request IDs and never log tokens.

## 10. Testing

- Unit: config parsing, canonical ID parsing/resolution, router selection
  matrix, health cooldowns, cache TTL/single-flight, error mapping.
- Contract: FastAPI `TestClient` with fake broker runtimes — history/quote/
  depth/chain/futures shapes, auth refusals, consumer 429.
- WS: subscribe/unsubscribe, refcounted upstream subscriptions, fan-out,
  queue overflow, heartbeat, status/gap frames.
- Failover: primary 429/5xx → secondary serves; both fail → 503; live switch
  emits status+gap and resubscribes; secondary failure → halted.
- Integration (opt-in, `-m live`): one index, one equity, one option chain
  against real credentials; excluded from CI.
- Existing gates: import-linter, `tests/test_import_boundaries.py`, ruff,
  mypy baseline, coverage (`fail_under=80`).

## 11. Delivery slices

1. Skeleton: package, config, auth, errors, `BrokerRuntime` × 2, directory,
   health/brokers/instruments routes.
2. History REST + batch + cache + router failover.
3. Quote/depth/option-chain/futures snapshots + coalescing/degrade semantics.
4. Live WS + hub + heartbeat + queue policies.
5. Failover hardening + metrics + live smoke test.
6. Fix verified existing defects: `routes/stream.py:70` syntax error;
   `routes/market_data.py` quote/search serialization.

## 12. Out of scope

- Order placement, portfolio, execution, strategy, replay, indicators.
- Multi-process or horizontal scaling of broker connections.
- Order book reconstruction beyond broker-provided depth.
- Persisting index/option history to a second parquet lake.

## 13. Corrections to existing code (required for slice 6)

- `interfaces/src/tradex_interfaces/routes/stream.py:70` currently reads
  `async _REPLAY_GUARD_LOCK = threading.Lock()`, which is a syntax error and
  makes `/ws/stream` unimportable. Remove `async`.
- `interfaces/src/tradex_interfaces/routes/market_data.py:36` calls
  `dict(quote)` on a frozen `Quote` dataclass, raising `TypeError` → 502 on the
  success path. Serialize with the domain serializer.
- `GET /search` declares `response_model=list[str]` but returns `Instrument`
  objects; return metadata dicts and widen the response model.
