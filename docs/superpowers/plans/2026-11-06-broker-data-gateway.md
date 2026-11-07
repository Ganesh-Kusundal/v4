# Broker Data Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Build `tradex_gateway`, a dual-broker FastAPI service exposing historical data, live quotes/depth, and option chains to the scalper app with health-aware failover and internal rate limiting.

**Architecture:** New uv workspace member `gateway/` (`tradex_gateway`). One process owns one `BrokerRuntime` per configured broker (Dhan, Upstox), each built through `build_broker_from_env(..., allow_order_operations=False)`. A `MarketDataRouter` selects brokers by capability and health; `HistoryService` is datalake-first for NSE spot equity and broker-direct otherwise; `LiveHub` maintains refcounted upstream WS subscriptions and cold-switches brokers on failure.

**Tech Stack:** Python 3.12+, FastAPI, uvicorn, Pydantic, httpx (tests), existing `tradex_*` packages.

**Spec:** `docs/superpowers/specs/2026-11-06-broker-data-gateway-design.md`

## Global Constraints

- Never construct `DhanBroker`/`UpstoxBroker` directly; use `tradex_runtime.live.build_broker_from_env`.
- Brokers are built with `allow_order_operations=False`; the gateway must never expose order endpoints.
- One process only; refuse `workers > 1`.
- Synchronous broker calls must run in worker threads, never on the event loop.
- Canonical IDs: `NSE:RELIANCE`, `IDX:NIFTY`, `NFO:NIFTY:20261112:FUT`, `NFO:NIFTY:20261112:25000:CE`.
- Timestamps returned to clients are epoch milliseconds UTC.
- No `tradex_interfaces` import from `tradex_gateway` source.
- Run tests with: `.venv/bin/python -m pytest <paths> -p no:cacheprovider --import-mode=importlib -c pyproject.toml`.

---

### Task 1: Package scaffold, config, errors, auth, models

**Files:**
- Create: `gateway/pyproject.toml`
- Create: `gateway/src/tradex_gateway/__init__.py`, `__main__.py`, `cli.py`
- Create: `gateway/src/tradex_gateway/config.py`, `errors.py`, `auth.py`, `models.py`
- Modify: root `pyproject.toml` (workspace members, uv sources, pythonpath, testpaths, importlinter)
- Modify: `tests/test_import_boundaries.py` (`_SRC`, `_ALLOWED`)
- Test: `gateway/tests/test_config.py`, `gateway/tests/test_auth.py`

**Interfaces:**
- Produces: `GatewayConfig.from_env(env=None)`, `GatewayConfig.validate()`
- Produces: `GatewayError` hierarchy with `status`, `code`, `retryable`, `retry_after`
- Produces: `TokenBucket(rate, burst).allow(key, now)`, `require_api_key`
- Produces: Pydantic models `HistoryBatchRequest`, `QuotesRequest`, `SubscribeMessage`

**Steps:**
- [x] Write failing tests for env parsing (defaults, comma lists, bools, invalid numbers) and token bucket exhaustion/refill.
- [x] Run tests; expect import errors.
- [x] Implement config/errors/auth/models and package files.
- [x] Wire root workspace, pythonpath, testpaths, import-linter, boundary test.
- [x] Run `pytest gateway/tests/test_config.py gateway/tests/test_auth.py` → PASS.
- [x] Commit.

### Task 2: BrokerRuntime, health, directory

**Files:**
- Create: `gateway/src/tradex_gateway/brokers.py`, `directory.py`
- Create: `gateway/tests/fakes.py`
- Test: `gateway/tests/test_health.py`, `gateway/tests/test_directory.py`

**Interfaces:**
- Produces: `BrokerRuntime(name, broker, error=None)` with `.broker`, `.available()`, `.health(capability)`, `.instrument(iid)`, `.search(q)`, `.close()`
- Produces: `BrokerHealth.available(now)`, `.record_success()`, `.record_failure(seconds, message)`, `.snapshot()`
- Produces: `InstrumentDirectory(runtimes).resolve(text, asset_class=None) -> ResolvedInstrument`, `.search(...)`, `.nearest_expiry(underlying)`
- Produces: `ResolvedInstrument(instrument_id, instrument, brokers, per_broker)`

**Steps:**
- [x] Write failing tests: cooldown expires; success resets; directory resolves `IDX:NIFTY` to an `Index` via registry; search dedupes and reports broker availability.
- [x] Implement `FakeBroker` (methods used by gateway) and the two modules.
- [x] Run `pytest gateway/tests/test_health.py gateway/tests/test_directory.py` → PASS.
- [x] Commit.

### Task 3: MarketDataRouter

**Files:**
- Create: `gateway/src/tradex_gateway/router.py`
- Test: `gateway/tests/test_router.py`

**Interfaces:**
- Consumes: `BrokerRuntime`, `BrokerHealth`
- Produces: `MarketDataRouter(config, runtimes).call(capability, fn, preferred=None) -> (result, broker_name)`
- Produces: `CAPABILITIES = ("live","quotes","history","depth","chain","instruments")`

**Behavior:** try preferred, then healthy configured primary, then healthy secondary; on typed broker errors record failure and continue; `ValueError`/`InstrumentNotFoundError`/`CapabilityNotSupportedError` do not set cooldown but allow the next broker; all exhausted → typed `GatewayError`.

**Steps:**
- [x] Write failing tests: primary 429 → secondary called; both fail → `NoHealthyBroker`; cooldown expiry re-admits primary; instrument-not-found falls through.
- [x] Implement router.
- [x] Run `pytest gateway/tests/test_router.py` → PASS.
- [x] Commit.

### Task 4: HistoryService

**Files:**
- Create: `gateway/src/tradex_gateway/history.py`
- Test: `gateway/tests/test_history.py`

**Interfaces:**
- Consumes: directory, router, `MarketWindowLoader`
- Produces: `HistoryService(...).history(instrument_id, timeframe, start, end, source="auto", broker=None, fill_gaps=False) -> dict`
- Produces: `HistoryService(...).batch(requests) -> list[dict]`

**Behavior:** NSE spot equity reads the datalake first; index/future/option and non-NSE go broker-direct. Broker fetch chunks by `BROKER_LIMITS` (Dhan 90d intraday, Upstox 30d). Unsupported timeframe → fetch 1m and `HistoricalSeries.resample`. `fill_gaps` merges the missing tail. Closed ranges cached 3600 s, current range 5 s; identical keys single-flight. Bars are epoch-ms UTC dicts.

**Steps:**
- [x] Write failing tests: datalake hit returns source=datalake; empty lake falls through to broker; broker unsupported timeframe resamples; cache hit avoids the second broker call; fill_gaps merges tail.
- [x] Implement service with injectable `window_loader` (fake in tests).
- [x] Run `pytest gateway/tests/test_history.py` → PASS.
- [x] Commit.

### Task 5: Quote, depth, chain, futures services

**Files:**
- Create: `gateway/src/tradex_gateway/market.py`
- Test: `gateway/tests/test_market.py`

**Interfaces:**
- Produces: `QuoteService.quote(iid, broker=None)`, `.quotes(iids, broker=None)`
- Produces: `DepthService.depth(iid, levels, live_cache=None, broker=None)`
- Produces: `ChainService.option_chain(underlying, expiry=None, live=False)`, `.futures(underlying)`
- Consumes: directory, router, optional live depth cache callable

**Behavior:** quotes use `quote_batch` (≤500 per call); depth REST delivers 5 levels, deeper requests use the live cache or set `degraded=true`; option chain defaults to `directory.nearest_expiry`, caches 10 s, never fans out all expiries; futures are master-derived.

**Steps:**
- [x] Write failing tests: batch quote shape; depth degrade flag; chain expiry default + cache; futures serialization.
- [x] Implement services.
- [x] Run `pytest gateway/tests/test_market.py` → PASS.
- [x] Commit.

### Task 6: LiveHub and `/v1/stream`

**Files:**
- Create: `gateway/src/tradex_gateway/live.py`
- Test: `gateway/tests/test_live.py`

**Interfaces:**
- Produces: `LiveHub(config, router, runtimes).subscribe(client_id, instruments, mode, levels, queue, loop) -> ack`
- Produces: `.unsubscribe(client_id)`, `.publish_quote(quote, broker_name)`, `.publish_depth(depth, broker_name)`, `.latest_depth(iid)`, `.status()`
- Produces: `BrokerFeed.start_quotes/start_depth/stop`

**Behavior:** one upstream subscription per instrument regardless of clients; per-client fan-out via `loop.call_soon_threadsafe`; quote and depth modes coexist; switch broker on disconnect/staleness (cold switch), emit `status` and `gap` frames; depth levels trimmed; `depth_levels` capability decides Dhan 20 vs Upstox 30.

**Steps:**
- [x] Write failing tests: refcount (two clients, one broker subscribe call); fan-out delivers to both queues; unsubscribe drops the upstream set; switch emits status+gap and resubscribes on the second broker.
- [x] Implement hub and feed with a fake broker.
- [x] Run `pytest gateway/tests/test_live.py` → PASS.
- [x] Commit.

### Task 7: App assembly and REST/WS routes

**Files:**
- Create: `gateway/src/tradex_gateway/app.py`
- Create: `gateway/src/tradex_gateway/cli.py` (finish)
- Test: `gateway/tests/test_api.py`, `gateway/tests/test_ws.py`

**Interfaces:**
- Produces: `create_app(config=None, runtimes=None) -> FastAPI`
- Routes: health, brokers, instruments search/resolve, history, history batch, quote, quotes, depth, option-chain, futures, `/v1/stream`, `/metrics`

**Behavior:** async handlers call sync services via `asyncio.to_thread`; API-key auth; consumer token bucket; error envelope; readiness false until a runtime is available; lifespan builds brokers in a thread only when `runtimes` was not injected.

**Steps:**
- [x] Write failing API tests: 401 without key, health shape, history via fake runtime, quote/depth/chain shapes, consumer 429.
- [x] Write failing WS test: subscribe ack, ping/pong, unsubscribe.
- [x] Implement app, routes, middleware.
- [x] Run `pytest gateway/tests/test_api.py gateway/tests/test_ws.py` → PASS.
- [x] Commit.

### Task 8: Existing defect fixes and full verification

**Files:**
- Modify: `interfaces/src/tradex_interfaces/routes/stream.py:70`
- Modify: `interfaces/src/tradex_interfaces/routes/market_data.py:21-52`
- Test: `interfaces/tests/test_market_data_routes.py`

**Steps:**
- [x] Fix `async _REPLAY_GUARD_LOCK = threading.Lock()` → `_REPLAY_GUARD_LOCK = threading.Lock()`.
- [x] Serialize `Quote` with the domain serializer; make `/search` return metadata dicts and widen `response_model`.
- [x] Add tests: stream module imports; quote handler returns a dict; search returns dicts.
- [x] Run gateway suite, interface tests, import-boundary test, ruff on new files.
- [x] Commit.

## Self-Review

- Spec coverage: contract (§5) → Tasks 4-7; routing/failover (§6) → Tasks 3, 6; rate limiting/caching (§7) → Tasks 1, 4, 5; auth/config (§8) → Task 1; errors/metrics (§9) → Tasks 1, 7; testing (§10) → every task; existing fixes (§13) → Task 8.
- No placeholders; names used later are defined in earlier Interfaces blocks.
- Type consistency: `history()` returns a dict; `router.call` returns `(result, broker_name)`; `BrokerRuntime.broker` may be `None` when construction failed.
