# TradeX v4 — Architecture Map & Flow Review

Generated from code inspection (not aspiration). Every arrow below was traced
through the source; the review section rates what the map shows.

---

## 1. System Map

Three packages, strict one-way dependency discipline (enforced by
`tests/test_import_boundaries.py`):

```
┌─────────────────────────────────────────────────────────────────────┐
│  trading/  (tradex_trading)          — application & runtime        │
│                                      depends on → brokers, domain   │
│  ┌───────────┐ ┌──────────┐ ┌─────────────┐ ┌─────────────────────┐ │
│  │ sdk       │ │ execution│ │ reactive    │ │ strategy            │ │
│  │ session   │ │ engine   │ │ ReactiveBus │ │ core + extensions   │ │
│  │ 4 services│ │ OMS/risk │ │ (RxPY)      │ │ (auto-discovered)   │ │
│  ├───────────┤ ├──────────┤ ├─────────────┤ ├─────────────────────┤ │
│  │ runtime   │ │ datalake │ │ replay      │ │ analytics/interface │ │
│  │ boot/feed │ │ parquet  │ │ walk-fwd    │ │ fastapi / cli       │ │
│  └───────────┘ └──────────┘ └─────────────┘ └─────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
                    │ depends on
┌─────────────────────────────────────────────────────────────────────┐
│  brokers/  (tradex_brokers)          — provider adapters            │
│  dhan/ upstox/ paper/  common/(base, resilience, provider_client,   │
│  transport, token_lifecycle, auth, instruments, ws_*)               │
└─────────────────────────────────────────────────────────────────────┘
                    │ depends on
┌─────────────────────────────────────────────────────────────────────┐
│  domain/  (tradex_domain)            — pure kernel                  │
│  value_objects · instruments · execution · market · options         │
│  events · errors · enums · protocols · capabilities(mechanism)      │
│  wire(InstrumentRegistry) · serialization                           │
└─────────────────────────────────────────────────────────────────────┘
```

**Composition root:** `runtime.startup.boot()` is the only place the object
graph is wired (`TradingSession.paper()/live()` are thin wrappers over it;
`boot_context()` adds a lifecycle handle). Fail-closed: boot errors prevent
session creation.

**Bus semantics:** `ReactiveBus` = RxPY Subject with synchronous causal drain
(nested publishes enqueue; order matches message log). Single-threaded by
design; live sessions wrap it in `ThreadSafeReactiveBus` (RLock-serialised
publish + bounded 10k log) because broker receive threads publish ticks.

---

## 2. Component Responsibilities

| Component | Owns | Must NOT own |
|---|---|---|
| `InstrumentRegistry` (domain.wire) | canonical↔provider keys, aliases, atomic snapshot reload | any I/O |
| `BrokerAdapter` protocol | the contract every broker satisfies | policy |
| `BaseBroker` | per-op gating, lifecycle, instrument master load | mapping (lives in clients) |
| Client mixins (`*_orders/_portfolio/_marketdata`) | payload building, row→domain mapping | resilience (delegated) |
| `ProviderHttpClient` | cache, auth inject, 401-once, status chain | retry/limits (pipeline's job) |
| `ResiliencePipeline` | rate-limit → breaker → safe-retry | business meaning |
| `TokenLifecycle*` | mint/refresh/broadcast, durable generations | trading concerns |
| `ExecutionEngine` | idempotency → risk → fill → OMS pipeline; event publication | provider I/O (fill source's job) |
| `FillSource` family | mode seam: Simulated/Paper/Broker/Replay | risk, persistence |
| `LiveFillBridge` | cumulative stream updates → delta Fills | order placement |
| `MarketFeed` | broker WS → bus bridge, refcounted feed registry | socket ownership (broker's) |
| Services (trade/portfolio/stream/scanner) | policy gates, capability loudness | composition (boot's job) |

---

## 3. Flows

### F1 — Live tick (event-driven)
```
Exchange WS ──► AutoReconnectMixin (fresh token, replay subs, backoff)
            ──► ws backend decode ──► MarketFeed._on_quote (bound-once handler)
            ──► ThreadSafeReactiveBus.publish(Quote)
            ──► StreamService subs · ReactiveStrategyEngine · FastAPI /ws/stream
```

### F2 — Order submit (imperative edge)
```
strategy ─► session.trade.submit(req)
             ├─ order gate (session policy)
             ├─ require_capability(order_type)
             └─► engine.submit ─► _run_pipeline(sync=True)
                   ├─ idempotency guard (correlation reserve)
                   ├─ risk manager
                   ├─ fill_source.submit
                   │    ├─ Broker: adapter ─► HTTP pipeline ─► venue (ACK)
                   │    ├─ Paper:  FillModel (tape/book fill, cash+position)
                   │    └─ Simulated(Backtest): next-bar-open reference fill
                   ├─ OMS cache update
                   └─ publish OrderPlaced (+OrderFilled if immediate)
CQRS alternative: strategy publishes PlaceOrderCommand on the bus; the engine
subscribes and runs the SAME pipeline.
```

### F3 — Modify / Cancel (imperative edge, event-complete)
```
trade.modify_order ─► gates ─► engine.modify
     ├─ cache lookup (reject unknown/terminal)
     ├─ fill_source.modify ─► venue
     ├─ OMS projection (price/qty/trigger/tif)
     └─ publish OrderModified
trade.cancel ─► engine.cancel ─► transition_to(CANCELLED) ─► publish OrderCancelled
```

### F4 — Live fills (event-driven)
```
broker order WS ─► backend receive thread
                ─► client._stream_order_from_row (registry resolve,
                   tradedPrice override on fill rows)
                ─► LiveFillBridge._on_order
                     ├─ correlation match to engine order id
                     ├─ delta = cumulant − applied (skip duplicates)
                     ├─ optional trade-book fill_id resolver (exact dedup)
                     └─ publish OrderFilled(delta @ traded price)
                ─► engine._apply_fill ─► PositionManager
Cross-thread: WS thread publishes through ThreadSafeReactiveBus lock.
```

### F5 — Backtest / Replay
```
datalake parquet ─► ParquetBacktestLoader / ParquetMarketProvider
                 ─► SimulatedFillSource(FillModel): next-bar-open reference
                 ─► same ExecutionEngine spine ─► golden parity tests
Replay differs from backtest only in market-source wiring inside boot().
```

### F6 — HTTP call anatomy (every REST op)
```
client mixin ─► ProviderHttpClient.request
   ├─ ReadCache lookup (safe reads; invalidated after writes)
   ├─ token inject (token_provider called per request)
   ├─ ResiliencePipeline.send
   │    ├─ MultiBucketRateLimiter.acquire (per-provider tables, env overrides)
   │    ├─ CircuitBreaker (CLOSED/OPEN/HALF_OPEN)
   │    └─ RetryableHttpClient (safe-method retries; 429 → bucket cooldown)
   ├─ status chain: 401/403 → one-shot token refresh; 429 → RateLimitError;
   │   5xx-on-mutation → UncertainSubmissionTracker + OrderSubmissionUnknownError
   └── urllib HttpTransport (or injected fetch in tests)
```

### F7 — Instrument master lifecycle
```
daily scheduler ─► ensure_master_fresh ─► download CSV/JSON
  ─► parse + build_instrument_from_row ─► registry.replace_all (atomic snapshot;
     readers never see partial state) ─► chains derived from master fallback
Intraday WS ids register as aliases; the master owns the primary key.
```

### F8 — Token lifecycle
```
MintTokenManager / PortTokenManager ─ ensure_token (generation-guarded)
  ├─ TOTP login flow (auth.py, cooldown-guarded) ─ durable mint slots
  ├─ TokenBroadcast → WS backends re-authenticate on refresh
  └─ 401-once: rejected_token → single refresh → exactly one retry (GET only)
```

### F9 — State machines
```
Session:  NEW ─► READY ─► STOPPED          (services gated on READY;
                                            start/stop idempotent)
Order:    NEW→PENDING→ACK→PARTIALLY_FILLED→FILLED/CANCELLED/REJECTED
          enforced by transition_to legal-transition table (+SUBMITTED legacy)
Breaker:  CLOSED ⇄ OPEN → HALF_OPEN → CLOSED
```

### F10 — Durability & single-writer (live)
```
boot(live) with cfg.persistence.path:
  ├─ acquire runtime/live/<broker>.writer.lock   (fail-closed; stale-pid clear)
  ├─ SQLiteOrderStore.load_into(engine.cache)    (restart resume)
  ├─ attach_order_persistence(bus, cache, store) — 5 lifecycle events mirror
  │   current cache state into SQLite at publish time
  └─ post-start: broker.get_orderbook() → engine.reconcile + status refresh
session.stop() ─► bus.dispose() detaches persister ─► writer lock released
### F11 — Chart UI (openalgo-charts frontend, render-only)
```
browser http://127.0.0.1:8000/ui/  (frontend/dist via FastAPI StaticFiles)
  ├─ history   GET /api/charts/history/{exch}:{sym} ─ datalake-first
  │             (ParquetStorage + candles_from_dataframe + resample),
  │             broker fallback; IST-naive → UTC seconds at the edge
  ├─ indicators chart.addIndicator('backend:*') ─ Tier-2 fetch contract →
  │             POST /api/charts/indicators/compute (backend registry only;
  │             zero formulas in JS; new registry entries appear automatically)
  ├─ live bars WS /ws/stream subscribe_bars → Quote bus → BarAggregator
  │             (never caches the forming bar) → bar frames
  ├─ replay    replay_start → SyntheticTickGenerator over datalake M1 →
  │             the SAME BarAggregator path as live (source-parity by design);
  │             window clamps to the lake's last day when now misses it
  ├─ trading   GET /api/charts/book → TradingController.setOrders/
  │             setPositions; writes go through /orders (one execution spine)
  └─ strategy  POST /api/charts/backtest (BacktestEngine, fills carry real
               prices) · POST /api/charts/scanner/run (ScannerEngine +
               ParquetMarketProvider, offline) · forms built from
               GET /api/charts/strategies metadata
Division of labor invariant: the backend computes; the chart renders.
```

---

## 4. Review

### Strengths (evidence-backed)
1. **Dependency discipline is real** — import-boundary tests fail CI on
   violations; the domain carries zero infrastructure imports.
2. **Single composition root honored** — paper/live both delegate to boot();
   no competing construction paths remain (factory removed).
3. **One reliability stack** — every production HTTP call traverses the same
   rate-limiter/breaker/retry pipeline; the unprotected variant is test-only.
4. **Complete event vocabulary** — placed/filled/rejected/cancelled/modified +
   CQRS command entry; fills are exactly-once into the OMS (fingerprint dedup).
5. **Atomic instrument reload** — snapshot-swap registry; tick threads never
   observe half-loaded masters.
6. **Parity evidenced at three levels** — golden cross-mode suites, scripted
   cross-provider contracts, recorded-tape live≡paper equality.
7. **Fail-closed capability system anchored to implementations** — drift fails CI.

### Residual risks / gaps (honest)
| # | Gap | Severity | Mitigation status |
|---|---|---|---|
| R1 | In-memory OMS/idempotency volatility | **Closed (opt-in)** | `cfg.persistence.path` wires SQLiteIdempotencyGuard + SQLiteOrderStore; lifecycle events mirror cache→store; boot restores pre-start and reconciles against broker book post-start |
| R2 | Per-process rate limiters | Medium → **Enforced** | live boot acquires a pid-lockfile (`SingleWriterLock`) — fail-closed naming the holder, stale-pid auto-clear, released on session.stop |
| R3 | Live-tape fixture breadth | Low-Medium | STOP trigger tapes (both providers), REJECT/CANCEL row contracts added; further scenarios incremental |
| R4 | Bar-open vs tick fill timing | Accepted | Golden tests pin the by-design difference |
| R5 | Dual submit doors | Low | Documented in engine.submit — both share one pipeline |
| R6 | Upstox partial heuristic | Low | Fixed + regression-tested: open+filled>0 → PARTIALLY_FILLED |
| R7 | Metrics optional | Closed | Boot already constructs MetricsRegistry and wires it into the engine |

### Scores (post-migration)
| Dimension | /10 | Basis |
|---|---:|---|
| Domain clarity | 9 | canonical types, complete event vocabulary |
| Abstraction quality | 8 | every layer earns its place; walls documented |
| Simplicity | 8 | net −147 LoC during migration; one pipeline/root/spine |
| Coupling | 8 | boundary tests; provider facts out of domain |
| Cohesion | 8 | single-purpose services; scoped mixins |
| Indirection | 7 | documented walls remain (deliberate) |
| Provider isolation | 9 | native IDs registry-confined; typed errors uniform |
| Testability | 9 | fetch/broker/boot seams; anchoring + tape suites |
| Lifecycle correctness | 8 | locks on lazy init; health surface; local-first verify |
| Concurrency safety | 8 | guarded lazy inits; TS bus; documented stream reasoning |
| Extensibility | 8 | new broker = package + capability table + boot map entry |
| Operational simplicity | 7 | single root; in-process limits; memory-first stores |

**Verdict:** the architecture is genuinely minimal and correct-by-construction
where it matters most (money movement, identity, provider isolation),
event-driven across the whole data plane and the full order lifecycle, with
parity between simulated modes proven by golden evidence and live parity
proven by recorded-tape replay through the same spine. The open risks
(R1/R2/R3) are operational-deployment decisions rather than design flaws.

---

*Maintained alongside the code; regenerate claims from source before citing.*