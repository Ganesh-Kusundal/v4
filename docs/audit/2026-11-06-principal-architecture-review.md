# Principal Architecture Review — TradeX v4

**Date:** 2026-11-06 · **Reviewed commit:** `638b24e12dcf2b67552239c0b144143c12e62915` (HEAD, "feat(gateway): expose lot size on instrument and derivative endpoints")
**Method:** direct source verification (file:line) + two parallel deep-dive surveys (replay/backtest/live parity; API/contract surface) + one full-suite guard run and the parity/contract suites. Claims labelled **[V]** were verified by reading or running in this checkout on 2026-11-06; claims from the surveys carry their own citations.
**Prior art:** `docs/audit/2026-09-27-full-platform-review.md` (the 13-specialist review). Most of its three headline findings were remediated in September–October (see §1.4). This review is the post-remediation state, plus everything that landed since: the **Chart UI was deleted 2026-09-29** and the **`tradex_gateway` package landed 2026-11-06**.

---

## 0. Verdict

This is no longer "a partially implemented app with duplicate logic". It is a **serious, heavily-tested backend core** (a frozen-dataclass `tradex_domain`, a single AC-22 order path through `ExecutionEngine.submit()`, an append-only hash-chained event store, broker-capability adapters, and 170 parity + 196 contract tests that pass) that has **lost several of the things on its perimeter**:

1. **There is no frontend.** The chart host (`frontend/` + `openalgo-charts` clone) was removed 2026-09-29 (CLAUDE.md:217–220). The API surface remains; the UI does not. Any "frontend fix" section in a spec like the one this review answers is therefore a **green-field rebuild plan**, not a refactor — and the API contract it would consume is not yet typed (spec 0003 is ~5% implemented).
2. **The newest package, `tradex_gateway` (committed at HEAD today), ships with the repo's architectural guardrail test red.** `tests/test_guardrails.py::test_deleted_time_helpers_stay_deleted` fails on the committed tree: `gateway/src/tradex_gateway/history.py:371` redefines `_tf_seconds`, which the cleanup waves consolidated into `domain/timeframe.py:114 bucket_seconds`. The `verifications done on this checkout: 524 passed / 1 failed`.
3. **Paper/live strategies run a different indicator path than backtest.** Backtest wires `context.analytics`; paper/live boot does not (`startup.py:828–838`), so golden-gated indicator math never executes in production and the hand-rolled `_closes`/`_sma` fallback in each strategy does.
4. **"Replay" does not trade and "live" does not replay** — the two share bars via `BarAggregator` but diverge in engine wiring, analytics, timing (wall-clock vs FakeClock), sizer equity source, pending expiry, and cash gating (details §4–§6).

**Real-money verdict.** Defensible for paper trading and for research. Still **not deployable with real money**, now for narrower, verifiable reasons than in September: the risk spine is wired and parity-tested, but (a) the live HTTP surface permits live order placement with only a UI-advisory gate, (b) the typed wire contract that was supposed to make the API safe to consume is unimplemented, (c) the paper/live indicator divergence means your live P&L can differ from your backtest P&L on the same strategy/bars, and (d) HEAD ships a red architectural guardrail — the pipeline that is supposed to catch this class of drift did, and the commit landed anyway.

---

## 1. System audit & gap analysis

### 1.1 Current shape (measured)

| Fact | Value | Source |
|---|---|---|
| Workspace members | 14 (`uv` workspace) + `services/duckdb-analytics` + `gateway` (15th, recent) | `pyproject.toml [tool.uv.workspace]`, `gateway/` |
| Production LOC | ~80k `*/src/*.py` | `find … \| wc -l` [V] |
| Legacy behavioral test suite | 337 files, ~85k LOC under `trading/tests/` (non-member by design) | `trading/pyproject.toml` (no build-system) |
| Per-package suites | domain 18, brokers 40, execution 13, runtime 8, … | find [V] |
| Guard tests | 26 files; **1 failing at HEAD** [V] | `tests/` run 2026-11-06 |
| Parity / contracts | 170 + 196 passed [V] | `pytest trading/tests/parity trading/tests/contracts` |
| Goldens (chart-parity) | 145 JSON fixtures (~1.9 MB) + `PROVENANCE.json`; pin `be7418e` (label v2.3.2); TS math vs Python analytics is *deliberate* parity, not accidental duplication | `trading/tests/analytics/`, `openalgo-charts.pin` [V] |
| Domain quality | frozen dataclasses, `Decimal` money, FSM order transitions, zero internal deps | `domain/src/tradex_domain/*.py` [V] |

### 1.2 Verified violations of SoC / SOLID / SSOT

1. **Frontend logic inside the backend (presentation in the transport layer).** `interfaces/stream_indicators.py` embeds incremental indicator compute + a module-global `TAIL_CACHE` (~lines 100–680); `stream.py:318–344` reshapes depth frames to the deleted openalgo-charts "Engine MarketDepth contract" with an LTP fallback; `stream.py:806–820` and `chart.py:249–252` encode IST-session/axis contracts; `chart.py:36–90` maps domain→chart-tier order status incl. an `UNKNOWN` presentation tier (API-contract survey §8). This is the old UI's presentation policy baked into the API. If a frontend returns, it must own this, not the backend.
2. **Duplicate time/interval math — the pattern the guard exists to catch.** HEAD's `gateway/history.py:371` reintroduces `_tf_seconds` after D4/D5 consolidation into `domain/timeframe.py` (`bucket_seconds`, `interval_seconds`) — guarded test now red [V]. The same class of drift exists in timezones: `domain/timezones.py` (IST_ZONE ZoneInfo) vs `domain/market_calendar.py:28` (`IST = timezone(timedelta(5,30))`) — a known, documented duplicate ("a separate follow-up"); and prose/literals re-stating the 09:15 open (`scripts/generate_fixtures.py:8`, `duck_analytics/config.py:13–14`, plus prose in `mcp/server.py`, `catalog.py`, `scanners.py`). One `market_calendar`/`timeframe` home; everything routes through it.
3. **Two same-named permission functions with opposite semantics** — `config/permissions.py:49` (detect-and-refuse, wired at `startup.py:299`) vs `brokers/…/token_lifecycle.py:80` (repairs via chmod, unwired). Documented in ARCHITECTURE.md §11.5, still two policies under one name.
4. **Two bus implementations** — `reactive Bus` vs `ThreadSafeReactiveBus` (by design) plus a deprecated introspection stub `_backpressure_triggered` retained "for backward compat with test import" (`bus.py:43–45`) [V].
5. **Duplicated event-ish modules in domain**: `barrier/fill_references/protective/order_factory/risk_budget` are focused and legitimate; but domain also carries `bar_source.py`, `market_builder.py`, `market_calendar.py` (session logic) and `timeframe.py` (bucketing) — the two latter are the correct single homes and everything else should route through them (the gateway does not).
6. **Application layer is live but razor-thin.** `application/` has real use-cases (`cancel_order`, `modify_order`, `_price` wire parsing) wired into `interfaces/routes/orders.py` — never delete it, but everything else orbits `ExecutionEngine` directly, so the layer earns its keep only as the place where order commands gain business vocabulary. Either grow it to own all three order use-cases end-to-end or accept it as a naming seam; it must not accrete business logic ad hoc.
7. **Interfaces does everything.** It imports 12 internal packages (ARCHITECTURE.md §4.1 table) — more than runtime. It is legitimately the composition leaf, but it also re-implements presentation shaping (§1.2.1), auth/CSRF/session plumbing, workspace persistence, replay guarding, and safety reporting. It is a 9k-LOC god-leaf.
8. **Doc/SSOT drift.** `docs/ARCHITECTURE.md` is dated 2026-09-29 and is now wrong in at least: package counts, "known drift" §11 (claims `account_sizer` unwired — it is wired at `startup.py:808–810` [V]), the absence of `gateway`/`screening.py`/`sensitivity.py`/`retry_config.py`, and LOC totals. `README.md` quick-start is a hand-walkthrough no test executes.

### 1.3 Dead / test-only code at this commit

| Item | Status |
|---|---|
| `DurableFillDedup` (`execution/idempotency.py:210`) | production callers: **0** [V] — engine builds plain `FillDedup` (`engine.py:71,232,277`); shelf-ready class |
| `fingerprints_agree()` (`execution/idempotency.py` vs `sqlite_event_store.py:704`) | **0 callers** — two fingerprint implementations pinned only by convention |
| `SQLEventLog` (`reactive/event_log.py:21`) | durable-bus replay log; **0 production callers** (tests only); `startup.py` never passes `event_log=` — the "durable bus" mode is dead-in-practice |
| `assert_runtime_secret_permissions` (`brokers/…/token_lifecycle.py:150`) | callers: none anywhere [V] |
| `assert_secret_file_mode` (`brokers/…/token_lifecycle.py:80`) — the *repair* twin | 0 external callers; config twin (`permissions.py:49`, refuse) is the one used (`startup.py:45,444`) |
| `check_secret_files` / `is_insecure_mode` (`config/permissions.py`) | **0 prod callers** — only the single assert path is wired |
| `ParquetMarketProvider` (`market_data/market_provider.py:39`, deprecated) | still used by interfaces chart/stream at ×8 sites — transitional, not dead; schedule removal |
| `_backpressure_triggered` (`reactive/bus.py:41`) | introspection stub kept "for backward compat with test import" |
| `runtime/clocks.py`, `analytics/runtime.py`, `runtime/metrics.py`, `brokers/common/dhan_segments.py` | shims/impl pairs (legit except `clocks.py` shim → `domain`, keep until callers migrate) |
| Analytics full-recompute | **3 production call sites** still recompute-per-push/bump: REST `chart.py:482–484`, WS `stream_indicators.py` (per-push, by design today), backtest views (`views.py:46–49`, version-bump ⇒ per-bar recompute) — O(n²) over a run |
| `BarAggregator.flush()` at 15:30 | no production caller — the final bar of the day only closes on teardown/next tick [V from parity survey] |
| `SessionBarAggregator` `RevisedBar` handling | `_revise_closed` emits `RevisedBar`; the frame publisher accesses `.closed/.timeframe` → `AttributeError` swallowed by bus isolation; AC-11 revised bars reach **nobody**; `mark_filled(...)` (the traded-bar freeze) has no production caller [V] |
| `SessionBarAggregator` late-tick / `FifthStep` | emitted but effectively dead on the live path (see `RevisedBar`) |
| `_replay_error` duplicate definition (`stream.py:933` vs `:948`) | first shadowed by the second — dead code in the WS layer, two different lanes used |
| `benchmarks/` + `brokers/.benchmarks/` + `.benchmarks/` | perf claim in the 09-27 review ("44.1 µs/tick") was against a stub payload; keep but demand real load gates |
| `test doubles kept in src` (e.g. `InMemoryEventStore`, `FakeClock`) | fine as injectable seams; never construct them in production wiring |
| `gateway/` load harness (`gateway/tests/load_harness.py`, 661 lines) | strong chaos tooling; zero production callers — good, it's test-only by design |

### 1.4 What the 09-27 review found vs what is true today

| 09-27 finding | Status now |
|---|---|
| `abs(strength)` sizing; every strategy `strength=1.0`; Sizer type absent | **Fixed**: `account_sizer(equity_source=…)` wired at `startup.py:808–810`; `RiskConfig` limits enforced (require_limits at boot for paper/live). Backtest parity measures an 8,333 (backtest) vs 7,916 (live-ish sizer) divergence documented in ARCHITECTURE.md §11.4/§11.5 era — equity-source difference is now deliberate, still a divergence |
| Strategy lifecycle write-only / `pause()` no-op | **Fixed**: sessions dispose subscriptions on stop; `StrategyRuntime` lifecycle is exercised by the parity suite |
| Backtest look-ahead (deferred flush on last candle) | **Fixed**: AC-1 next-open fill; last-bar signals reported `num_unfilled`, never flushed (`backtest.py:966–981`) [V from survey] |
| `/` serves the live API key | **Fixed/removed**: frontend gone; no API key is injected into served HTML (no served HTML at all) |
| Live order path unreachable from UI | **Moot** (no UI) — replaced by the inverse problem: live order placement reachable over HTTP with only an advisory UI gate |

The pattern to internalize: **this codebase remediates found defects aggressively once they are on an audit list; it does not reliably prevent new ones.** The gateway landed this week re-creating a guard-violating helper the cleanup waves had deleted, and HEAD is red. Prevention, not audit, is the gap.

---

## 2. Domain model — what exists, what must change

### 2.1 Verdict on the current model (largely keep)

`tradex_domain` is the strongest layer in the repo [V]:

- `Price`/`Quantity`/`Money` as `Decimal` value objects; frozen `slots=True` dataclasses everywhere; `Order` FSM with explicit legal transitions and fill-quantity invariants (`execution.py:_LEGAL_TRANSITIONS`); `Fill.fill_id`, `product_type`, `strategy_id` for per-trade accounting; `Position.mark_price/marked_at/mark_source` with a venue-agnostic mark *policy name*; `InstrumentId` canonical key; `HistoricalSeries` with session-anchored `_bucketize` (no epoch-floor drift).
- No broker-specific or UI-specific fields in domain objects: holds (broker names never appear as enums in the wire; marks are "BID"/"ASK").

### 2.2 Defects to fix

1. **Tick vs bar are not unified.** `Quote` and `Candle` are siblings; there is no `MarketEvent` abstraction the way the pipeline spec wants. The bar aggregator is a runtime concern, so the *domain* lacks `Tick` → `Bar` derivation semantics; replay/live share `BarAggregator` but backtest bypasses aggregation entirely (publishes `Candle` direct). Keep `Quote`, rename/refine `OHLC` is fine, but add a `StreamEvent` union (Quote|Depth|Candle|RevisedBar|FeedStatus) as the single bus payload contract with schema version.
2. **`RevisedBar` exists but is not first-class.** Either delete it (and delete `mark_filled`'s caller-less contract) or make it a proper `CandleRevision` domain event, tested end-to-end, with the freeze enforced (a bar booked into a strategy must not be mutated). Current state is the worst option: present, broken, and swallowed.
3. **Timestamp discipline is inconsistent by layer:** datalake stores **IST-naive** candle stamps; live fills stamp **tz-aware IST** (`live_fill_bridge.py:64–74`); backtest uses `FakeClock` UTC-ish; `datetime.now(UTC)` defaults are scattered (`Quote.timestamp`, `Fill.timestamp`, `OrderRequest.reference_timestamp`). Pick one contract: **all domain timestamps tz-aware UTC**, convert to IST-naive **only at the datalake boundary** (documented pinning), delete naive paths from domain, add an AST guardrail test forbidding `datetime.now(` in `domain/` without `UTC`.
4. **`Candle` has no provenance.** Add `source: CandleSource` (simulated|replayed|live) is *optional* — better: never add mode to domain; mode belongs in the session config. Do NOT add provenance fields. (Flagging because audit #1 in 09-27 recommended similar conveniences that caused mode-forks.)
5. **Symbols-as-strings in APIs.** `Instrument` serializes to canonical string keys — fine. But `Quote.exchange`/`provider` free strings duplicate what `InstrumentId` already knows; keep for diagnostics, document as non-authoritative.
6. **No domain type for `Signal`.** Signals live in `tradex_strategy`/`core/engine.py` (`strategy/strategy.py` in domain has strategy artifacts, not the live signal DTO). A `Signal` value object (instrument, side, strength, reference_timestamp, strategy_id, rationale) should exist in domain so backtest/replay/live serialize it identically and `correlation_id()` derivation is one place.
7. **Session / trading day.** `domain/market_calendar.py` holds the NSE/BSE/MCX calendar and `market_builder.py` builds sessions — good. But paper/live/backtest disagree on *when* a day is open (see §4.3 wall-clock leaks). The model is right; the enforcement points are wrong (broker adapters shouldn't own session policy).

### 2.3 Target unified domain (backtest = replay = live)

```
InstrumentId, Timeframe, Side/Exchange   (identity — frozen, hashed)
Candle (1 tick ⇒ notional timeframe; OHLCV; UTC-aware ts)
CandleWindow / HistoricalSeries          (typed series, session-anchored bucketing)
Indicator(state)                          (incremental, push-once semantics)
Signal                                    (side, strength, ref_ts, strategy_id)
OrderRequest → Order (FSM) → Fill → Position → PortfolioSnapshot
Money (Amount = Decimal, Currency = INR)
RiskParams (limits, drawdown, daily-loss)  (typed, validated at boot)
TradingSession (state machine, calendar-anchored)  (runtime concern, domain enums)
```

Rules to enforce with tests:
- One serialization module (`domain/serialization.py`) for **every** domain type; no ad-hoc `to_dict` in packages.
- `FromDict` must reject unknown keys (currently tolerant) — forward-compat only via explicit `wire_version`.
- Zero domain → package imports (already true; keep `test_import_boundaries` red-green).

---

## 3. Architecture re-design (high level)

### 3.1 Where it sits vs where it should sit

Current (uv workspace ≈ clean architecture, but with a hollow seam):

```
L0 kernel   domain, observability        (good)
L1 adapters reactive, config, brokers, analytics
L2 engines  execution, application(hollow), strategy
L3 data     market_data, replay, gateway(?), services/duckdb
L4 compose  runtime
L5 edges    interfaces, deployed services (gateway)
```

Target (hexagonal):

```
              ┌──────────────────────── Presentation ─────────────────────────┐
              │  (absent) Chart UI host · CLI · this API's typed client       │
              └───────────────┬──────────────────────────┬───────────────────┘
                              │ typed DTOs only           │ WS frames (typed)
              ┌───────────────▼───────────────────────────▼───────────────────┐
              │  Interface layer  (fastapi_app + routes + gateway both front) │
              │  transport: auth, csrf, rate-limit, envelope, versioning       │
              └───────────┬──────────────────────────────────┬───────────────┘
                          │ Command/Query DTOs                │ Projections
              ┌───────────▼───────────────────────────────────▼──────────────┐
              │  Application layer  (use-cases: place-order, backtest,        │
              │  scanner-run, workspace…) — ONE owner per use case            │
              └───────────┬──────────────────────────────────┬──────────────┘
                          │ ports                            │ ports
   ┌──────────────────────▼──────────┐        ┌──────────────▼───────────────┐
   │  Domain / Strategy / Exec core  │        │  Projections & read models   │
   │  (pure, no I/O)                 │        │  (TradingCache, event store) │
   └──────────────────────┬──────────┘        └──────────────▲──────────────┘
                          │ ports (FillSource, BrokerAdapter,
                          │  MarketFeed, EventStore, Clock, PositionSource)
              ┌───────────▼──────────────────────────────────┐
              │  Infrastructure  brokers/paper, market_data,   │
              │  parquet, sqlite, duckdb, gateway, feeds        │
              └───────────────────────────────────────────────┘
```

Boundaries that must become enforceable (extend `test_import_boundaries.py`):

| From \ To | domain | application | runtime | interfaces | gateway |
|---|---|---|---|---|---|
| domain | — | ✗ | ✗ | ✗ | ✗ |
| application | ✓ | — | ✗ | ✗ | ✗ |
| runtime | ✓ | ✓ | — | ✗ | ✗ |
| interfaces | ✓ | ✓ | ✓ | — | ✗ |
| gateway | ✓ | ✗ (use own I/O path) | ✗ | ✗ | — |

**Gateway verdict (verified):** it is a *second product* (external dual-broker data plane for the "amt-institutional-order-flow-scalper" app, design doc `docs/superpowers/specs/2026-11-06-broker-data-gateway-design.md`, untracked at first landing), not an interfaces extension. It is wired into **workspace membership + root testpaths + import-linter + mypy ratchet only**: not in `predeploy_gate.sh`'s import smoke (14 packages, omits it), **not in CI coverage** (quality-gate runs `--cov` for 13 packages — neither `tradex_gateway` nor `duck_analytics`), not in clean-install imports, not in the Dockerfile COPY list/ENTRYPOINT, not in compose or Prometheus, and documented nowhere. It re-composes `build_broker_from_env` itself (`brokers.py:162–180`) — a **second composition root** alongside `runtime/startup.py`. It shares 6 capabilities with interfaces (history, quotes, auth, throttling, errors, WS) and adds dual-broker health-aware failover + TTL history cache — genuinely useful for its external consumer. Before it accrues a user: (a) fold its time-bucket duplication into `domain.timeframe` (make the guard green — delete `_tf_seconds`), (b) decide explicitly whether it shares `brokers/` adapters or its own composition, and port either the failover logic or the composition into one home, (c) give it the same typed wire contract + error envelope as interfaces — currently interfaces serves the session, gateway is the data plane; same repo, two philosophies (`/v1` vs root-mounted), zero shared transport code. (d) One repo, two services means one **documented** launch path each; today gateway "ships but is never launched". Fix that, or spin it out.

Related second-product finding: `services/duckdb-analytics` (`duck_analytics`) has **zero workspace importers**, ships a **tracked second `uv.lock`** (164K, 2026-09-13) despite root-lock consolidation, and is in root testpaths (61 tests) but unlaunched. Same decision needed: document, fold, or spin out.

### 3.2 Frontend vs backend division (when the UI returns)

**Frontend owns (and only these):**
- Rendering (chart library host), input handling, workspace layout persistence keys, optimistic UI *only for non-money affordances*, order ticket UI state, replay **transport** (play/pause/speed/seek) — never the replay *logic*.
- Formatting for display (unit, precision) derived from server-supplied display metadata.

**Backend owns:**
- All trading logic, risk, sizing, fees, P&L, indicator math (authoritative module in `tradex_analytics`), session windows, corporate actions, and *the decision that a bar is "final"*.
- The typed contract (§7). No chart-specific shaping in routes: return domain DTOs, let the client project.

**Forbidden in frontend:** PnL math, risk checks, indicator math on raw bars (except transient previews clearly labelled "preview" and re-validated server-side), session-window decisions, any secret, `qty>0` logic beyond validation hints, order-status mapping from venue codes (server maps once).

### 3.3 What is strictly forbidden in the API layer

- `body: dict` / `-> dict` (chart routes are 100% dict today) — everything through Pydantic DTOs ≥ spec 0003.
- Two error envelopes: keep `{error:{code,message}}`, port gateway's `retryable/retry_after/broker` fields into it, single `ErrorResponse`.
- Presentation-tier enums (the `UNKNOWN`-for-UI order status in `chart.py:map_order_status`) — serve the domain enum plus `terminal: bool`.

---

## 4. Trading pipeline re-architecture (deterministic, event-driven)

### 4.1 Target pipeline (the one every mode runs)

```
Feed(broker|tape|generator) → Normalize → Clock(pinned) → BarAggregator/Tick   [event]
      ↓ BarClosed(Candle)
  IndicatorEngine.push(candle)   # incremental — O(1) amortized, no recompute
      ↓ IndicatorUpdate
  Strategy.on_event(MarketView)  # sees only closed bars + frozen state
      ↓ Signal
  RiskManager.check(SignalRequest) → clamp/resize/reject
      ↓ OrderRequest (correlation_id deterministic)
  OMS.submit → FillSource(sim|paper|book|broker)
      ↓ Fill / OrderEvent
  PositionAccountant + CashLedger + EventStore.append   # atomic with fill
      ↓ PortfolioSnapshot / PnL
  Projections (cache, metrics, WS broadcast)
```

Rules:
1. **One engine.** `ExecutionEngine` is already near this; make `replay`/`backtest`/`paper`/`live` thin **session configurations** of one runner (`TradingSession` + `Feed` + `FillSource` + `Clock`), not co-equal codebases. Today we have: `BacktestEngine` (bus driver), `ReplayController` (chart filler, no trading), `SessionBarAggregator` (live only), `BarAggregator` (shared), `TapeRecorder` — five overlapping time-domains.
2. **Indicator state lives in the pipeline, not per view.** Kill the "version bump → full recompute" cache (`analytics/views.py`, `cache.py`) in favor of `IndicatorEngine.push(candle) → IndicatorUpdate` with the SAME math the 107 goldens pin (the goldens must be the oracle both for ports).
3. **Bar-close semantics are explicit.** `Candle.closed: bool` (or distinct event `BarClosed`). Strategies and the chart both consume `BarClosed`. Tick-driven previews are a separate, lossy lane.
4. **Time travel is control, not logic.** Replay = same session with a `ReplayClock` injected; seeking rewinds the *session state machine* (store snapshot → fold tail), never forks code. The current `ReplayController` bypasses the trading spine entirely — keep it only as the tape publisher for a future UI's "watch the tape" mode, not as a second replay.
5. **No recomputation per candle.** `analytics` must expose `push`-style incremental indicators (goldens already define their batch reference; add stream-incremental parity to `test_indicator_parity_gate.py` for every registered indicator — today only a subset is parity-gated).

### 4.2 What exists today vs that target

| Stage | Backtest | Paper/Live | Target delta |
|---|---|---|---|
| Feed | datalake Candle list, direct bus | WS → `MarketFeed` → `BarAggregator` | **one `Feed` port** with two adapters |
| Clock | `FakeClock` (`backtest.py:175`) | wall clock / broker ts | inject `Clock` everywhere; ban `datetime.now` outside infra |
| Aggregation | none (synthetic bars fed) | `SessionBarAggregator` | unify; backtest feeds tick-aggregated bars too for full parity |
| Indicators | `AnalyticsRuntime` fresh per run; full recompute at **3 production sites**: backtest views (`views.py:46–49`, O(n²) per run), REST `chart.py:482–484`, WS `stream_indicators.py` per-push | wired to nothing (`analytics=None`) → per-strategy hand fallback | `IndicatorEngine` (incremental) wired for ALL modes; kill all 3 recompute sites |
| Strategy seam | `ReactiveStrategyEngine` | same | keep (this part is genuinely shared) |
| Order seam | `SimulatedFillSource` | `PaperFillSource`/`PaperBookFillSource`/`BrokerFillSource`+`LiveFillBridge` | keep the `FillSource` port; unify book-match config |
| Accounting | `PositionAccountant` + engine `CashLedger` (no durable store) | + `SQLiteEventStore` durability | backtest may use in-memory store; same accounting object always |
| MTM | bar-close candles | quote-driven `MarkToMarketService` | one `MarkingPolicy` on the port |
| Corporate actions | point-in-time applied | **not wired** | wire or explicitly forbid in live with loud config error |
| Protective legs | engine-simulated stop-market gap (`stop_gap_bps`) | rides venue super-order | document that parity suite tests unprotected only; add protected-parity cases |
| Sizing | fixed opening equity | live `equity_source=broker cash` | one sizer; document backtest must pass `equity_source` fn too |

### 4.3 Live vs replay divergences to eliminate (all verified [V])

1. **Indicator path fork** — paper/live `analytics=None` (`startup.py:828–838`); strategies fall back to hand-rolled `_closes` (e.g. `sma_cross.py:87–112`); golden parity never exercises this branch ⇒ **silent signal divergence**.
2. **`bar` final-close at 15:30** — `SessionBarAggregator.flush()` has no production caller; last bar of the day reaches nobody.
3. **`RevisedBar`/traded-bar freeze** — both unhandled (see §1.3); AC-11 is unimplemented in production paths.
4. **Wall-clock leaks** — `PaperBroker._default_clock` (`paper/adapter.py:158–162, 993–1016`); `PaperBookFillSource` defaults `clock=None` → after-hours book runs silently zero-fill; `TradingSession.paper()` passes nothing (`session_impl.py:543–595`). Two paper paths disagree with each other, not just with backtest. Backtest's clock pin saves it; paper/live have none.
5. **Pending expiry** — backtest `0` (immortal) vs paper/live `pending_max_age_bars=3` (`schema.py:364`).
6. **Cash gating asymmetry** — backtest `allow_negative_cash=(risk_manager is None)`; paper/live `fail_closed_cash=True` + broker-seeded ledger (`startup.py:731–737`).
7. **Sizer equity source** — fixed `initial_capital` vs live broker-cash (`startup.py:798–819`) — the documented 8,333 vs 7,916 divergence.
8. **Timezone mixing** — live fills tz-aware IST vs backtest UTC/naive — breaks naive subtraction in analytics (`fill.timestamp - lot_time`) — pick UTC-aware everywhere (§2.2.3).
9. **Costs** — `net fees` in backtest only if caller passes `FeeCalculator`; paper/live via `fees_enabled` (HIGH-6b). Default-on must be the same in all modes; fees are not optional.
10. **Fill timestamps** — `FillModel.fill_timestamp` falls back to `now()` (`fill_model.py:74–88`); `LiveFillBridge` stamps `datetime.now(UTC)` fallback (`live_fill_bridge.py:43`) — any input lacking `reference_timestamp` breaks determinism silently.
11. **`ReplayController` vs backtest** — replay feeds charts and never trades; nothing pins "replay does not reach the venue" as a contract. Add the negative invariant as a test.
12. **Session state vs wall clock** — paper refuses fills outside 09:15–15:30 IST weekdays; backtest books historical bars without gate (correct for research). Codify the difference as config intent, not adapter behavior.

Each item must land as either **code deleted/unified** or **a parity test that fails on divergence** (§8).

---

## 5. Frontend — the honest plan (nothing to refactor)

### 5.1 State of the world [V]

- `frontend/` deleted 2026-09-29 (CLAUDE.md:217–220). `openalgo-charts` survives only as the pin for golden fixtures (`openalgo-charts.pin`, v2.3.2).
- The old host was ~1,145 LOC in 9 modules; it had already been measured as a *pure consumer* in the 2026-09-13 integration map — the problem was never the host, it was the contract under it.
- `.kanban/` board shows the platform post-cleanup; no UI task exists. `tradex_gateway` (2026-11-06) is data-plane-only for an external app, not this UI.

So a "frontend refactor" cannot be executed; what can be produced is the **contract-first re-integration spec**:

### 5.2 Target: pure-consumer frontend

```
UI host (any thin webview/chart lib — openalgo-charts 2.3.2 or equivalent)
   │  HTTP: typed fetch client (generated from OpenAPI)
   │  WS:   typed frames (quote/depth/bar/order/fill/position/feed_status)
   ▼
Backend projections only — no business logic in the client
```

Non-negotiable client rules:
- **Time control only.** Replay buttons send `replay_*` intents; the server decides events. The client never fabricates a bar, a fill, or a PnL.
- **One price of truth.** Charts render server bars; the client may resample for display only with identical bucketing as backend (or fetch server-resampled).
- **Order ticket** is a DTO builder (`OrderRequest`), not a validator of trading rules; the server validates (`__post_init__`) and the client surfaces the typed error envelope (`code`), never string-matching messages.
- **No client-side caching of money.** `/portfolio`, `/orders` always fresh or via WS increments reconciled by `seq` + snapshot hash (extend `sync.py` design).
- **Generated types.** `openapi-typescript` (or hand-mirrored Pydantic→TS) with a CI diff-gate from the moment spec 0003 lands (§7).

### 5.3 What the backend must expose first (blocks any UI)

1. Spec 0003 fully implemented (§7) — without it, any UI re-grows `dict`-shaped glue.
2. `BarFrame` (typed) used by **both** history endpoint and WS — one `BarDto`, one `TickDto`.
3. `ReplayProjection` as a first-class query: given `(symbol, timeframe, range)`, returns the exact bar sequence the engine would see — then the chart is *literally* a projection of the engine.
4. Safety/status surface for an "arm/disarm/flat" affordance: `/health/ready`, `/api/charts/safety`, WS `feed_status` — server-enforced arm flag, not advisory (fix the live-order HTTP hole: enforce `live_orders_enabled` server-side, or remove the gate and hard-block live HTTP orders in production with `live_enabled=false` default at HTTP layer too).

---

## 6. Backend — refactor plan

### 6.1 Broker-agnostic OMS (exists; harden)

Keep: single path AC-22 (`engine.submit`), idempotency (`IdempotencyInflight` + hash-chained event store + durable fill index), UNKNOWN-submission policy, kill switch, `PositionAccountant`/`CashLedger` split, drift reconciliation + adopt path, crash recovery via event fold. This is real and parity-tested.

Change:
1. **Durable fill-dedup online.** Either wire `DurableFillDedup` to the live fill bridge, or delete it and rely on `FillDedup`+`fill_id` — do not ship a tested-but-unwired dedup layer in a real-money path.
2. **Failure semantics for live fills.** The bus `OrderFilled` path re-checks affordability and can drop/reject *after* the venue filled (`engine.py:1123–1263` night path) — the venue fill is truth; the local reject must become a **reconciliation item**, never silently dropped. Verify-and-fix; add the test "venue fill arrives for locally-rejected order → adopted, not discarded".
3. **Bracket legs parity.** Backtest simulates stop-market gap fills in-run; live rides venue super-orders; parity suite covers unprotected only — add bracket-protected parity cases or forbid untested protection modes in production with a loud config error.
4. **Corporate-action adoption** for live sessions, or an explicit startup refusal for instruments with unprocessed CAs; backtest has it, live never does — a live P&L lie waiting for the next bonus issue.
5. **Recovery proof at boot (R1)** — keep. Add: `fill_sources` capability check at boot (broker must satisfy the fill-source contract or boot fails), and a dry-run `engine.submit(probe_order)` in a `--dry-run` mode for deploy smoke.
6. **Secret permissions at boot** — wire `assert_runtime_secret_permissions('runtime')` (or the config variant; pick one policy: refuse > repair). Delete the losing variant **and** the duplicate mode constant — `SECRET_FILE_MODE` (token_lifecycle.py:62) vs `EXPECTED_SECRET_MODE` (permissions.py:31; its comment claims it "reads" the token-layer constant but hardcodes it). Fix `.env.local` 0644 on this machine, or boot refuses — as coded today, that is the correct behavior. Also note the failed goldens/permission story: the token-state file pairs under `runtime/{dhan,upstox}/` are local broker state, gitignored — correct — but they sit next to source; keep it that way and never export them.

### 6.2 Paper broker — the ugly middle

It is simultaneously (a) the simulator, (b) the risk-gate prover, (c) the CI oracle. Split concerns:
- Extract **book matching** (price/time priority, circuit blocks, session gate) behind `BookMatcher` port with injected clock — one implementation, used by `PaperBroker` *and* `PaperBookFillSource`.
- Delete the wall-clock defaults; `TradingSession.paper()` must take an explicit `clock=`; parity tests pin it (they already do).
- Make the session gate a *capability* of the simulated venue, evaluated off the injected clock — then backtest and paper agree by construction.

### 6.3 Lifecycle per trading day (make it a first-class state machine)

```
PRE_OPEN → COLD_START(feed READY proof) → OPEN(ARM?) → DAY_TRADING →
15:30 FINAL_FLUSH (flush aggregator, persist day snapshot, seal event log segment) →
POST_CLOSE (reconcile vs venue, adoption if clean, CA processing, next-day plan) → STOPPED
```

Today `SessionState` stops at READY/RUNNING/STOPPED-ish; nothing owns the **end-of-day seal**. The 15:30 flush bug (§4.3.2) and the day boundary for risk baselines (`risk_baseline_store` exists but is under-exercised) fall out of this. `TradingSession` must own the day lifecycle; `startup.boot` wires it; shutdown (`shutdown.py`) implements the seal; a `tradex day-close` CLI proves it idempotently.

### 6.4 Gateway (new) — fold or ship properly

Its sibling second product is `services/duckdb-analytics` (`duck_analytics`, MCP server, 0 importers, tracked 164K second `uv.lock`). Decide them together: **one documented launch path each, or one repo each.** Otherwise the repo accumulates launched-never services with no coverage and no docs — exactly what happened here.

Acceptance criteria before gateway leaves the repo door:
1. Guard test green (`_tf_seconds` deleted; use `domain.timeframe.bucket_seconds`).
2. Docs: README section + docs/ARCHITECTURE.md update + runbook; predeploy gate includes it or explicitly excludes it with reason — and **CI gets `--cov=tradex_gateway` + clean-install import** (it is a workspace member that gates nothing today).
3. Contract discipline: same error envelope as interfaces; the `/v1` versioning policy shared with interfaces (or neither versioned but documented); `?api_key=` WS auth gets the same Origin check (§7.4).
4. History path uses `market_data/parallel_fetcher` windowing — no third implementation of the 90-day Dhan chunk.
5. **One composition root**: either port `build_broker_from_env`-with-`allow_order_operations=False` into `brokers/` as a public factory used by both (prefer — it's a capability already), or keep gateway's and delete the divergence deliberately with a comment. Not both forever.
6. Load/chaos harness (`gateway/tests/load_harness.py`, 661 lines) wired into a nightly CI job; PR gate stays the fast suite.

### 6.5 Deterministic simulation engine

`TapeRecorder` + `test_backtest_live_tape_parity.py` is the foundation of the determinism story. Keep and extend: record every production session's tape in CI-replayable form; property-based suite over tapes (fees monotone, position conservation, idempotency) already exists in skeleton (`test_property_parity.py`) — widen it to every strategy.

---

## 7. API & contract design

### 7.1 Contract rules (the spec, kept)

Adopt spec 0003 wholesale with the deltas in §3.3:
- Pydantic DTOs for **every** route (request+response). No bare `dict` bodies/returns — AST/lint rule in `tests/test_guardrails.py` (pattern: route handler signature contains `dict`).
- WS frames as models with a `type` discriminator; publish the JSON-Schema artifact `interfaces/openapi.json` + `docs/contracts/wire-v1.json`.
- Cache-hit/miss history shape made identical (spec's known defect persists: `chart.py:382–416`).
- Number policy: JSON **strings** for all `Decimal` money/qty; integers for epoch millis; floats only for display-derived analytics labelled as such.

### 7.2 Versioning strategy

- HTTP: `/api/v1/...` (move `/api/charts/*` → `/api/v1/charts/*`; keep old paths one release behind with deprecation headers `Deprecation: true`, `Sunset:`).
- Interfaces app version stays `0.1.0`; wire `openapi.json` `info.version` = contract version, bumped **only** on breaking DTO changes.
- WS: frames carry optional `wire_version: 1`; consumers must ignore unknown fields (add a test).
- Breaking changes: dual-serve (emit both frame shapes for one minor release) — cheap because serde is centralized in contracts module.

### 7.3 Event schemas (single source: `domain/events.py` + `domain/wire.py`)

| Frame | Schema (logical) | Producer |
|---|---|---|
| `quote` | `{type, instrument, ltp, bid, ask, ts_ms, seq}` | feed |
| `depth` | `{type, instrument, bids[], asks[], ts_ms, seq}` | feed |
| `bar` | `BarDto` (one shape history+WS) `{t,o,h,l,c,v,timeframe,closed:true}` | aggregator |
| `indicator` | `{type, series:[{time,value}...], source:"backend"}` | indicator engine (label authoritative) |
| `order` / `fill` | `OrderDto` / `FillDto` (strings for decimals) | OMS projection |
| `position` | `PositionDto` incl. `mark_source, mark_stale` | accountant |
| `feed_status` | `{state, ready, instruments_ok, ts}` | feed supervisor |
| `replay_*` | control + acks, `run_id` always present (kill the "sometimes absent" variant) | replay controller |

### 7.4 Streaming replay ≡ live

One broadcaster: `FeedTap` publishes the same `(quote|depth|bar)` union; replay = recorded input into the same tap. Client cannot tell which mode it is in except via `feed_status.mode`. **Replay and live must pass the same client conformance test** (drive both, assert identical frame sequences modulo `mode`).

### 7.5 Security defects on this surface [V]

1. Live order placement over HTTP allowed; server gate absent (`live_orders_enabled` is a UI-advisory flag). → Server-side gate or hard refusal in production mode.
2. WS `/ws/stream` has **no Origin check**; cookie `Secure=False`; `SameSite=lax` held only by luck. → Origin check + `Secure` behind TLS; SameSite=strict for the session cookie.
3. Fail-open paper auth when no api_key (`auth/deps.py:113–116`) — keep for dev, but **refuse non-loopback fail-open** even in paper.
4. CSRF only on order mutations; `/api/charts/backtest`, `/scanner/run`, `PUT /workspace/*`, `/auth/logout` unprotected. → CSRF (or strict-origin) everywhere state-changing; workspace writes same.
5. `/metrics` loopback-or-auth — good; pin with a test.
6. Two error envelopes; gateway richer. → unify.
7. No request-id / correlation header echoed on errors → add `X-Request-Id` end-to-end (bus carries correlation_id; HTTP boundary should too).
8. Rate limiting process-local, money-routes only (fine for single worker; document; add per-key daily caps if multi-tenant ever happens).

---

## 8. Test strategy (mandatory)

### 8.1 Inventory (measured [V])

- Guard/architecture: 26 files (`tests/`) — fast, valuable; **CI-red at HEAD**.
- Behavioral legacy: 337 files / ~81.7k LOC `trading/tests/` — the authoritative suite (61% of all test LOC in-repo); per-package suites are 133 files/~27.7k LOC, mostly contract/smoke (e.g. `interfaces/tests` = 4 files/388 LOC against 8.4k LOC of interface source — under-invested exactly where the dict-bodies live).
- Root `testpaths` = 22 suites (config + duckdb tests were silently never run until appended — same failure mode as the gateway coverage gap).
- CI coverage measures **13 packages** — `tradex_gateway` and `duck_analytics` are imported nowhere and covered nowhere.
- 2 TODO/FIXME markers repo-wide, both noise; no commented-out code blocks (the tree is disciplined on this).
- Only 2 same-basename collisions between `trading/tests` and package suites, both divergent content — no silent duplication of test fixtures.

### 8.2 Required additions (in priority order)

P0 — prevent harm:
1. **Live-vs-backtest tape parity through the production `SessionBarAggregator`** including cross-minute late ticks and the revised-bar freeze (currently broken — §4.3, testability gap).
2. **Analytics-wiring parity**: paper/live boot must produce identical signals to backtest on the same tape (catches §4.3.1).
3. **End-of-day seal test**: 15:30 flush reaches strategy; final bar books once (catches §4.3.2).
4. **Venue-filled-after-local-reject** adoption test (§6.1.2).
5. **Broker adapter contract suite** per broker vs the `BrokerAdapter` protocol, incl. capability-loud errors and `submit_super_order` legs mapping; keep `trading/tests/parity` as the venue-agnostic oracle; add nightly **sandbox/staging** smoke against Dhan/Upstox paper endpoints (opt-in, quarantined).
6. **Replay isolation contract**: replay session emits zero `OrderPlaced` to a live adapter (assert adapter spy untouched).

P1 — correctness breadth:
7. Property-based: fee monotonicity, position conservation, idempotent re-delivery, `Fill` dedup, sizer bounds, drawdown halt determinism.
8. Golden-parity for the remaining registered indicators; wire stream-incremental parity into the same gate (the goldens — 145 fixtures — must oracle *both* the batch and the incremental path).
9. Time-zone orthogonality property: no test may assert on naive datetimes outside datalake-pinned fixtures (lint or property).
10. Serialization round-trip + unknown-key rejection for every wire DTO (once §7 lands).

P2 — regression net:
11. Guard-suite additions: `no_naive_now_in_domain`, `no_dict_bodies_in_routes`, `no_wallclock_outside_infra`, `env_var_secret_at_rest` permissions scan at CI (permissions of `runtime/**/token_state.json` asserted before any test runs — cheap, prevents recurrence of the 0644 finding).
12. Performance gate: `bench_order_path` with real order sizes + latency histogram vs threshold in CI (advisory) and a load test (`gateway/load_harness` pattern) nightly.
13. Doc-vs-code: a test that greps `docs/*.md` for file:line citations and verifies them (the repo's own docs rot faster than its code; this one being exhibit A).
14. Coverage floors (CI-only, keep) + per-package floors for the five money paths (execution, brokers, runtime, replay, application).

### 8.3 Parity matrix as the north star

CI must have a single job, `parity-gate` (it exists — `parity.yml`) [V], extended with: strategies × sessions × clocks grid (sma_cross, mean_reversion, bracket_breakout × backtest, paper-pinned-clock, tape-replay × 3 speeds) all asserting identical fill streams modulo documented cost/slippage parameters. If a divergence is intentional, it lands in a checked-in `DIVERGENCE.md` with an owner and an expiry date; today the sizer divergence is documented but ownerless and unexpired.

---

## 9. Refactoring roadmap

### Phase 1 — Safety (1–2 weeks) — code frozen, only tests + observability added

**Change:** delete nothing; add tests §8.2 P0.1–P0.4, §8.2 P2.11–P2.13; wire boot guard `assert_runtime_secret_permissions` (refuse, loud); enable `TRADEX_RECORD_TAPE` in a weekly CI tape capture; fix `.env.local` to 0600 (operator action, verify with test).
**Delete:** nothing.
**Do NOT touch:** `domain/`, `execution/engine.py` pipeline body, `.pre-commit-config.yaml` ratchets.
**Validation:** guard suite green at HEAD; parity suite green; new P0 tests red→green on their fix PRs; doctor `.env.local` 0600; boot refuses 0644.

### Phase 2 — Domain cleanup (1–2 weeks)

**Change:** UTC-aware timestamps in domain (§2.2.3); promote `RevisedBar` to a proper event or delete it and its freeze semantics (decide; delete if unused — prefer deletion); add `Signal` DTO; tighten `FromDict` unknown-key rejection; add `MarketEvent` union + `BarClosed`; remove `Quote.exchange/provider` duplicates or hard-deprecate.
**Delete:** `_backpressure_triggered` stub; duplicate `_replay_error`; naive-IST handling outside datalake boundary; `protective.py` if truly unused after consolidation (verify callers first).
**Do NOT touch:** serialized wire compatibility without a version bump; event store schema (hash chain continuity).
**Validation:** domain 100% branch-covered arithmetic; goldens unchanged; serialization round-trip property green; `test_import_boundaries` green.

### Phase 3 — Architecture realignment (2–4 weeks)

**Change:** unify feed/clock/aggregation behind ports (§4); `IndicatorEngine` incremental, wired in boot (paper/live) and backtest; `BarClosed` through strategy seam; one `SessionLifecycle` with end-of-day seal (§6.3); gateway fold-in decisions (§6.4) or explicit exclusion — decide gateway + duckdb together; spec 0003 DTOs + OpenAPI + CI diff-gate + the `dict`-body lint (§7.1); unified error envelope; server-side `live_orders_enabled` enforcement; WS Origin check + `Secure` cookie; **CI coverage for `tradex_gateway` and `duck_analytics`**; `fingerprints_agree()` deleted and the two fill fingerprints (`idempotency.FillDedup` vs `sqlite_event_store.fill_fingerprint`) unified behind one home in `domain/fill_fingerprint.py` (which exists — verify callers use it exclusively); durable-bus dead code (`SQLEventLog`, `event_log=` parameter) deleted or wired — never both.
**Delete:** `AnalyticsRuntime` full-recompute path (replaced); one of the two secret-permission functions (+ duplicate `SECRET_FILE_MODE`/`EXPECTED_SECRET_MODE`); `SQLEventLog`/bus replay shim; `fingerprints_agree()`; interfaces' chart-reshape helpers (§1.2.1, §3.3) once BarFrame lands.
**Do NOT touch:** AC-22 single-door; UNKNOWN-submission policy; hash-chained event store format; broker adapter capability tables.
**Validation:** parity grid §8.3 green; replay server ≡ live server frame-conformance test; boot in `live` refuses without feed/risk/durable events (already) **and** without tape-enabled in CI (new); gateway guard green; spec 0003 checklist all checked; interfaces has zero `dict` bodies (lint).

### Phase 4 — Frontend/backend separation (blocked on §5.3; 2–3 weeks once unblocked)

**Change:** build the thin host (reuse openalgo-charts pin or equivalent) **after** Phase 3 contract lands; generated TS client; workspace persistence through typed endpoints; order ticket via DTOs; replay transport over WS only.
**Delete:** legacy chart-related shaping in interfaces (depth reshaping, IST helpers, status-map presentation tier).
**Do NOT touch:** session cookie model (until auth redesign), `live_orders_enabled` semantics (server-enforced).
**Validation:** UI E2E (Playwright) driving paper session: order → fill → position visible; replay seek ≡ bar sequence; axe-core a11y pass; zero trading logic in client (dependency-cruiser boundary: client imports no domain math modules).

### Phase 5 — Performance & observability (ongoing)

**Change:** target the measured bottleneck (tick spine is one lock-serialised thread — 09-27 measured 44 µs/tick; re-measure with real payloads); `ThreadSafeReactiveBus` lane isolation under contention test; metrics: add `fills_missing_product_type_total`, `revised_bar_count`, `paper_book_zero_fill_sessions`, `pending_expired_total`, seal-duration histogram; alerts; deploy runbooks updated (`docs/operations/`); tracing spans on the order path (OpenTelemetry; `observability/tracing.py` exists — wire it end-to-end).
**Delete:** stub-based benchmark payloads; ad-hoc metrics counters duplicated in runtime vs observability.
**Do NOT touch:** single-thread determinism of the spine — parallelize only at boundaries (fetch, indicator batch, DuckDB analytics) with the order path untouched.
**Validation:** order-path p99 under load ≥ baseline; zero parity drift; dashboards reviewed by an operator; one live-paper dress rehearsal.

---

## 10. Final deliverables

### 10.1 Architecture diagrams (textual)

```
┌──────────────────────────── TRADEX v4 (target) ─────────────────────────────┐
│  [absent: UI host] ── typed OpenAPI/WS ──► interfaces (edge: auth,csrf,env) │
│                                                    │                        │
│                          application (use-cases: place/backtest/scan/ws)    │
│                                   │ ports                                   │
│   ┌───────────────────────────────┼────────────────────────────────┐        │
│   │ domain/strategy/execution core│  projections: TradingCache,     │        │
│   │  ExecutionEngine ─ FillSource │  EventStore fold, Portfolio     │        │
│   │  CashLedger/PositionAccountant│                                  │        │
│   └───────────────┬───────────────────────────────┬──────────────────┘        │
│   ports: Feed Clock IndicatorEngine EventStore BrokerAdapter PositionSource   │
│   ┌───────────────▼───────────────────────────────▼────────────────┐          │
│   │ infra: brokers(dhan/upstox/paper) gateway parquet duckdb sqlite │          │
│   └─────────────────────────────────────────────────────────────────┘          │
│  config ──► every layer (schema-validated at boot)                             │
│  observability ──► every layer (metrics, tracing; domain stays silent)          │
└──────────────────────────────────────────────────────────────────────────────┘
```

```
Golden-parity oracle flow:
  107 fixtures ──► analytics (batch) ──┬─► strategy-parity gate
                                       ├─► stream-incremental gate (new)
                                       └─► wire-shape gate (spec 0003)
  tape (record) ──► SessionBarAggregator ──► signals ≡ backtest tape replay   [P0.1]
```

### 10.2 Component responsibilities (deltas from today, not a rewrite)

| Component | Owns | Must NOT own |
|---|---|---|
| domain | types, FSM, position math, serialization, calendar | I/O, clock, mode |
| application | place/cancel/backtest/scanner use-cases | wiring, policy |
| execution | AC-22 spine, OMS, idempotency, accounting | strategy, feed |
| strategy | sizing, scanners, engine seam | broker, venue |
| replay/market_data | tape store, loaders, CA | trading side effects |
| runtime | boot (composition), session day-lifecycle, feeds | policy |
| interfaces | transport: auth, envelope, versioning | shaping beyond DTOs |
| gateway | external data plane (product 2) | order placement, shared internals |
| observability | metrics registry, tracing | business meaning |
| config | schema, secrets check | enforcement timing |

### 10.3 Anti-patterns found (named, with exhibits)

1. **Unwired-safety anti-pattern** — capability built and tested but invoked nowhere (`DurableFillDedup`, `assert_runtime_secret_permissions`, `SessionBarAggregator.flush`, `mark_filled`).
2. **Mode-fork thin veneer** — same engine class, different wiring that silently changes semantics (analytics=None; pending expiry; sizer equity; fees default).
3. **Replay-that-cannot-trade** — a second path wearing the name of the real thing; no negative contract test.
4. **Speculative seam hollowness** — `application/` layer with nothing in it; ceremony over value.
5. **Guard-commit regression** — cleanup-wave gains undone by a fast-follow feature commit; CI/local gate red at HEAD unnoticed.
6. **Docs-as-truth** — architecture docs asserting code facts that code contradicts (`account_sizer` "unwired"); stale counts ossify (add the doc-citation linter).
7. **Presentation in transport** — chart-tier status mapping, depth reshaping, IST helpers in `interfaces/`.
8. **Silent wall-clock** — `datetime.now` defaults in domain objects (`Quote`, `Fill`, `OrderRequest`) making "determinism" aspirational rather than enforced.
9. **Dual-policy naming** — same function name, opposite semantics, both "correct" arguments (the two secret-permission functions).
10. **Fail-open by default** — dev-fail-open auth reaching environments that are not obviously dev (loopback caveat only in error text).
11. **Gates that don't gate** — workspace member imported by nothing, covered by no CI coverage flag, in no clean-install smoke, in no predeploy import list (`tradex_gateway`, `duck_analytics`): the packaging gate passes because nobody wired the package into it.
12. **Second composition roots** — `gateway/brokers.py:162–180` re-composes the broker graph alongside `runtime/startup.py`; config drift between two "correct" builders is guaranteed eventually.
13. **Consolidated-then-redefined** — a deletion locked in by a guardrail test, reborn in the next feature commit; the guard exists precisely for this and is currently red.

### 10.4 Before vs after (one-liner per axis)

| Axis | Before (2026-11-06 HEAD) | After (target) |
|---|---|---|
| Frontend | deleted; no consumer | thin pure-consumer host; contract-generated |
| API bodies | bare `dict` everywhere | 100% Pydantic DTOs + OpenAPI + CI diff-gate |
| Error envelope | two dialects | one `{error:{code,message,retryable?}}` |
| Indicator path | batch goldens vs ad-hoc per-strategy fallback | one incremental engine, all modes, golden-oracle |
| Bar final close | missing/swallowed | `BarClosed` explicit, flushed at 15:30, sealed |
| Revision semantics | `RevisedBar` broken/ignored | deleted or first-class + tested freeze |
| Paper fill | wall-clock, two disagreeing paths | injected clock, one book matcher |
| Day lifecycle | none (running/stopped) | PRE_OPEN→OPEN→SEAL→POST_CLOSE state machine |
| Gateway | red guard, undocumented | green, folded windows, docs+CI, product-explicit |
| Docs | stale counts, wrong "facts" | citation-linted architecture doc + generated counts |

### 10.5 Non-negotiable coding rules for future development

1. One path to the venue (`ExecutionEngine.submit()`); the bus never carries orders.
2. Every fill is deduped by `fill_id`/fingerprint **before** it touches the book; venue is truth; local reject ⇒ adoption question, never packet loss.
3. Domain is frozen, `Decimal` for money, UTC-aware timestamps, no wall clock, no mode, no broker, no UI.
4. A change that forks backtest vs paper vs live is a bug unless it lands in `DIVERGENCE.md` with owner+expiry and a parity test asserting the fork's exact boundary.
5. No new `dict` on the wire; no route without DTOs; no two envelopes; no `datetime.now` outside infra/boot; every env secret read at boot with permission assert.
6. A guardrail test red at HEAD is a release blocker — feature commits may not add bypass debt; ratchets only shrink.
7. Capability-loud adapters: unsupported ⇒ typed error, never silent degradation; sells are never blocked by risk.
8. Deletion beats deprecation: two implementations ⇒ one is deleted in the same PR that introduces the second consumer.
9. Every commit that adds a route adds its contract test; every commit that adds a strategy adds its golden+parity entry; every commit that adds a metric names it in the catalog.
10. When this document and the code disagree, **the code wins and this document gets fixed in the same PR** — doc-as-fact is the bug, not the code.

### 10.6 What to do Monday morning

1. Make the guard green (delete `_tf_seconds`, call `bucket_seconds`). Commit.
2. Wire the boot permission assert (§6.1.6). Refuse, don't repair.
3. Land the two P0 parity tests (late ticks / analytics wiring) as red tests with a fix plan.
4. Enforce `live_orders_enabled` server-side; add WS Origin check; `Secure` cookie behind TLS flag.
5. Schedule the gateway carve-out decision (fold §6.4 vs spin-out repo) — before it accrues a user.

---

## Appendix A — Files read for this review

- docs: `ARCHITECTURE.md`, `audits/2026-09-27-full-platform-review.md` (prior art), `design/2026-09-13-frontend-integration-map.md`
- source: `domain/{market,execution,value_objects,serialization}.py`, `reactive/bus.py`, `interfaces/{fastapi_app,models,routes/*}` (via survey), `gateway/*`, `execution/{engine,fill_sources,idempotency}` (via surveys), `strategy/core/engine.py` (via survey), `replay/backtest.py` (via survey)
- runs: `pytest tests/` (1 failed, 524 passed), `pytest trading/tests/parity trading/tests/contracts` (170 + 196 passed)
- graphify: `graphify-out/` updated 2026-11-06 (code-only); used for orientation, not as evidence.
