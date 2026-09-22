# TradeX v4 — Principal Architecture Review & Re-Architecture Plan

**Date:** 2026-09-04
**Reviewer lens:** Principal trading-systems architect; real-money Indian markets platform.
**Scope:** `domain/`, `trading/`, `brokers/`, `frontend/` — full stack.
**Companion to:** `docs/ARCHITECTURE.md` (system map), `docs/reviews/architecture-design-review-2026-08-29.md` (baseline). This review is independent and evidence-traced; where it disagrees with prior docs it says so.

**Verdict in one paragraph:** The core spine — `domain/` purity, `ExecutionEngine._run_pipeline`, shared `FillModel`/`position_math`, parity suites, import-boundary tests — is genuinely strong and should not be rewritten. The system's real problems are **proliferation, not deficiency**: a second, production-dead event-sourced OMS stack (`trading/events/`); a third order state machine living in the browser (`frontend/src/trade/order-engine.ts`) whose idempotency never reaches the backend; P&L that is computed in floats in the UI and is **never marked to market in the backend risk gate**; and replay/live parity that holds for market data but not for *trading*. Everything below follows from attacking those four things and deleting what loses.

---

## 1. System Audit & Gap Analysis

### 1.1 The four critical findings (trading-correctness)

#### C1 — Chart order path has **no end-to-end idempotency** 🔴

Evidence chain:
- `frontend/src/trade/order-engine.ts` issues a `clientToken` per order and correctly refuses to release it on transport failure (pessimistic, good).
- `frontend/src/trade-feed.ts` → `place()` strips it: the POST body is `{exchange, symbol, side, order_type, quantity, price, trigger_price}` — **no `clientToken`, no `correlation_id`**.
- `trading/interface/routes/orders.py::_build_order_request` builds `OrderRequest` without `correlation_id`.
- `execution/engine.py::_run_pipeline`: `if self._guard is not None and cid is not None:` — **the idempotency guard is skipped entirely when no cid arrives.**

Consequence: a retry caused by a 504, socket reset, or double-fired fetch can **place the same real-money order twice**. The browser's token only dedupes within one JS heap. This violates the system's own design intent (the `MemoryIdempotencyGuard`/`SQLiteIdempotencyGuard` exist and are excellent — orders just never carry keys to them).

**Fix (Phase 1, mandatory):** `POST /orders` requires an `Idempotency-Key` header (or `client_token` body field) → routes layer constructs `OrderRequest(correlation_id=...)` → existing guard engages. Reject 422 when absent. The frontend change is one line (`trade-feed.ts` forwards the token).

#### C2 — Live/paper risk gate never sees unrealized PnL 🔴

Evidence chain:
- `position_math.apply_fill` sets `unrealized_pnl=Money(0)  # updated when quotes arrive`.
- No code anywhere marks positions to market on a quote (verified: the only writers of `unrealized_pnl` carry forward the old value or set zero; only `BacktestEngine._mark_to_market` computes MTM, and it is backtest-only reporting).
- `RiskManager._equity_pnl()` (daily-loss + drawdown guards) sums `realized + unrealized` from the position book.

Consequence: in live/paper, `_equity_pnl()` == realized-only PnL. A position deeply underwater but not yet closed shows **zero loss** to the daily-loss guard and the drawdown guard. The guards only trip after you realize the loss — which is exactly the failure mode a daily-loss limit exists to prevent.

**Fix (Phase 1, mandatory):** a small `MarkToMarketService` subscribed to `Quote` on the bus updates cached positions' `unrealized_pnl` through `PositionManager` (Decimal, q2-quantized). Backtest parity already exists via `_mark_to_market`; move that function into `position_math.py` so all modes share one MTM function.

#### C3 — Fill dedup falls back to an ambiguous fingerprint when the venue omits `fill_id` 🟠

`engine._record_applied_fill`: without a venue trade id the key is `(order_id, side, qty, price)`. Two genuine equal-lot partial fills at the same price (plausible on liquid F&O) are indistinguishable from a re-publish; one is silently dropped — a lost fill means wrong avg price, wrong PnL, wrong position. The code documents this; "documented" is not "fixed."

**Fix (Phase 2):** make `fill_id` contractually required at the adapter boundary (`Fill.__post_init__` warning now, adapter contract test failing CI next). Where a venue truly provides no trade id, synthesize a monotonic id in the adapter's order-stream decoder, not in the engine.

#### C4 — Replay does not fork *logic* but forks *session semantics* 🟠

Three "replays" exist:
1. **Frontend bar-replay** (`main.ts` + `ReplayController`): prefix-slices bars already in the chart. Pure visualization — fine.
2. **Backend tick-replay** (`replay_start` → `SyntheticTickGenerator` → `BarAggregator` → same WS path as live): market-data replay is properly source-parity by design — good.
3. **Backtest** (`replay/backtest.py`) — a full trading simulation with its own equity curve, corporate actions, and risk.

The gap: during (1) and (2) the **trading session stays live**. `POST /orders` during bar-replay executes against *current* session state while the chart shows historical bars; during tick-replay, orders fill in *wall-clock* pipeline time against *replayed* market data. There is no replay-scoped `TradingSession`, no replay-mode fill model bound to the replay clock in the reactive path, and no UI indication that order semantics changed. The synthetic-tick generator's own docstring says it is "reference-grade simulation, not production data" — trading against it through the live spine is unguarded.

**Fix (Phase 3):** make mode a **session-level fact, not a client pill**: `SessionConfig.mode` gates order submission (reject or sandbox-fill when `mode != live|paper`), and tick-replay boots a replay-scoped session via the existing `boot()` composition root rather than mutating a live one.

### 1.2 Duplicate logic (the full inventory)

| # | Duplicate | Locations | Keep | Delete/merge |
|---|---|---|---|---|
| D1 | **Entire second OMS stack**: FSM, risk engine, order store, fill matcher, recovery, session | `trading/events/*` (store, actor, processor, projectors, order_fsm, risk_engine, fill_matcher, recovery, session, data_source ≈ 2.7k LoC + ~1.2k test LoC) vs `trading/execution/*` + `sdk/session.py` | `execution/` stack (parity-proven, production-wired) | **Delete `trading/events/`**; port its two genuinely superior ideas — durable append-only `EventStore` and crash recovery — into `execution/` (see §6.2). Production imports are only `trading/scripts/probe_review_fixes.py` + its own tests; no runtime route touches it. |
| D2 | **Third order state machine in the browser** | `frontend/src/trade/order-state-machine.ts` + `order-engine.ts` (intent FSM, 539 LoC) | backend `Order.transition_to` table as the *only* authority | Demote the frontend machine to an **intent layer** that never claims order state (see §5). |
| D3 | PnL math computed in floats in UI | `frontend/src/trade/pnl.ts` (`unrealizedPnl`, `breakeven`, `riskReward`) vs backend `position_math` + (missing) MTM | backend computation | UI keeps only *display formatting*; PnL values arrive server-computed (needs C2 fixed first). |
| D4 | Pre-trade validation in browser | `frontend/src/trade/validation.ts` (tick/band/lot/freeze) vs broker RMS (authoritative) | backend validation (new, see §6.4); broker RMS | Keep a **minimal** client snap-to-tick for UX, delete the rest after backend enforcement exists. The file itself admits: "ADVISORY ONLY… the production terminal calls `trade.place` directly and never constructs `OrderEngine`." |
| D5 | Two status vocabularies | domain `OrderStatus` (8 members) ↔ chart union (`pending/working/partial/filled/cancelled/rejected`) + `stale` + intent states | domain enum | Map **once** in one wire module (§7); delete `UNKNOWN→rejected` mapping (send `unknown` explicitly). |
| D6 | Order creation from request | `engine._make_order`, `fill_sources._make_order`, `events/actor` (its own) | `fill_sources._make_order` | One `OrderFactory.from_request()` in domain or execution. |
| D7 | Two submit doors (imperative + CQRS command) | `engine.submit` and `PlaceOrderCommand` subscription | keep both but document as one pipeline (already done, R5) | Acceptable; add a lint-time check that no new entry points appear. |
| D8 | Two replay transports (bar vs tick) | `main.ts` ReplayController vs WS `replay_*` | tick-replay (backend-owned) | Bar-replay stays as a *chart* feature but must be renamed "chart scrub" so no one mistakes it for a trading mode. |
| D9 | Bucketing/resampling logic | `domain/market._bucketize`, `runtime/bar_aggregator.bucket_start`, datalake `candles_from_dataframe` resample, frontend `registerInterval` bucketing | one domain bucketing module | §2.4. |

### 1.3 Dead / vestigial code (delete list)

- `trading/events/` — entire package (D1). Highest-value deletion in the repo.
- `OrderStatus.UNKNOWN` + `SUBMITTED` as *live* transitions: `SUBMITTED` exists only as `OrderReceipt`'s default and one adapter's ACK placeholder; `UNKNOWN` exists only for malformed broker rows. Replace with explicit `OrderReceipt(status=order.status)` and a domain-level `UNKNOWN` that can only enter via reconciliation, never via submission. (The current `_LEGAL_TRANSITIONS[SUBMITTED]` table that omits `REJECTED` is a trap.)
- `frontend/backend-indicators.ts` module-level `fallbackSymbol/fallbackExchange/fallbackInterval` — self-described "migration shim, not the source of truth." Pass context explicitly; delete the globals.
- Tier-2 indicator `REFRESH_MS = 15_000` polling shim — replace with a push subscription on `/ws/stream` (`indicator_update` frame) once the stream carries a generic topic channel; delete the poller.
- `scanner.py` snapshot fallback uses `datetime.now(UTC)` → nondeterministic inside replay/backtests. Restrict `ScannerEngine` to explicit windows; delete the wall-clock path.
- `frontend/src/main.ts` (1,003 LoC): replay transport, control socket, boot sequence, chart settings, logs — split along §5 boundaries; the file is where parity of *controls* currently goes to die.
- `HistoricalSeries.__iter__ -> object` + `# type: ignore` — make it `Iterator[Candle]`.
- `trading/scripts/probe_review_fixes.py` — a probe script importing the dying stack; delete with D1.
- `market_calendar` duplication: `runtime/calendar.py` re-exports domain constants (fine) but datalake has its own session-grid comments; single-source the session grid (§2.4).

### 1.4 SOLID / SoC / SSOT violations (beyond the above)

- **SSOT:** order state has three homes (backend cache, broker stream, browser FSM); position PnL has three homes (backend Decimal, backtest float curve, browser floats). C1/C2/D3 close this.
- **OCP:** `interface/routes/chart.py` hand-maps order types (`STOP_LOSS_LIMIT`→`SL`…) inline; adding an order type means editing a route file. Mapping belongs in one wire adapter module with an exhaustive-match test.
- **ISP:** `BrokerAdapter` is a fat protocol (lifecycle + orders + portfolio + market data + options in one). Capabilities gate features but every adapter must *implement* everything. Split into `OrderPort`, `MarketDataPort`, `PortfolioPort`, `InstrumentPort` composed by an adapter (mechanical, low risk, Phase 3).
- **DIP:** `RiskManager` binds to `positions_provider`/`price_provider`/`cash_provider` as `Any` callables. Fine at this scale, but the `getattr` probing (`getattr(fill, "position_projection_owned", False)`) is stringly-typed seams. Formalize `FillSource` metadata as protocol members.
- **LSP:** `PaperBroker.owns_position_projection` flips engine behavior by attribute sniffing — a subclass contract expressed as a duck-typing flag. Move to the `FillSource` protocol explicitly.

### 1.5 Live vs replay divergence ledger (current, honest)

| Concern | Backtest | Tick-replay | Bar-replay | Live/Paper |
|---|---|---|---|---|
| Market data → bars | datalake parquet | SyntheticTickGenerator → BarAggregator (same aggregator ✅) | prefix slice in browser | broker WS → BarAggregator (same ✅) |
| Fill timing | next-bar-open (`next_open`) — **but** recording-only strategies fill at bar close (legacy bridge, M3 in code) | wall-clock pipeline | n/a | wall-clock pipeline |
| Risk | shared `RiskManager` (deterministic `now`) | **none** (orders hit live session) | n/a | shared `RiskManager` |
| Corporate actions | yes (`CorporateActionStore`) | no | no | live positions only |
| Order idempotency | engine guard when cid present | **none (C1)** | n/a | **none (C1)** |
| Session state | fresh per run | live session reused ⚠️ | none | durable |

The `next_open` vs `signal_close` fill modes and the `claimed/bridged` double-bridge in `BacktestEngine.run` are the other parity soft spots: two fill-timing models inside one mode. Consolidate to one (next-open) and delete the recording-only bridge in Phase 2 after migrating the two strategies that use it.

---

## 2. Domain Model Re-Design

The domain kernel is already close to right. Required changes, not rewrites:

### 2.1 Candle & time — one clock, one grid
- **Rule:** `Candle.timestamp` is **UTC, tz-aware** everywhere. IST-naive timestamps exist only at two boundaries: the datalake storage contract and the broker ingest. Convert at those edges, nowhere else.
- Add to `tradex_domain`:
  - `SessionGrid` (exchange → open/close/pre/post, half-day support) — single source for `BarAggregator`, datalake resample, `NSETradingCalendar`, and the Dhan phantom-bar filter.
  - `bucketize(candles, timeframe, grid)` used by `HistoricalSeries.resample` AND the datalake — so a live-aggregated D1 bar and a historical D1 bar share boundaries. Today `_bucketize` uses UTC-epoch days while datalake daily candles are IST-dated: **live and historical daily bars can disagree by 5h30m**. Intraday bars are unaffected but the same function must serve both.
- Kill the `ZoneInfo("Asia/Kolkata")` hardcode inside `BarAggregator._emit`; the grid carries tz.

### 2.2 Indicator — stateful, incremental, contract-only
`IndicatorComputer` exists as a protocol; formalize it:
```python
class IncrementalIndicator(Protocol, Protocol[T_in, T_out]):
    warmup: int
    def update(self, bar: Candle) -> T_out | None: ...   # None until warm
    def snapshot(self) -> bytes: ...                      # serialize state
    @classmethod
    def restore(cls, state: bytes) -> "IncrementalIndicator": ...
```
- `snapshot/restore` is what makes replay time-travel cheap (checkpoint per session minute, rewind = restore) and what lets the live stream hand a warm indicator to a replay.
- No per-candle recomputation from scratch: `analytics/` engines must all be `update()`-style. Audit `analytics/engine.py` during Phase 2; anything calling full-series compute per bar becomes O(n²) on live streams.
- **No UI fields.** Catalog metadata (plots, colors) stays in the registry/wire layer, not the computation object.

### 2.3 Signal / Trade / Position / Order — deltas
- `Signal` is fine (frozen, versioned metadata). One change: `strength: float` is doing double duty as "directional qty" in the strategy engine (`abs(result.strength) → quantity`). Replace with `quantity: Quantity | None` + `confidence: float`; using |strength| as size is a hidden unit bug waiting for a strategy that returns 0.5.
- `Trade` does not exist as a first-class object (trades are floats in `BacktestResult.fills` dicts). Add a frozen `Trade` (entry fill, exit fill, pnl, fees, reason) produced by one `TradeAssembler` consumed by backtest, replay, and the UI markers.
- `Position`: add `unrealized_pnl` semantics to the type — a position without a mark is *stale*, so introduce `marked_at: datetime | None` and make `unrealized_pnl` optional. This makes C2 impossible to reintroduce silently (risk gate refuses `marked_at is None`).
- `Order`: already excellent (FSM via `transition_to`). Remove `SUBMITTED`/`UNKNOWN` from submission paths (§1.3).

### 2.4 Risk parameters
One frozen `RiskPolicy` dataclass (max_order_value, max_position_value, per-minute rate, daily_loss, max_drawdown, allowed_instruments, per-strategy `RiskBudget`s) — the *same* object parsed from config for backtest, replay, paper, live. Today `RiskManager` (mutable, provider-injecting) and `events/risk_engine.RiskConfig` (pure) split the same concerns; after D1 deletion there is one, and `check()` becomes `(policy, portfolio_view, order) → Decision` with the mutable rate-window isolated in a `RateLimiter` class.

### 2.5 Session / Trading Day
Formalize what `master_lifecycle` + `calendar.py` + writer-lock imply:
```
TradingDay: date, grid, holidays
TradingSessionState: NEW → RECOVERING → READY → HALTED(kill-switch) → SETTLING → CLOSED
```
`boot()` gains `trading_day: TradingDay` and refuses to start a live session outside the grid (advisory warning, hard gate for live *orders* via the risk gate). Kill-switch transition becomes a persisted session event (the events/ stack already had the right idea here — port it).

---

## 3. Architecture Re-Design (target state)

Hexagonal, three packages (unchanged) + one frontend, with ports made explicit:

```
                        ┌────────────────────────────────────────────┐
   frontend/            │  PRESENTATION (pure consumer)              │
   (browser)            │  chart rendering · replay scrub UI ·        │
                        │  order intent capture · no state authority  │
                        └──────────────┬─────────────────────────────┘
                                       │ REST + WS, versioned wire (§7)
                        ───────────────┼──────────────────────────────
                        ┌──────────────▼─────────────────────────────┐
   trading/             │  APPLICATION                               │
                        │  boot() composition root · FastAPI routes   │
                        │  SessionService (mode gate, lifecycle)      │
                        └──┬───────────┬───────────┬─────────────────┘
                           │           │           │
             ┌─────────────▼──┐ ┌──────▼──────┐ ┌──▼──────────────┐
             │ EXECUTION      │ │ STRATEGY    │ │ ANALYTICS       │
             │ OMS · risk ·   │ │ Reactive-   │ │ indicators ·    │
             │ fills · fees · │ │ Strategy-   │ │ profiles ·      │
             │ MTM · ledger   │ │ Engine ·    │ │ transforms      │
             │                │ │ Scanner     │ │                 │
             └──────┬─────────┘ └──────┬──────┘ └──┬──────────────┘
                    │                  │           │
                 ┌──▼──────────────────▼───────────▼───┐
                 │ DOMAIN (tradex_domain) — pure kernel │
                 │ Candle·Quote·Order·Fill·Position·    │
                 │ Signal·RiskPolicy·SessionGrid·ports  │
                 └─────────────────────────────────────┘
                    ▲ Adapter pattern (ports & adapters)
                 ┌──┴───────────────────────────────────┐
   brokers/      │ INFRASTRUCTURE                        │
                 │ Dhan/Upstox/Paper adapters · WS ·     │
                 │ resilience pipeline · token lifecycle │
                 │ datalake (parquet) · event store      │
                 └───────────────────────────────────────┘
```

### What runs where — the non-negotiable table

| Concern | Frontend | Backend | Shared (domain) |
|---|---|---|---|
| Bar aggregation | ❌ forbidden | ✅ BarAggregator | bucket math |
| Indicator computation | ❌ forbidden (Tier-2 fetch only) | ✅ registry | indicator *contract* |
| **Order validation** | snap-to-tick only (UX) | ✅ authoritative pre-broker gate + RMS | constraint *types* |
| **Order state** | intent only (`SENT/AMBIGUOUS`) | ✅ FSM | status enum |
| **PnL / fees / breakeven** | display formatting | ✅ Decimal computation | Money/Price/Quantity |
| Risk enforcement | ❌ forbidden | ✅ RiskPolicy gate | RiskPolicy type |
| Replay clock | play/pause/seek *requests* | ✅ owns time | — |
| Session mode | pill reflects server state | ✅ authority | — |

**Strictly forbidden in the frontend:** any money arithmetic beyond formatting, any state machine that claims broker truth, any validation that can *allow* anything, any clock used for trading decisions, any retry that can re-send a mutation without the server deduping it.

---

## 4. Trading Pipeline Re-Architecture

Target — one deterministic pipeline, one driver, four clocks:

```
            ┌────────────────────────── SessionDriver (per mode) ──────────────────────┐
            │                                                                          │
 Data ──► Preprocessing ──► Indicators ──► Strategy ──► Risk ──► Orders ──► Execution ──► State
 (ticks/  (SessionGrid     (Incremental  (Strategy.on_  (RiskPolicy  (OrderFactory, (FillModel,
 bars)     bucketing,       Indicator,    bar/quote,     gate,         idempotency,    fill source
           gap filter)      snapshot/     Signal)        budgets)      FSM)            per mode)
                            restore)                                                   │
                                                                                       ▼
                                                                              EventStore (append-only)
                                                                                       │
                                                                              Projectors → Read models
                                                                              (orders, positions, PnL, UI)
```

Rules (each auditable in code today):

1. **One event vocabulary.** `Candle | Quote | Depth | OrderPlaced | OrderFilled | OrderCancelled | OrderModified | OrderRejected | PositionUpdated | SessionEvent` on `ReactiveBus`. Backtest publishes the same objects live does (already true — keep it).
2. **The driver is the only thing that differs.** `SessionDriver` has four implementations — `LiveDriver(broker_ws)`, `PaperDriver(broker=PaperBroker)`, `ReplayDriver(recorded_ticks, clock=ReplayClock)`, `BacktestDriver(parquet, clock=FakeClock)` — differing **only** in data source and clock. Today this is ~80% true; the deltas are: tick-replay bypasses session scoping (C4), backtest keeps its equity/CA loop inside `run()` (acceptable: that is reporting, not logic — move `_mark_to_market` into domain so the risk gate shares it, per C2).
3. **Replay time travel without forking.** Replay = same pipeline + `ReplayClock` + checkpointed indicator state (§2.2 `snapshot/restore`). Seek backwards = restore last checkpoint ≤ target bar, replay forward. No second engine. Controls (play/pause/speed/seek) are *commands to the driver*, never client-side bar surgery on live state.
4. **Incremental indicators.** §2.2; `warmup` + `update` only. Full-series compute exists for scanner/batch contexts, never in the live loop.
5. **Determinism.** All `now()` inside the pipeline comes from the injected `Clock`. Audit and remove the remaining wall-clock reads in the reactive path (the `Fill` default `datetime.now(UTC)` and `RiskManager` default are the two known; the first is already mitigated by `reference_timestamp` — make it mandatory in Phase 2).
6. **Order write path.** Client intent → `POST /orders` with `Idempotency-Key` → risk → fill source (per mode) → FSM → event store → projector → WS push. Exactly one path (C1 fix lands here).

---

## 5. Frontend Refactor Plan (pure consumer)

Current state is better than most trading UIs but still owns too much. Plan, in dependency order:

**F1. Kill the client-side write authority (Phase 3).**
`order-engine.ts` (539 LoC + 442 LoC tests) collapses to an `OrderIntentController`: capture intent (symbol/side/qty/type/price), one optimistic row (`SENT`/`AMBIGUOUS`/`SETTLED` — 3 states, not 9), server dedupes via idempotency key. OCO linking moves to the backend as a bracket/OCO order *type* (the backend already has `submit_super_order`; expose it over one endpoint instead of client-side peer-cancellation). The drag-modify rate limiter (150 ms coalescing) is genuine UI concern and stays.

**F2. Server-computed PnL (Phase 3, after C2).**
`/api/charts/book` and WS `position` frames carry `unrealized_pnl`, `marked_at`. `pnl.ts` shrinks to formatting; `riskReward`/`bracketValid` (pure, harmless) may stay as display helpers.

**F3. One state store, one stream (Phase 4).**
`main.ts` (1,003 LoC) splits into: `store.ts` (single observable app state: symbol/interval/mode/replay-position), `streams.ts` (WS multiplex — today three separate `WebSocket` wrappers exist: `feed.ts WsBarHub`, `trade-feed.ts TradeWsHub`, `main.ts ControlSocket`; they must share one socket + one reconnect policy + one auth story), `panels/*` (dumb renderers). State flows one way: stream → store → components.

**F4. Replay controls control time, nothing else (Phase 3).**
`ReplayController` remains a chart *scrubbing* feature; add a prominent mode ribbon from server session state (`LIVE / REPLAY / TICK-REPLAY / BACKTEST`), disable order buttons in modes the server rejects (defense in depth — the server still gates).

**F5. Delete the legacy shims (Phase 2).**
`fallbackSymbol/Exchange/Interval` globals; 15 s indicator polling → `subscribe` on the stream's `indicator_update` topic; the `setIndicatorContext` API.

**What the frontend keeps:** everything visual (`pill.ts`, primitives, profiles renderers, bracket drawing, draw rail), keyboard shortcuts, URL/localStorage persistence, optimistic *intent* rows, chart scrub. The existing discipline in `transforms.ts`/`profiles.ts`/`backend-indicators.ts` ("backend computes, chart renders") is the model for the trade tier, not the exception.

---

## 6. Backend Refactor Plan

**B1. Delete `trading/events/` (Phase 2 — the big one).**
It is a parallel OMS universe (own FSM, risk, store, recovery, kill switch with *different* semantics — its kill switch doesn't cancel at broker, the engine's does). Nothing in production imports it. **Port two things first:** (a) `EventStore` (SQLite append-only, indexed by session) becomes `execution/event_log.py` — the durability layer `boot()` attaches to the engine's lifecycle events alongside the existing `SQLiteOrderStore`; (b) `SessionRecovery`'s replay-project-then-resume flow becomes `execution/recovery.py` driven off that log. Then `git rm -r trading/events/ trading/tests/events/` (−≈3.9k LoC) and delete `probe_review_fixes.py`.

**B2. Broker-agnostic OMS — finish the job (Phase 3).**
`ExecutionEngine` is broker-agnostic; the seams that leak are: `BrokerFillSource`'s attribute sniffing (formalize into the `FillSource` protocol), bracket orders routing directly to `session.broker.submit_super_order` from the route layer (bypassing idempotency/risk/OMS — **route it through the engine pipeline as a composite order**, otherwise brackets are the one order type with no risk check and no dedup), and `_map_order_type` living in a route file (move to wire module, exhaustive test).

**B3. Adapter pattern — split the fat protocol (Phase 3).**
`BrokerAdapter` → `OrderPort` + `MarketDataPort` + `PortfolioPort` + `InstrumentPort`; adapters compose them. `BrokerCapabilities` continues to gate. Contract tests (§8) bind to ports so a new broker (Angel, Fyers) is: ports + capability table + boot map entry + recorded-tape fixture.

**B4. Deterministic simulation engine — shared by backtest AND replay (Phase 2).**
Extract from `BacktestEngine.run()` the parts that are not backtest-specific: next-open deferral (already in `ReactiveStrategyEngine`), CA application (already shared via `PositionManager.on_corporate_action`), MTM (move to `position_math`), trade assembly (new `TradeAssembler`). What remains in `BacktestEngine` is: data loading, equity curve, metrics. A `ReplayTradingDriver` then gets trading-in-replay for free with identical semantics — closing C4.

**B5. Lifecycle per trading day (Phase 3).**
`boot(config, trading_day)` wires: day-scoped event log file (`runtime/<day>/<mode>.events.sqlite`), rate-window reset, day-start PnL baseline (fixes `_session_date` roll-forward edge where a session crosses midnight), kill-switch persistence, and EOD settle (cancel-or-carry policy as config). Writer lock becomes per-day.

**B6. Risk engine hardening (Phase 1-2).**
C2 (MTM into the gate), explicit `RiskPolicy` object (§2.4), and one rule change: **fail-closed when a provider is missing in live mode** — today `positions_provider=None` silently disables the position-value and daily-loss gates; in `mode=live` boot must *require* them wired.

---

## 7. API & Contract Design

**Envelope (all REST + WS):**
```json
{ "schema": "tradex.v1", "...": "payload" }
```
- `schema` on every response and every WS frame; server rejects/400s unknown major versions, accepts `v1.x` minors. One constant, one test.
- Naming: snake_case everywhere. `/api/charts/book` is the lone camelCase offender — version it (`/api/charts/v1/book`) with camel→snake done server-side once, then migrate the frontend and delete the shim.

**Event schemas (WS `/ws/stream`, all `tradex.v1`):**

| type | payload | producer |
|---|---|---|
| `bar` | `{instrument, interval, time, o,h,l,c, volume, closed}` | BarAggregator |
| `quote` / `depth` | domain Quote/Depth wire form | MarketFeed |
| `order` | full domain Order wire form (id, status, filled_qty, prices, tag, strategy version) | OMS events |
| `fill` | `{order_id, fill_id, qty, price, ts}` | OMS events |
| `position` | `{instrument, net_qty, avg_price, realized, unrealized, marked_at}` | PositionUpdated |
| `pnl` | `{realized, unrealized, fees, equity}` — 1 Hz throttle | MTM service |
| `signal` | Signal wire form (strategy, version, reason) | strategy engine |
| `replay_*` | existing acks + `{replay_position, total}` | ReplayDriver |
| `indicator_update` | `{instance_id, points}` | analytics |
| `error` | `{code, message, correlation_id}` | boundary |

- **Order status vocabulary:** the wire carries the **domain enum verbatim** (`NEW/PENDING/ACK/PARTIALLY_FILLED/FILLED/CANCELLED/REJECTED/UNKNOWN`). The chart-tier mapping (`map_order_status`) is a frontend concern consuming domain values — one mapping, tested, in one file. `UNKNOWN` renders as its own badge (lossy `→rejected` removed).
- **Idempotency:** `POST /orders`, `PUT /orders/{id}`, `DELETE /orders/{id}`, `POST /orders/bracket` all accept `Idempotency-Key`; replays return the original receipt with `Idempotent-Replay: true`.
- **Streaming parity rule:** replay and live produce **byte-identical frame sequences** for the same underlying data (bar/order/fill), differing only in `replay_*` control frames and the session banner. This is testable (§8) and is the definition of "no forked logic."

---

## 8. Test Strategy

Existing assets to keep: golden mode-parity suite, `parity/` tapes, `contracts/` adapter protocol tests, import-boundary tests, frontend vitest + Playwright. Gaps and additions:

| Layer | Tests | Status → Action |
|---|---|---|
| **Unit (domain)** | FSM transitions, value objects, bucketize w/ SessionGrid, Money/Decimal rounding | good → add SessionGrid boundary cases (D1 IST vs UTC from §2.1) |
| **Unit (indicators)** | incremental vs batch equivalence: `sum(update(i)) == compute(series)` for every registry entry | **new, mandatory** — this is the O(n) guarantee; property-based over random walks |
| **Property-based** | `position_math`: for any fill sequence, Σ realized + MTM − fees ≡ equity delta; no negative abs exposure; flips re-base avg correctly; fee cap ≤ brokerage cap. Hypothesis not currently a dep — add it | **new** (P1 of roadmap) |
| **Parity: backtest ≡ replay ≡ paper** | one recorded tape through all three drivers → identical fills, positions, PnL (Decimal-exact) | exists → extend tape with: partial fills, equal-lot dupes (C3), flip, CA on ex-date |
| **Parity: replay ≡ live frames** | WS frame recorder: live session frames vs replay of same data → identical sequences modulo control frames (§7 rule) | **new** |
| **Contract: adapters** | ports satisfy protocol; capability table matches implementation; recorded-tape STOP/REJECT/CANCEL rows | exists → add idempotency-key round trip per adapter |
| **Contract: wire** | every REST/WS payload validates against schema v1 fixtures; unknown-major rejected; camelCase shim removal gated | **new** |
| **E2E simulation** | Playwright: place → drag-modify → OCO via bracket endpoint → kill switch → reconcile; replay scrub with orders disabled | exists partially → add server-side dedup case (double POST same key = one order) |
| **Regression (logic drift)** | golden files for fee math, bucket boundaries, status mapping; CI parity workflow | exists → add "no wall-clock in pipeline" lint (rg for `datetime.now` outside clock seams) |

**Mandatory new test: idempotency end-to-end** (C1) — frontend test asserting `clientToken` reaches the wire; backend test asserting duplicate key ⇒ one order; integration test asserting transport-error retry with same key ⇒ one order. This single test chain is the highest-leverage addition in the repo.

---

## 9. Refactoring Roadmap

### Phase 0 — Freeze & instrument (½ week)
- **Change:** add CI gates that make the plan safe: import-boundary, parity workflow, `datetime.now` lint in `execution/`+`strategy/`, frontend typecheck in CI.
- **Delete:** nothing yet.
- **Don't touch:** execution engine, position math, fill model.
- **Validate:** full test suite green on main; baseline coverage recorded.

### Phase 1 — Safety (money correctness) (1 week)
- **Change:** C1 idempotency end-to-end (routes + feed + guard); C2 MTM service + `marked_at`; brackets through the engine pipeline (B2); fail-closed missing risk providers in live (B6); C4 session-mode gate on `POST /orders`.
- **Delete:** nothing.
- **Don't touch:** domain types, broker adapters.
- **Validate:** new E2E dedup test, parity suite unchanged (Decimal-exact), risk-gate unit tests with simulated MTM.

### Phase 2 — Domain cleanup (1–2 weeks)
- **Change:** delete `trading/events/` after porting EventLog+Recovery (B1); `SUBMITTED`/`UNKNOWN` demoted from submission paths; `Signal.quantity`; `Trade` + `TradeAssembler`; MTM into `position_math`; SessionGrid + unified bucketize; fill_id required at adapter boundary (C3); recording-only backtest bridge retired (migrate the two strategies to returning signals).
- **Delete:** ≈4k LoC (D1 list + §1.3).
- **Don't touch:** frontend, REST contracts (frontend ships after).
- **Validate:** LOC delta negative; all parity goldens byte-stable; indicator incremental-vs-batch property test green.

### Phase 3 — Architecture realignment (2–3 weeks)
- **Change:** `SessionDriver` abstraction with four drivers (B4, C4); ports split of `BrokerAdapter` (B3); day-scoped lifecycle + persisted kill switch (B5); wire envelope `tradex.v1` + `/api/charts/v1/*` versioning (§7); OCO/bracket as backend order type; frontend OCO peer-cancel retired.
- **Delete:** route-level order-type mapping, `events/` leftovers, dual WS socket wrappers (frontend F3 partially).
- **Don't touch:** fill math, fee math.
- **Validate:** replay-vs-live frame parity test; adapter contract tests for all three adapters; openapi diff reviewed.

### Phase 4 — Frontend/backend separation (2 weeks)
- **Change:** F1 intent controller (−~700 LoC frontend), F2 server PnL in book/WS, F3 store/streams split of `main.ts`, F5 shim deletions, `/api/charts/book` snake_case migration.
- **Delete:** `order-state-machine.ts` (→3-state intent), `pnl.ts` money math, `validation.ts` beyond tick-snap, indicator polling.
- **Don't touch:** chart library integration, profile/transform renderers.
- **Validate:** Playwright E2E suite extended with dedup + replay scenarios; vitest suite rebuilt around intent controller; bundle size delta recorded.

### Phase 5 — Performance & observability (1–2 weeks)
- **Change:** bus split by criticality (order pipeline synchronous; market data observable-offload — the prior review's G1, still open); metrics: fill latency, bus depth, dedup evictions (exists), MTM staleness, WS queue drops; structured logs with `correlation_id` end-to-end (strategy → order → fill → position); session replay debug bundle (event log + tape = deterministic reproduction of any live incident).
- **Delete:** `deque(maxlen=10_000)` in-memory log as primary durability (superseded by EventLog).
- **Validate:** load test (10k instruments tape) p99 publish latency budget; incident-replay drill documented in runbook.

---

## 10. Final Deliverables

### 10.1 Component responsibilities (target, one line each)
- **tradex_domain:** types, FSM table, bucketing w/ grid, ports, zero I/O.
- **ExecutionEngine:** the only order mutation path; idempotency → risk → fill → FSM → events.
- **EventLog/Recovery (new, ported):** durability + crash resume for that path.
- **FillSource family:** the mode seam; nothing else knows the mode.
- **RiskManager/RiskPolicy:** the only order-approval authority; fail-closed in live.
- **MTM service:** the only unrealized-PnL writer.
- **ReactiveStrategyEngine:** signal → command bridge; next-open timing only.
- **ScannerEngine:** explicit windows only; no wall clock.
- **SessionDriver ×4:** data + clock; nothing else.
- **boot():** the only object graph.
- **Frontend store:** projection of server state; intent capture; zero authority.

### 10.2 Anti-patterns found (name them to kill them)
1. **Parallel universe** (`events/` stack) — the same subsystem built twice with different semantics.
2. **Browser-omniscient OMS** — 9-state FSM in JS claiming broker truth.
3. **Idempotency theater** — tokens generated, never delivered.
4. **Silent-degradation risk gate** — missing providers ⇒ missing enforcement (live).
5. **Attribute-sniffing seams** (`getattr(..., False)`) instead of protocol members.
6. **Mapping-in-a-route** — order-type/status translation inline in HTTP handlers.
7. **Two time conventions** (naive IST vs UTC) with conversions at arbitrary edges.
8. **Mode as UI decoration** — replay/live distinction not represented in session state.
9. **Migration shims fossilized** — fallback globals and polling loops self-documented as temporary.
10. **Probe scripts in the tree** importing the dying stack.

### 10.3 Before → After

| | Before | After |
|---|---|---|
| OMS stacks | 2 (execution + events) | 1 + durable event log |
| Order FSMs | 3 (domain, events/, browser) | 1 (domain) + 3-state intent |
| Idempotency | engine-only, chart orders bypass | end-to-end, required |
| Unrealized PnL | never computed live; risk gate blind | MTM service; gate fail-closed |
| Replay | market-data only; trading unguarded | session-scoped driver, same pipeline |
| Fill timing models | 3 (next-open, signal-close, recording-bridge) | 1 (next-open) |
| Status vocabularies | 4 | domain enum + one wire mapping |
| WS sockets in browser | 3 | 1 multiplexed |
| Timezone handling | implicit at ~6 sites | SessionGrid, 2 boundaries |
| `main.ts` | 1,003 LoC | < 300 per module |
| Net LoC | — | **negative** (deletion-led refactor) |

### 10.4 Non-negotiable rules for future development

1. Every order-carrying request carries an idempotency key; the server, not the client, is the dedupe authority.
2. Order state changes only through the domain FSM; the browser renders, it never adjudicates.
3. Every rupee computation is Decimal, in one place, server-side; the frontend formats.
4. No `datetime.now()` inside the pipeline — the injected `Clock` is the only time.
5. No wall-clock in deterministic modes — a missing provider fails closed in live.
6. One bucketing function; one session grid; UTC inside the pipeline, IST only at storage/ingest edges.
7. New broker = ports + capability table + tape fixture + contract tests, nothing else.
8. Wire changes are versioned (`tradex.vN`) and additive within a major.
9. Deleting a duplicate beats refactoring it; a second implementation of anything with money semantics is a defect.
10. Parity suites are release gates, not documentation.

---

*Evidence trace: every claim above cites a file inspected on 2026-09-04 at branch `refactor/execution-state` — `execution/engine.py` (guard-cid skip, fill fingerprint, kill switch), `position_math.py` (unrealized=0), `interface/routes/orders.py` (no cid), `trade-feed.ts` (token stripped), `order-engine.ts` (9-state FSM), `events/*` (parallel stack), `backtest.py` (M3 bridge), `bar_aggregator.py` (IST hardcode), `market.py` (UTC bucketize), `main.ts` (3 sockets, replay transport), `stream.py` (per-connection aggregators), `routes/chart.py` (book mapping, UNKNOWN→rejected).*
