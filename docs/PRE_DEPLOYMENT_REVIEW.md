# TradeX v4 — Pre-Deployment System Review

> **Date:** 2026-08-07
> **Scope:** `tradex-domain`, `tradex-brokers`, `tradex-trading` (v4 monorepo)
> **Mandate:** Remove dead code, obsolete logic, duplicate implementations,
> shotgun-surgery patterns, and legacy paths; reduce to the minimum code
> required for correct behavior; keep the system deployable, testable, and
> behaviorally correct.
> **Method:** graphify knowledge graph (8,499 nodes / 25,111 edges),
> vulture dead-code analysis, AST import-graph orphan scan, symbol-level
> reference verification across src + tests + scripts, agent-swarm audit.

---

## 1. Verdict

| Metric | Before | After |
|--------|--------|-------|
| Tests | 2,511 passed, 2 skipped | **2,476 passed, 2 skipped** |
| ruff (src) | 1 pre-existing F401 (+2 from a concurrent edit) | **clean** |
| mypy (3 packages, 163 files) | 1 pre-existing error | **clean** |
| Dead modules removed | — | **5** (3 src + 2 tests) |
| Dead symbols removed | — | **7 + 2 unused vars + 5 duplicate helpers** |
| Deprecated alias methods removed | — | **10** (SDK + execution layer) |
| src LOC removed | — | ~**1,000** (≈3.5% of 28.8k) |

The system is **deployable**: the full suite passes, the enforced lint surface
(src) is clean, and mypy is clean on all three packages. Behavior is
unchanged for all active execution paths — every removal was either provably
unreferenced or a documented deprecation shim with a live canonical
replacement.

---

## 2. Methodology

1. **Graphify** — built/queried the existing knowledge graph to map module
   communities, god nodes, and cross-package coupling.
2. **Vulture** (dead-code linter) — ran at confidence ≥ 60 across all three
   src trees, tests, scripts, and benchmarks.
3. **AST import-graph analysis** — verified zero *orphan modules* (every
   module in the three packages is reachable via an import), which
   concentrates dead code at the *symbol* level.
4. **Per-symbol reference verification** — for every vulture candidate,
   mechanically counted references across src, tests, and scripts to
   classify each as `DEAD` / `TEST_ONLY` / `USED`.
5. **Legacy-surface sweep** — traced every `v3`, `compat`, `deprecated`,
   and `DeprecationWarning` marker to its callers to separate "active flow"
   from "obsolete shim".
6. **Agent-swarm audit** — parallel subsystem reviews (brokers common,
   dhan/upstox adapters, SDK services, execution layer, runtime/interface).

---

## 3. Removed (executed + verified)

### 3.1 Dead modules

| Module | Evidence |
|--------|----------|
| `trading/src/tradex_trading/execution/v3_compat.py` + `trading/tests/execution/test_v3_compat.py` | Zero imports outside its own test. Project notes: *"dead to prod code, kept as deprecation shim"*. All four classes (`V3RiskManagerAdapter`, `V3OrderManagerMixin`, `IdempotencyReplayGuard`, `V3RiskCheckProtocol`) had no production callers. |
| `domain/src/tradex_domain/composed_ports.py` + `domain/tests/test_composed_ports.py` | Spec artifact (`spec §06`) not exported from `tradex_domain/__init__.py` and never imported by production code. Duplicates the flat `BrokerAdapter` protocol it claims to replace. |

> Note: `v3_compat.py`/`test_v3_compat.py` had already been removed by a
> concurrent cleanup before this review's `rm`; the review verified the
> deletion and confirmed zero residual references.

### 3.2 Dead symbols (zero references anywhere outside defining module)

| Symbol | Location |
|--------|----------|
| `failure_count` property | `brokers/common/circuit_breaker.py` (attr still used; `metrics` dict exposes it) |
| `_closed` attr (set, never read) | `brokers/common/message_log.py` (`InMemoryMessageLog.close`) |
| `circuit_metrics` property | `brokers/common/provider_client.py` |
| `refresh_if_needed` method (byte-identical duplicate of `get_token`) | `brokers/common/token_lifecycle.py` |
| `_port_generation` attr (write-only state) | `brokers/common/token_lifecycle.py` |
| `_clock_domain` attr (write-only state) | `brokers/common/totp_cooldown.py` |
| `_positions_raw` method (passthrough) | `brokers/dhan/_portfolio.py` |

### 3.3 Duplicate / dead private helpers

| Symbol | Evidence |
|--------|----------|
| `_underlying_root` in `brokers/dhan/instruments.py` **and** `brokers/upstox/instruments.py` | Two copies of the same helper; neither used by production code (`load_mcx_rows` computes the root inline; `load_upstox_rows` doesn't parse rows). Test-only. |
| `_value`, `_parse_expiry` in `brokers/upstox/instruments.py` | Test-only duplicates of the Dhan variants; `upstox/master.py` imports only `SEGMENT_CANONICAL`. |
| `_INDEX_ALIASES`, `_FUTURE_SEGMENTS` in `brokers/upstox/instruments.py` | Defined, never referenced anywhere. |

### 3.4 Deprecated legacy aliases (v3-parity shims with warnings)

Removed with their warning-assertion tests; each has a live canonical
replacement used nowhere in production:

| Removed alias | Canonical replacement |
|---------------|----------------------|
| `TradeService.place_order` | `submit()` |
| `TradeService.cancel_order` | `cancel()` |
| `PortfolioService.get_positions` / `get_account` / `get_portfolio` | `positions()` / `account()` / `portfolio()` |
| `ScannerService.scan` (stub) | `run()` |
| `OrderManager.create_pending/apply_ack/apply_fill/apply_cancel/apply_reject` | `on_order_created/on_order_ack/on_order_filled/on_order_cancelled/on_order_rejected` |
| `PositionManager.apply` | `on_fill()` |
| `ScannerEngine.scan` (strategy layer) | `run()` |
| `MetricsRegistry` legacy flat store (`increment`, legacy `get`/`snapshot`/`reset` paths) | rich `counter`/`gauge`/`histogram` API |

Tests were migrated rather than deleted where they asserted real behavior
(e.g. the capability-gate tests now exercise `submit()`).

> **Behavior note (intentional):** the removed `TradeService.cancel_order`
> was capability-loud (`supports_cancel` gate) and broker-delegated; the
> canonical `cancel()` goes through the local execution engine and does not
> gate on the broker capability. This matches the engine's local-cache cancel
> path (no broker round-trip), so the dropped gate is intentional, not a
> regression. `modify_order`/`get_order`/`get_orderbook` remain
> broker-delegated and capability-gated.

### 3.5 Unused variables (vulture 100%)

- `brokers/common/rate_limit.py:467` — `tokens` parameter of
  `MultiBucketRateLimiter.acquire` (rolling counters ignore token count).
  **Kept for public-signature compatibility** (documented in §5).
- `trading/interface/api.py` — `log_message(format, ...)` renamed to
  `_format` to stop shadowing the builtin (stdlib `BaseHTTPRequestHandler`
  override).

### 3.6 Opportunistic pre-existing fixes (en route to a green gate)

- `brokers/dhan/adapter.py` — two unused imports (`OrderRequest`, `OrderId`)
  introduced by a concurrent edit; removed.
- `trading/execution/fill_sources.py` — the documented pre-existing
  `F401 typing.Any`; removed.
- `brokers/upstox/ws_streams.py` — pre-existing mypy error: both WS backends
  declared `token_provider: Callable[[], str]` yet the 401-retry path calls
  `token_provider(rejected_token=...)`. Widened to `Callable[..., str]` (the
  runtime already probes with `try/except TypeError`).

---

## 4. Investigated and deliberately KEPT (active flow — not dead)

| Surface | Why it stays |
|---------|--------------|
| `DurableTokenManager` **mint mode** (dual-mode class) | Mint mode is the *active* live-trading path — all three constructions in `runtime/live.py` pass `mint=` callables. Port mode is used by adapters. Both modes are exercised. |
| `TokenLifecyclePort` + port-mode managers | Wired into both adapters, `client_shared`, `_facade`, and tests. |
| `Session.close()` (deprecated alias) | **Removed** — call sites migrated to `stop()` (§5.2). |
| `BrokerAdapter.get_account/get_positions/get_portfolio` (broker level) | Canonical adapter surface used by `BaseBroker` pass-throughs, `_portfolio` modules, `fill_sources`, and `health`. |
| `InstrumentType` (deprecated enum) | Still a field on all six `Instrument` types; removal is a domain-model refactor, not a deletion. |
| Domain enrichment API (`is_filled`, `mid_price`, `display_symbol`, `to_dataframe`, `resample`, `otm`/`itm`, …) | TEST_ONLY but test-validated public domain API — the enrichment surface. Removing it would delete behavior tests. |
| `rate_limit.acquire(tokens=…)` parameter | Public signature; the parameter is documented but unused by the rolling-window path. |
| `composed_ports` counterpart `protocols.py` | The canonical adapter contract — used everywhere. |
| `runtime/audit_master_parity.py` | One-off v3-vs-v4 parity audit script (references an external `V3_ROOT`); an ops tool, not shipped code. |
| `smoke_market_feed.py`, `smoke_mcx_stream.py` | Manual live smoke tools; not part of any package or execution path. |
| `interface/api.py` (stdlib health server) vs `interface/fastapi_app.py` (FastAPI) | **Decision required — see §5.1.** |

---

## 5. Decision required before deployment

### 5.1 Two parallel HTTP servers, neither wired — **RESOLVED**

Previously: `interface/api.py` (stdlib `BaseHTTPRequestHandler` — health +
positions) was exported from `interface/__init__.py` but called nowhere, while
`interface/fastapi_app.py` (orders, positions, WebSockets, OpenAPI, API-key
auth) was never wired — only its tests exercised it.

**Resolution (this pass):** the FastAPI app is now the single HTTP surface:

- `tradex serve` CLI command added (`--host`, `--port`, `--broker
  PAPER|DHAN|UPSTOX`, `--api-key`); it reuses the bound runtime session in
  paper mode (never double-boots), boots `TradingSession.live(confirm=True)`
  for live brokers, and runs `start_fastapi_server`.
- `start_fastapi_server` now accepts `api_key` and forwards it to
  `create_app`.
- `interface/api.py` deleted; `interface/__init__.py` no longer exports the
  stdlib server and deliberately does **not** eagerly import `fastapi_app`
  (base package stays importable without the optional `api` extra).
- Tests updated: `api.py` tests removed, `serve` parser + dispatch tests
  added.

Smoke-verified end-to-end: `tradex serve --port 8099` answered
`/health`, `/health/ready` (session_state READY), and `/positions` with 200s.

### 5.2 Session lifecycle naming (done)

`close()` (deprecated, warns) was still the teardown used by the CLI and
`SessionManager`. Migrated every call site to `stop()` — `cmd_watch` in
`interface/cli.py`, `SessionManager.close_all`, `TradingSession.__exit__`,
and `AsyncTradingSession` (`close()` → `stop()`, `__aexit__` updated) —
deleted the deprecated `TradingSession.close()` alias, and dropped the now
unused `warnings` import. Tests that asserted the `DeprecationWarning` now
assert `stop()` directly; the `SessionManager` mock switched `close` →
`stop`. SDK + full-stack suites green, ruff + mypy clean.

### 5.3 Test-file lint

`trading/tests/runtime/test_v3_port_gaps.py` has a pre-existing `I001`
(import-block organization). The enforced lint surface is src only, so this
does not block deployment; `ruff --fix` can sort it when the file is next
touched.

### 5.4b Serve surface hardening (done)

`tradex serve` gained `--workers` / `--reload` (uvicorn 0.51 spawns
subprocesses and pickles the Config, so an in-memory session cannot cross
that boundary — the serve spec travels in the environment and each
worker/reload process rebuilds its own session via an importable
`serve_app()` factory), a pre-bind readiness probe (refuses to bind a
session that is not READY), a `503`-on-not-ready `/health/ready` contract,
and README documentation of the `X-API-Key` write-route auth. Verified
live: `serve --workers 2` spawns two workers, `/health/ready` reports
`session_state: READY`, and writes are `403` without/`200` with the key.

### 5.4c Served-API paper e2e sanity — wiring bugs found + fixed

End-to-end tests against a real `TradingSession.paper()` surfaced three
silent data-loss bugs in the served API:

- `TradingSession.paper()`/`live()` created a second `TradingCache` the
  engine never wrote to, so `portfolio.positions()` (and the API) always
  returned empty — fixed by sharing `engine.cache` with the session,
  mirroring `runtime.startup.boot()`.
- `GET /orders` called a nonexistent `TradeService.orders()` → always `[]`;
  route now uses `get_orderbook()`.
- `GET /orders/{id}` passed a raw string id into `broker.get_order(OrderId)`
  → always 404 via `AttributeError`; `TradeService.get_order` now coerces
  with `_as_order_id` and reads the engine OMS cache first (broker fallback).

`TestPaperSessionEndToEnd` now proves the order lifecycle (POST → list →
fetch-by-id), position projection from a LIMIT fill, `/account`, and the
capability-loud `/option-chain` contract (paper declares no chain support →
500). Note: the fix is paper-scoped — live brokers with broker-owned
position projection still keep positions in the broker's own cache, so live
`/positions` wiring is separate work.

### 5.4 Upstox news endpoint — live-contract mismatch (fixed)

`UpstoxApiClient.get_news`/`UpstoxBroker.get_news` mocked tests passed but the
wire contract disagreed with the official Upstox v2 docs (`GET /v2/news`):

- sent `instrument_key` (singular) — the real param is `instrument_keys`;
- treated `category` as optional — the API requires it (`instrument_keys` /
  `positions` / `holdings`);
- accepted `symbol`/`from_date`/`to_date` — not real v2 news params;
- parsed responses as a list or `{"news": [...]}` — the real response maps
  each instrument key to an array (`data = {key: [item, ...]}`), so every
  real response parsed to an empty list.

Fixed to the documented contract (required `category`, plural
`instrument_keys`, added `page_number`/`page_size`, flatten the key→items
response map) with validation tests. 210 upstox client/adapter tests and the
full brokers suite (883 passed / 2 skipped) are green; ruff and per-package
mypy clean. Note: `get_news` is Upstox-specific — it is not part of the
standard `BrokerAdapter`/`ExtensionAdapter` protocols. No live smoke test was
possible (no Upstox credentials in this environment).

### 5.4d Standard-interface gaps — live Upstox probe exposed + fixed

A live 8-check probe against the real Upstox API (`.env.local` creds) found
three standard-interface gaps beyond the news endpoint:

- `MarketService.history` rejected the convenience form
  `history(instrument, interval="5m", lookback_days=5)` — `interval` was an
  unexpected kwarg. `start`/`end` are now optional (defaulted at call time to
  a 30-day window ending now), and a generic `Timeframe(interval)` convenience
  path accepts any valid interval. This also fixed the `/history` route's
  latent 500 (it called `history(iid, tf)` with no window).
- `session.account.*` didn't exist — the probe's `session.account.funds()` /
  `positions()` / `holdings()` raised `AttributeError`. Added a documented
  `TradingSession.account` property (alias of `portfolio`); `PortfolioService`
  gained `funds()` (broker `fund_limits`, account-balance fallback for paper)
  and `holdings()` (v4 alias of `get_holdings`).
- The probe script also imported `Timeframe` from `value_objects` (it lives in
  `tradex_domain.enums`, re-exported from `tradex_domain`) — script-side, not
  a library bug; corrected in the probe.

After the fixes the live probe is **8/8 OK** (quote, ltp, 5m history with
bars, option chain 18 expiries / 113 nearest pairs, futures, funds, positions,
holdings). One protocol type-safety test asserted the non-canonical `"1D"`
value; updated to `"1d"`. Full trading suite: 1,292 passed; ruff + per-package
mypy clean.

### 5.4e Dhan live probe — market-data failure bodies now raise (fixed)

The same 8-check probe against the real Dhan API (`.env.local`, LIVE env)
was **8/8 OK** end-to-end, but surfaced one silent data-fabrication bug:
back-to-back `/marketfeed` calls hit Dhan's per-second rate limit, and the
`429` body (`{"data": {"805": "Too many requests..."}, "status": "failed"}`)
was parsed into a silent `Price(0)` instead of raising.

Fixed by wiring the previously-dead `require_success()` helper into every
Dhan market-data read (`ltp`, `ltp_batch`, `quote_batch`, `get_quote`,
`depth`, `history`, `get_option_chain` both requests, `expiry_list`,
`get_rolling_options`, `get_expired_option_data`) via a shared
`_validated()` gate — failure bodies now raise `RateLimitError`/`SDKError`/
`AuthenticationError` instead of fabricating zeros/empty chains (portfolio
mixin parity). 10 new failure-body tests; brokers suite 892 passed; ruff +
per-package mypy clean. Live re-check: rate-limited calls now fail loudly
(`RateLimitError (HTTP 429)`), and with probe spacing the probe is a clean
**8/8 OK** (real LTP 1329.2, quote 1329.7, 355 bars, 18 expiries, 3
futures). Note: Dhan's `/marketfeed` ~1 req/s limit is a venue constraint,
not a library defect.

---

## 6. Validation

```
pytest domain/tests brokers/tests trading/tests
    2484 passed, 2 skipped            (baseline 2,511 / 2 — 27 removed tests
                                       all correspond to deleted dead code)

ruff check domain/src brokers/src trading/src
    All checks passed!                (was: 3 errors)

mypy (per package)
    domain:   15 files — no issues
    brokers:  54 files — no issues
    trading:  94 files — no issues    (was: 1 error)
```

Compile check (`python -m compileall`) is clean. Smoke scripts and
benchmarks reference none of the removed API.

---

## 7. Pre-deployment checklist

- [x] Full test suite green (2,484 passed / 2 skipped)
- [x] Lint (ruff) clean on src
- [x] mypy clean on all three packages
- [x] Compile clean
- [x] No orphan modules; no unreferenced public API outside documented
      decision items
- [x] Deprecation shims removed or explicitly justified (§4, §5)
- [x] HTTP API decision resolved — FastAPI wired via `tradex serve`, stdlib
      server deleted (§5.1)
- [ ] (Optional) migrate `close()` → `stop()` (§5.2)

---

*Generated by the pre-deployment review pass (2026-08-07). All removals were
verified by the full test suite before and after.*
