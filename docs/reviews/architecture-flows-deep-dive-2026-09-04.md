# TradeX v4 — Architecture, Flows & Connection Mechanics (Deep Dive)

**Date:** 2026-09-04
**Branch:** `refactor/execution-state`
**Scope:** how the system is wired, how broker connections and tokens are created and live, and the end-to-end flows — order / bracket / cancel / modify / fill / risk / MTM / streams.
**Companion to:** `docs/reviews/principal-architecture-review-2026-09-04.md` (what is wrong / target state) and `docs/design/state.md` (working-tree ledger). This document is the *mechanics* layer: every statement is traced to a file inspected at the stated branch HEAD + working tree.

---

## 1. The big picture

Three Python packages + one TypeScript frontend, with one composition root:

```
┌──────────────┐   REST + /ws/stream (FastAPI)          ┌─────────────────────────────┐
│ frontend/    │ ─────────────────────────────────────► │ trading/  (APPLICATION)      │
│ (consumer)   │                                       │  boot() = ONLY object graph   │
└──────────────┘                                       │  interface/ routes + ws      │
                                                       │  execution/ OMS · risk · MTM │
                                                       │  runtime/ live builders, feed│
                                                       │  sdk/ session, fill bridge   │
                                                       └──────┬──────────────┬────────┘
                                                              │ uses         │ adapts
                                                        ┌─────▼─────┐   ┌────▼──────────────────┐
                                                        │ domain/   │   │ brokers/  (ADAPTERS)   │
                                                        │ pure      │   │ Dhan · Upstox · Paper  │
                                                        │ kernel    │   │ REST client + WS +    │
                                                        │ (no I/O)  │   │ token lifecycle       │
                                                        └───────────┘   └───────────────────────┘
```

- **`domain/`** — pure types and FSM: `OrderRequest`/`Order`/`Fill`/`Position`/`Quote`, `OrderStatus.transition_to`, value objects (Decimal `Money`/`Price`/`Quantity`), events (`OrderPlaced`, `OrderFilled`, `OrderRejected`, `PositionUpdated`, `ErrorOccurred`), `BrokerCapabilities`, wire registry. Zero I/O.
- **`brokers/`** — the only place that owns provider wire formats. Each provider is an adapter (`DhanBroker`/`UpstoxBroker`/`PaperBroker`) that composes a REST client, WebSocket stream backends, a token manager, and an instrument registry, all over `BaseBroker`.
- **`trading/`** — application: `runtime/startup.boot()` composes everything; `execution/` owns the OMS (`ExecutionEngine`, `TradingCache`, `RiskManager`, fill sources, `MarkToMarketService`, `SQLiteOrderStore`); `interface/` is the FastAPI surface; `sdk/` exposes `TradingSession`.
- One codebase spans **four modes** — `paper`, `backtest`, `replay`, `live` — chosen by `AppConfig.mode`; the mode changes the fill source, the bus wrapper, the broker, and the risk mark policy, never the domain.

---

## 2. Boot: from process start to a READY session

Entry points all funnel into `runtime/startup.py::boot(config)` (CLI `tradex`, `serve_app()`, SDK factories `TradingSession.paper()/.live()`, `quick_start_live.py`).

**Safety gates first** (`boot` step 0): mode ∈ {paper, backtest, replay, live}; live requires a non-paper broker id; live requires `live_enabled=true`. Fail-closed: any boot error tears everything down (`_safe_teardown` closes broker/bus/writer lock).

**Step 1 — broker construction.**
- `mode=live` → `runtime/live.py::build_broker_from_env(broker_id)` — the **only** boundary that turns environment credentials into a real HTTP fetch.
- paper/backtest/replay → transport-less `PaperBroker()`/`DhanBroker()`/`UpstoxBroker()` constructed directly (paper is transport-less by design).

**Step 2-3 — metrics + bus.** Live mode wraps the `ReactiveBus` in a `ThreadSafeReactiveBus` (an RLock around publish — the broker feed thread, engine worker threads and API callers all publish concurrently; a raw RxPY Subject must never be driven from two threads).

**Step 4 — fill source per mode:** paper → `PaperFillSource`; live → `BrokerFillSource(broker)`; backtest/replay → `SimulatedFillSource`. All share one `FillModel` (slippage) + `FeeCalculator` so net P&L is mode-consistent.

**Step 4b — durability (opt-in `cfg.persistence.path`):** `MemoryIdempotencyGuard` is the default; with a path, both `SQLiteIdempotencyGuard` and `SQLiteOrderStore` share one DB file.

**Step 4c — single-writer lock (live only):** `SingleWriterLock("runtime/live/<broker>.writer.lock")`; acquire is fail-closed (a second live process on the same account refuses to boot). Held until session stop / atexit.

**Step 5-6 — risk + engine.** `RiskManager` gets `max_order_value/position/daily_loss/drawdown`, and live forces `require_fresh_marks=True` (see §6). `ExecutionEngine(bus, fill_source, risk, guard, …)` created, then the composition root binds providers:
- `risk.set_positions_provider(engine.cache.all_positions)` and `set_price_provider(engine.cache.get_quote)` — the OMS cache is the position *and* mark authority.
- `MarkToMarketService(engine.cache, bus)` subscribes to `Quote` before the session is exposed (§6).
- paper binds the shared cache into the fill source via the public `bind_cache()` seam.
- optional `cfg.risk.cash_provider` bound for paper/live only (BUY notional gate).

**Step 7 — `broker.connect()`.** For live brokers this loads the instrument master (downloaded CSV → parsed rows → `load_instruments` → atomic registry swap `replace_all`) and sets `_connected=True`. **It performs no auth handshake and opens no sockets** — the first authenticated request (or WS open) is the real connection.

**Step 7b — stream backend + live fill bridge (live only):** `broker.stream_backend()` (the broker's cached order-update WS backend) + `LiveFillBridge` subscribed to it. Best-effort: boot *succeeds* without it, but logs the stark warning that fills will never reach the OMS. `TradeBookFillIdResolver` stamps delta fills with real exchange trade ids when the venue exposes a REST trade book.

**Steps 6a/7c/7d — strategy, scanner, backtest wiring.** Persisted orders are loaded into the cache *before* session start (`order_store.load_into`), then `attach_order_persistence(bus, cache, store)` mirrors every lifecycle event to SQLite. Discovered extension strategies register into `ReactiveStrategyEngine`; `ScannerEngine` binds the broker (paper/live) or parquet datalake (backtest/replay); live gets `MarketFeed` + `InstrumentRefreshScheduler`.

**Step 8-9 — session + reconciliation.** `TradingSession(NEW → READY → STOPPED)` created. Before `start()` (which flips to READY), live runs **startup reconciliation**: pulls broker order book + positions, calls the side-effect-free `engine.reconcile`, refreshes cache statuses from the book, and trips the kill switch on HIGH/CRITICAL drift — a *newly tripped* switch refuses `session.start()`, so a diverged book never reaches the trading surface.

---

## 3. How a live broker connection is created (the full chain)

### 3.1 Credentials → environment facts

`.env.local` is loaded by the caller (`quick_start_live.py`, CLI `--env-file`, or the process env). `runtime/live.py` reads only names like `DHAN_ENVIRONMENT`, `UPSTOX_ENVIRONMENT`, `DHAN_CLIENT_ID`, `UPSTOX_API_KEY`…; **values never appear in logs.** `provider_environment()` resolves LIVE vs SANDBOX; each environment picks its own credential family (`DHAN_*` vs `DHAN_SANDBOX_*`) and base URLs (`_dhan_base`, `_upstox_bases` — deprecated-name fallbacks warn).

### 3.2 The fetch seam

`resolve_fetch(timeout)` returns a **curl_cffi Chrome-impersonating fetch** when installed (Upstox Cloudflare-blocks plain urllib with error 1010), else stdlib urllib. This fetch is injected through everything — no module ever touches urllib directly. Tests inject fake fetches; a real fetch defaults the master cache to the durable `runtime/` dir.

### 3.3 Token manager selection (build time, zero network)

- **Dhan** (`_dhan_token_manager`): if `DHAN_CLIENT_ID` + `DHAN_PIN` + `DHAN_TOTP_SECRET` exist → `MintTokenManager(state_path=DHAN_TOKEN_PATH|runtime default, mint=dhan_totp_mint(...))` with a 120 s `TotpCooldownGuard` (honors shared `DHAN_COOLDOWN_PATH` across processes). Otherwise the manager is `None` and a static `DHAN_ACCESS_TOKEN` is *required* — and used only in that no-manager case.
- **Upstox** (`_upstox_token_manager`) — three tiers: (1) OAuth refresh-token grant (`UPSTOX_REFRESH_TOKEN` + client secret) — and the manager **prefers the latest persisted refresh token** (rotation-aware); (2) TOTP self-mint (`UPSTOX_MOBILE`+`PIN`+`TOTP_SECRET`+client id/secret); (3) static token only when no mint exists.

`MintTokenManager` (common/token_lifecycle.py) is **generation-aware**: durable JSON state + a sidecar `.generation` file; token reuse until `expires_at − buffer`; mint on demand; `ensure_token(rejected_token=…)` gives **401-once per generation**; an expired-but-issued token is "trust until rejected". All state file writes are atomic (`_atomic_write_text`) and cross-process locked (fcntl flock) so sibling processes honoring the same TOTP rate limit don't double-mint.

### 3.4 Adapter composition

`DhanBroker.from_fetch(fetch, client_id, access_token, token_manager, base_url, allow_order_operations, instrument_loader)`:
1. `build_provider_client(...)` composes `HttpTransport(base_url, token_provider, auth_headers, on_auth_failure)` + `ProviderHttpClient` + `ResiliencePipeline`.
   - `token_provider` is called **per request** (`token_manager.ensure_token`), so mid-session refreshes are picked up automatically; Dhan headers are `access-token`/`client-id`, Upstox is `Authorization: Bearer`.
   - The pipeline is rate-limiter → circuit-breaker → safe-retry (429/5xx retried only for GET/HEAD/OPTIONS).
   - On 401/403 (or a business-level "invalid token" smuggled in a 2xx/400 body — Dhan DH-901/DH-906), **safe methods** call `on_auth_failure(token_sent)` → mint a fresh generation → replay exactly once. Mutations are one-shot; `submit_mutation` marks an uncertain submission on timeout/5xx and raises `OrderSubmissionUnknownError` until resolved.
2. `set_token_manager(...)` + `broker.master_loader` + `bind_stream_backend("market", broker.market_stream_backend())` — declared seams only (repo policy: no foreign private writes).

`BaseBroker` then installs a **declarative pass-through wall** (`_PASSTHROUGH_WALL` in base.py): one-line specs generate every delegated method with a gate — `"read"`, `"mutation"` (`allow_order_operations`), or `"mutation+cap:supports_super_order"` etc. So `submit_super_order` on a broker whose capability table says `supports_super_order=False` raises before any wire traffic.

### 3.5 WebSocket connections

Backends are lazy, shared, reconnect-capable (`AutoReconnectMixin`):
- **Dhan order stream** (`DhanOrderStreamBackend`, `wss://api-feed.dhan.co/v2/orderUpdate`): URL carries `?version=2&token=<fresh>&clientId=<id>&authType=2`. Order-update sockets push everything with no subscribe frame — reconnect = reopen with a **fresh token** and no replay needed.
- **Market data** (`DhanMarketDataStreamBackend`) and **depth-20** are separate sockets; Upstox mirrors this family (portfolio stream carries both orders and positions).
- First `subscribe_*` opens the socket on a daemon receive thread; `subscribe_orders(handler)` / `subscribe_quotes(keys, handler)` register handlers keyed by `id(handler)` (the caller must pass a stable bound callable — `MarketFeed` binds its callbacks once to avoid handler fan-out). `close()` is idempotent; the engine's kill switch and `session.stop()` are the only closers.
- Market tick → bus: `MarketFeed` (live only) tracks a wanted-instrument set, `subscribe_quotes` on the broker, and publishes `Quote`/`Depth` onto the session bus. Order rows → `LiveFillBridge` (§5.3).

**`health()`** is a zero-network lifecycle summary (configured/connected/authenticated/ready); **`verify_connection()`** is the wire probe. When the cached token is expired it first obtains a fresh one on demand (mint/refresh-capable managers), so verification is honest for TOTP/refresh brokers (Finding F1, fixed 2026-09-04).

---

## 4. The runtime object graph (who owns what)

| Component | Owns | Bound by |
|---|---|---|
| `ExecutionEngine` | `TradingCache` (orders/positions/quotes), `OrderManager`, `PositionManager`, applied-fill LRU (50k), `_cid_for_order` side table, kill switch, reconciliation | `startup._boot_tail` |
| `RiskManager` | rate window, rejection counter, day-start PnL baseline/peak | providers are the **cache** (`all_positions`, `get_quote`); cash optional |
| `MarkToMarketService` | the only writer of `Position.unrealized_pnl`/`mark_price`/`marked_at` | subscribed to bus `Quote` |
| Fill sources | the only mode seam | `BrokerFillSource(broker)` live; paper gets cache via `bind_cache` |
| `SQLiteOrderStore` | durable mirror + restart load (`load_into`) | bus `OrderPlaced/OrderFilled/OrderCancelled/OrderModified` mirror |
| `LiveFillBridge` | venue order rows → `OrderFilled` | bus `OrderPlaced` index (correlation → engine order id) |
| `MarketFeed` + `FeedRegistry` | one live quote/depth socket fan-out across WS clients | app.state per connection refcount |
| Bus | publish/subscribe + serialized live mode | `ErrorOccurred` → log + counter |

Threading: cache is reader-writer locked; positions lock per instrument; bus publishes serialized in live; engine idempotency guard and applied-fill LRU have their own locks; a live session is a single writer by lockfile.

---

## 5. The flows, end to end

### 5.1 Sync order submission (REST) — the canonical spine

`POST /orders` (or `/orders/bracket`):

1. **Route** (`interface/routes/orders.py`): `verify_api_key` dependency; `_require_idempotency_key` → missing/blank/oversized `Idempotency-Key` = 422. Builds the domain request (`OrderRequest` or `BracketOrderRequest` with side-aware SL<entry<target validated at construction) stamped with `correlation_id`.
2. **`engine.submit(request)` → `_run_pipeline(request, sync=True)`** (engine.py):
   - **0 kill switch** — checked before any reservation (a trip here never leaks a reserved cid).
   - **1 idempotency** — `guard.check_and_reserve(cid)`: a completed key **replays the original result** (no second broker call); a reserved-but-incomplete key returns `IdempotencyDuplicate`; a fresh key reserves. Order id unknown yet — the cid is remembered after the fill step in the `_cid_for_order` side table so `cancel()` can release it.
   - **2 risk** — `RiskManager.check(request)` (§6). Reject ⇒ OMS gets a REJECTED order, `OrderRejected` published, cid **released** (immediately reusable), receipt `status=REJECTED, message=risk_check_failed`.
   - **3 fill** — `fill_source.submit(request)`: live `BrokerFillSource` dispatches `BracketOrderRequest → broker.submit_super_order` else `broker.submit_order`, returns an ACK'd order (no fill yet). Boundary failures raise `OrderSubmissionUnknownError` (cid kept reserved — the venue may have accepted). Paper fill source fills immediately at the shared cache quote.
   - **4 OMS** — order registered (`OrderManager`), `cid → order_id` stamped, `OrderPlaced` published (this is the durability mirror hook). With a synchronous fill: position update via `PositionManager.on_fill` (skipped when the fill source owns projection), fee deduction, order → FILLED, fill fingerprint recorded **before** publishing, `OrderFilled` published, `guard.record_result(cid, order_id)`. ACK-only: `record_result` happens too — a client retry after a lost response can never submit a second live order.
3. Receipt → `OrderResponse`; a replay surfaces as `message="idempotency_replay"`.

Same spine from the reactive side: `OrderRequest` or `PlaceOrderCommand` on the bus → `_process_request` (fire-and-forget). There is exactly one order write path.

### 5.2 Bracket (super) orders

`POST /orders/bracket` preflights `caps.supports_super_order` (read-only ⇒ clean 422 before the engine for non-capable brokers like Upstox), then goes through the engine as a `BracketOrderRequest`:
- Submit → `BrokerFillSource.submit` → `submit_super_order` (entry + SL + target as one venue composite); a broker lacking the endpoint fails loudly (never a bare-entry degrade).
- The created `Order` keeps `target_price`/`stop_loss_price`/`trailing_jump` — **legs are the OMS marker** (`engine._is_bracket_order`). Both `fill_sources._make_order` and `engine._make_order` preserve them; `SQLiteOrderStore` persists them (schema + in-place `ALTER TABLE` migration) so a restarted bracket is still a bracket.
- `DELETE /orders/{id}` → `engine.cancel`: for a bracket, venue-first `cancel_super_order` (leg=ENTRY default on the wall), OMS → CANCELLED + `OrderCancelled` + cid release; plain orders keep OMS-local semantics (the engine kill switch does the venue plain-cancel). Kill switch skips its extra plain `fill.cancel` for brackets (no double venue cancel).
- `PUT /orders/{id}` → if the cached order is a bracket, the route builds a full-composite `BracketOrderRequest` (every omitted field defaults to the current order; ordering violations → 422) and `engine.modify` dispatches to `modify_super_order`, projecting the new prices onto the OMS record. A plain `OrderRequest` on a bracket is refused (never reaches `modify_order`).
- A recovered bracket (restart → `load_into`) keeps identity, so cancel/modify still hit the super endpoints — covered by restart tests for both cancel and modify.

### 5.3 Live fills — the async leg

`BrokerFillSource.submit` returns ACK only. The venue order-update WS pushes rows → `LiveFillBridge` (index built from `OrderPlaced`: correlation id → engine order id) → publishes `OrderFilled` → `engine._apply_fill`:
- Dedup fingerprint: `fill_id` when the venue provides one, else composite `(order, side, qty, price)` in a 50k LRU — the composite fallback is the documented C3 ambiguity (two equal-lot partials without trade ids are indistinguishable from a re-publish).
- Fills apply through the shared `position_math.apply_fill` (weighted avg, realized P&L, flip re-base) and fee deduction (₹20 brokerage cap honored across partial fills). Unknown orders get a minimal FILLED record so reconciliation sees them.

### 5.4 Marks and the risk gate

`MarkToMarketService.on_quote`: stale quotes ignored → `cache.update_quote` → the held position (if any) is re-marked **conservatively** (long at bid, short at ask; LTP fallback) → `Position(unrealized_pnl, mark_price, marked_at, mark_source)` → cache + `PositionUpdated`. Risk reads the same cache, so the daily-loss/drawdown gates now see **unrealized** P&L — the Phase-1 C2 fix. Live additionally fails closed: `require_fresh_marks` + `_increases_exposure` ⇒ every opening order needs the whole position book marked within `max_mark_age_seconds` (5 s default) *and* a fresh positive quote for the incoming instrument, or the order is denied.

### 5.5 Restart & reconciliation

Boot loads SQLite orders into the cache (legs intact), attaches the mirror, then for live pulls the venue order book + positions, runs `engine.reconcile`, and trip-kills on HIGH/CRITICAL drift — the session then stays NEW and orders are refused. Persisted idempotency keys survive restarts too (same SQLite file), so a post-restart retry of a completed order still replays rather than double-submits.

### 5.6 The client data plane

`/ws/stream` (one socket per browser connection): `subscribe` → `FeedRegistry.acquire()` → the shared `MarketFeed.subscribe` (one broker socket, fan-out) → `Quote` on the bus → per-connection frame writers, plus per-connection bar aggregators. `subscribe_orders` prefers the broker order backend, falling back to bus `OrderPlaced`. Depth is capability-bounded (`depth_levels`). Outbound queues drop-oldest under backpressure.

---

## 6. Findings & risks (mechanics layer)

- **F1 (real, brokers — FIXED 2026-09-04):** `BaseBroker.verify_connection()` returned `False` when the token manager's *cached* token was expired — even for mint-capable managers that would mint successfully on demand (proved live: Upstox quick-start reported failure, then passed after one `get_account` minted + persisted). Now, an expired token triggers an on-demand `ensure_token()` (mint/refresh) and the wire probe runs when it succeeds; the probe is skipped only when the manager cannot obtain a replacement or the mint fails. Regression tests: `brokers/tests/common/test_broker_contracts.py`.
- **F2 (operational, Dhan):** the Dhan LIVE connection is currently **not authenticated** — venue reachable (typed rejections), but the TOTP mint returns "Invalid TOTP" (`DHAN_TOTP_SECRET`/`PIN` mismatch or rotated seed) and the static `DHAN_ACCESS_TOKEN` is rejected at `/fundlimit`. Refresh credentials in `.env.local`. Upstox LIVE works end to end (auth probe + session boot; account probe returned a real balance).
- **F3 (pre-existing, amplified by brackets):** `engine.modify` dispatches the venue modify *before* the H5 risk re-check; a risk denial rolls back the OMS but the venue already changed — real-money cache/venue desync. For plain and composite modifies alike.
- **F4 (minor):** `modify_order` route calls `_require_idempotency_key` twice (function top + inside `try`).
- **F5 (awareness):** `connect()` is not an auth handshake — it loads instruments and flips a flag. A booted "connected" broker can still be unauthenticated; the first REST/WS action is where rejection surfaces. `verify_connection()`/startup reconcile are the honest probes, and the smoke script uses them.
- **F6 (config sensitivity):** the live writer lock resolves `Path("runtime/live")` relative to the current working directory (a sibling commit anchored the datalake root to the repo; the writer-lock path is a similar latent cwd hazard).

Phase-1 (C1 idempotency end-to-end, C2 MTM + fail-closed marks, brackets through the engine, cancel/modify + durable legs) is implemented and green in the working tree; the later phases of the plan (delete `trading/events/`, ports split, session drivers, replay isolation, wire envelope) remain gated future work.
