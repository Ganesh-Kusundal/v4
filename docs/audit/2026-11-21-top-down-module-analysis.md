# Top-Down Module Analysis — Pass 1 (2026-11-21)

**Branch:** `analysis/top-down-review-2026-11-21` · HEAD `e83d8e78`
**Method:** read the composition root outward — `domain` → `execution` → `strategy` → `runtime` → `interfaces`, then the supporting packages (`analytics`, `replay`, `market_data`, `brokers`, `config`, `reactive`, `gateway`, `application`). Every finding below carries file:line evidence from this checkout; nothing here is inherited from a doc without re-derivation.
**Companion docs:** `docs/audit/2026-11-06-principal-architecture-review.md` (full review), `docs/audit/2026-11-21-principal-review-delta.md` (post-remediation delta).

---

## 0. Coverage ledger (honest)

| Scope | Size | This pass |
|---|---|---|
| Production source (`*/src/**.py`) | 328 files / ~86k LOC | Spine read in depth; leaf modules sampled by symbol |
| `trading/tests/` (legacy behavioral suite) | 613 `.py` files | Not read; only the parity test relevant to a finding was run |
| Per-package tests | 133 files / ~27.7k LOC | Sampled |

Explicitly read this pass: `domain/{__init__,execution,market,enums,market_calendar,timezones,clocks,timeframe}.py`, `execution/{engine,idempotency,__init__}.py`, `strategy/core/engine.py`, `replay/backtest.py`, `analytics/incremental.py`, `runtime/{startup,session_impl,bar_aggregator}.py`, `interfaces/routes/{chart,stream_indicators}.py`, `config/schema.py`, plus symbol-level greps across all 15 packages.

---

## 1. Layer map (measured)

| Layer | Package | Files | LOC | Verdict |
|---|---|---:|---:|---|
| Kernel | `tradex_domain` | 36 | 6,324 | Strong. Frozen dataclasses, Decimal money, FSM, zero inward deps. Two defects (§3.1, §3.2). |
| Kernel | `tradex_observability` | 3 | 680 | Fine. Tracing wired into the engine. |
| Infra | `tradex_reactive` | 4 | 782 | Fine; one dead durable mode (`SQLEventLog`). |
| Infra | `tradex_config` | 5 | 1,109 | Strong fail-closed validation. |
| Adapters | `tradex_brokers` | 65 | 13,291 | Largest surface; capability tables + contested-symbol ranking correct. |
| Engine | `tradex_execution` | 24 | 9,122 | The real OMS. Single `submit()` door, hash-chained store, durable dedup now wired. |
| Engine | `tradex_strategy` | 37 | 5,500 | Shared seam; analytics now wired lazily for all modes. |
| Engine | `tradex_analytics` | 43 | 14,338 | Incremental indicators landed; batch oracle intact. |
| Data | `tradex_market_data` | 12 | 3,888 | Clean; `ParallelHistoryFetcher` is single-broker by design. |
| Data | `tradex_replay` | 7 | 3,265 | Backtest is the deterministic reference implementation. |
| Compose | `tradex_runtime` | 24 | 10,348 | Composition root + live feed services. Typing weakest here. |
| Edge | `tradex_interfaces` | 37 | 9,909 | Typed contracts landed; still carries presentation policy. |
| Edge | `tradex_gateway` | 27 | 3,600 | Second product; now in CI + predeploy gate (fixed since 11-06). |
| Use-cases | `tradex_application` | 2 | 302 | Hollow seam — 3 use-cases in 302 LOC. |
| Leaf | `tradex_http` | 2 | 21 | Correct minimal leaf. |

---

## 2. Module ledger — findings

### 2.1 `tradex_domain` (kernel)

**Good.** `Order._LEGAL_TRANSITIONS` (`execution.py:25–66`) encodes UNKNOWN as non-terminal with a documented reason; `OrderRequest.__post_init__` refuses non-positive quantity; `Depth.__post_init__` validates book ordering at the boundary; `BracketOrderRequest` refuses an invalid SL/entry/target geometry *before* any reservation.

**F1 — P0, correctness: `is_market_open()` defaults to naive local wall clock.**
`domain/src/tradex_domain/market_calendar.py:119` — `dt = datetime.now()` (no tz), then compares `dt.time()` against `MARKET_OPEN`/`MARKET_CLOSE`, which are **IST** wall-clock bounds. On any host not running IST (CI is UTC), the gate is wrong by 5h30m. The only production caller passes an explicit `now` today (`brokers/src/tradex_brokers/paper/adapter.py:1013`), so this is latent — but it is the exact shape of "works on the developer's laptop, wrong in the container". Fix: `dt = datetime.now(IST)`; delete the default entirely and make `dt` required, so no caller can silently inherit host time.

**F2 — RETRACTED: the two IST objects are not drift.**
`market_calendar.py:26` (`IST = timezone(timedelta(5,30))`) vs `timezones.py` (`IST_ZONE`). An earlier pass of this audit called the pair a duplicate. Reading the module's own justification (`market_calendar.py:17–26`) shows the fixed offset is load-bearing: `ZoneInfo.utcoffset(None)` returns `None`, and the session-grid arithmetic asks the zone for its offset without a datetime, so the `ZoneInfo` form cannot serve that call site. Two representations of one zone, chosen per call site and documented. **Do not collapse them**; the open action is only to make `timezones.py:22–26` point at this rationale so the next reviewer does not re-file it.

**F3 — P2: `datetime.now(UTC)` as a field default in domain DTOs.**
`events.py:41`, `execution.py:286`, `market.py:135`, `market_builder.py:109`, `protocols.py:260`. Not a determinism bug *if* every producer supplies the timestamp — but a domain object that self-stamps wall-clock time is a determinism hole waiting for the one producer that forgets. Prefer required timestamps in the pipeline path; keep the default only for ergonomic construction in tests.

### 2.2 `tradex_execution` (OMS)

**Good.** One intake (`ExecutionEngine.submit`), `IdempotencyInflight` vs `IdempotencyDuplicate` vs `IdempotencyKeyReuseMismatch` are distinct failures mapped to distinct responses; `_request_fingerprint` is operation-scoped (`engine.py:117–150`) so a submit key cannot be replayed as cancel.

**F4 — FIXED since 11-06: `DurableFillDedup` is now wired.** `runtime/src/tradex_runtime/startup.py:721` and `engine.py:282` construct it. Remove this from every "dead code" list; the 11-06 table is stale on this row.

**F5 — P1, dead code: `fingerprints_agree()` has zero callers.** `execution/idempotency.py:320` — only mentioned in its own docstring at `:280`. Two fill-fingerprint implementations therefore remain pinned only by convention. Either call it from a guard test that pins both implementations against the same table, or delete it. Prefer the guard test: it converts a comment into a gate.

**F6 — P2: `engine.py` is 1,716 LOC.** It is the one file where every money path converges, which justifies size, but `_TERMINAL_STATUSES`, bracket detection, fingerprinting and the night-path affordability re-check all live in it. Extraction candidates, in order of independent value: fingerprint → `domain/fill_fingerprint.py` (already exists), bracket detection → `execution/brackets.py`.

### 2.3 `tradex_strategy`

**Good.** `register()` (`core/engine.py:713–724`) now creates an `AnalyticsRuntime` lazily when a strategy declares `required_indicators`, closing the paper/live indicator fork at the seam rather than in each strategy. `stop_strategy` is isolation-correct: gates synchronously, disposes only that strategy's subscriptions, cancels working orders, and **deliberately does not flatten positions** (`core/engine.py:800–839`) with the reason stated.

**F7 — P2: implicit global analytics.** The lazy creation means the analytics instance is engine-scoped, not strategy-scoped. Two strategies on different timeframes now share one `AnalyticsRuntime` whose key includes the timeframe, so this is correct today — but it is an undocumented invariant. Add a test asserting two strategies × two timeframes get independent series, because the failure mode (cross-contaminated indicator state) is silent.

### 2.4 `tradex_runtime` (composition + live services)

**F8 — P0, correctness: `RevisedBar` reaches the frame publisher as the wrong type.**
`bar_aggregator.py:650–670` emits `RevisedBar(frame=BarFrame(...), reason="late_tick", ...)`. The publisher registered at `:942–951` is typed `def publish(frame: BarFrame)` and immediately reads `frame.closed`, then `self._candle(frame)` which reads `frame.instrument`. A `RevisedBar` has `.frame`, not `.closed`/`.instrument`, so **every late-tick revision raises `AttributeError`** inside the bus delivery, where isolation swallows it. Net effect: late-tick corrections never reach strategies or the chart, and the failure is invisible in production. Confirmed by the repo's own strict xfail: `.venv/bin/python -m pytest trading/tests/parity/test_sba_late_tick_parity.py -q` → `1 xfailed` (reason `A4: RevisedBar unhandled`).
Fix: unwrap in the publisher — `if isinstance(frame, RevisedBar): frame = frame.frame` — and route the revision through a distinct `BarRevised` bus event so a consumer can choose to re-book or ignore it. Then flip the xfail to a real assertion.

**F9 — P1, type safety at the composition root: `TradingSession.__init__` takes 14 `object | None` params.**
`session_impl.py:75–102`. `mark_to_market`, `metrics`, `feed_monitor`, `feed_recovery`, `event_store`, `validation_report_store` etc. are all `object`, so every downstream `getattr` is unchecked and a wiring mistake surfaces as an `AttributeError` on the first tick, not at boot. `startup.py` contains 59 `Any` occurrences. This is the weakest typing in the repo, at the one place where wrong wiring is most expensive. Fix: introduce a frozen `SessionServices` dataclass with real types and pass that.

**F10 — P2: `startup.py` is 1,743 LOC and imports 20+ packages.** Acceptable as *the* composition root; reject any growth beyond wiring. The `_compose_live_feed` helper (`:120–150`) is the right pattern — continue extracting per-mode composers rather than adding branches to `boot`.

### 2.5 `tradex_interfaces` (edge)

**Good.** Routes are Pydantic-typed now — `grep -rn "-> dict|body: dict" interfaces/src/tradex_interfaces/routes/*.py` returns **nothing**. The live-order gate is server-enforced (`routes/orders.py:210`), not advisory. Response contract is versioned (`2ca252c6`).

**F11 — P1, presentation policy in the transport layer.** `routes/chart.py` holds `_ORDER_STATUS_MAP` (domain status → chart trade-tier status, `:74+`), and `routes/stream_indicators.py` is **959 LOC** containing a process-global `IndicatorTailCache`, incremental indicator compute, tick throttling, and thread dispatch. The chart UI it was written for no longer exists. This is a compute+cache engine living inside an HTTP router: it cannot be unit-tested without the web framework, and it will be re-implemented by the next UI. Move the engine to `tradex_analytics` (where `incremental.py` already lives) and leave the route as a thin adapter.

**F12 — P2: `chart.py` is 1,222 LOC.** Same cause. Split by resource (history / indicators / backtest / scanner) when F11 moves the engine out.

### 2.6 `tradex_analytics`

**Good.** `incremental.py` is the correct design: stateful, O(1), each class documented as matching its batch counterpart element-for-element including warmup, with parity asserted in tests. This is the single biggest correctness win since 11-06, because live and backtest can no longer drift by a rounding decision.

**F13 — P2: `views.py` still keeps a version-keyed batch fallback** (`views.py:116–171`). Correct as a fallback, but the incremental path should be preferred for every registered indicator, not only SMA/EMA/RSI/Bollinger/MACD. Track which indicators still land on `_compute`.

### 2.7 `tradex_replay`

**Good — treat as the oracle.** `_stop_gap_price` (`backtest.py:97–130`) prices a triggered stop at the worse of the bar's own adverse extreme and a modelled gap, so it can neither invent a loss nor beat a printed price. `DEFAULT_STOP_GAP_BPS = 25` is labelled "a stated assumption, not a measurement". This is the standard the rest of the codebase should be held to: numbers carry their epistemic status.

**F14 — P2: replay controller duplicates aggregation control.** `replay_controller.py` calls `aggregator.flush()` at five sites (`:763, :851, :863, :893, :924`). Each is a place where replay's notion of "end of bar" can diverge from live's. Consolidate into one `close_bar()` on the aggregator so the five call sites become one contract.

### 2.8 `tradex_market_data` / `tradex_brokers`

**Good.** `ParallelHistoryFetcher` is honestly single-broker; contested-symbol resolution ranks claims and logs the winner; Dhan tick-size now normalizes paise → rupees (`common/instrument_loader.py:166+`).

**F15 — P2: `brokers` is 13,291 LOC across 65 files** — the largest package, with `common/` holding 24 modules (auth, transport, retry, cache, ws, endpoints, rate table, token lifecycle). Most are legitimate shared infrastructure. The risk is that `common/` becomes a second domain: keep broker-neutral *mechanism* there and broker-specific *policy* in `dhan/`/`upstox/`.

### 2.9 `tradex_reactive`

**F16 — P2, dead mode: `SQLEventLog` has zero production callers.** `reactive/src/tradex_reactive/event_log.py:65`; only `reactive/tests/test_bus_replay_sql.py` constructs it. `startup.py` never passes `event_log=`, so the "durable bus" is a tested-but-unused mode. Decide: wire it (and pay the write cost) or delete it and its test.

### 2.10 `tradex_application`

**F17 — P2, hollow seam.** 2 files / 302 LOC. It correctly owns cancel/modify/price parsing and is wired into `routes/orders.py`, but `submit` still orbits the engine directly. Either grow it to own all three order use-cases end-to-end, or document it as a naming seam and stop it accreting logic ad hoc.

### 2.11 `tradex_gateway` / `duck_analytics`

**FIXED since 11-06:** the gateway guard is green, and it is now present in `scripts/predeploy_gate.sh:39` and covered in CI (`quality-gate.yml:87` `--cov=tradex_gateway --cov=duck_analytics`, plus import smoke at `:133`). The 11-06 "ships but gates nothing" finding no longer holds.

**F18 — P2, still two products in one repo.** `duck_analytics` has root `pyproject.toml` wiring at nine places (`:164–307`) but no workspace importer. Keep the current CI coverage, and add one documented launch path per service or spin them out.

---

## 3. Cross-cutting audit

### 3.1 Duplicate logic (verified)
| Kind | Where | Status |
|---|---|---|
| Timeframe seconds | `domain/timeframe.bucket_seconds` | **Consolidated**; `_tf_seconds` only in the guard test that forbids it |
| IST zone | `market_calendar.IST` vs `timezones.IST_ZONE` | **Not drift** — deliberate, reason documented (F2 retracted) |
| Fill fingerprint | `idempotency` vs `sqlite_event_store` | **Open**, unpinned (F5) |
| Session bounds (09:15/15:30) | `market_calendar` owns | Good; literals elsewhere are prose only |
| Bar-close trigger | `replay_controller` ×5, `bar_aggregator.flush` | **Open** (F14) |
| Indicator math | batch vs incremental | **Deliberate parity**, asserted — keep |

### 3.2 Dead / test-only code (verified, current)
| Item | Callers | Action |
|---|---|---|
| `RevisedBar` | aggregator + tests only | **Wire it** (F8) — it is a correctness feature, not dead weight |
| `SQLEventLog` | tests only | Wire or delete (F16) |
| `fingerprints_agree()` | none | Convert to a guard test (F5) |
| `DurableFillDedup` | **wired** (`startup.py:721`) | Remove from any dead list |
| `_backpressure_triggered` | **gone** | Guard already enforces it |

### 3.3 Coupling
`interfaces` → 12 internal packages (composition leaf, expected). `runtime/startup` → 20+ (composition root, expected). The unhealthy edges are: `interfaces` owning a compute engine (F11) and `session_impl` accepting `object` (F9).

### 3.4 Replay vs live parity — current state
| Divergence | State |
|---|---|
| Indicator path | **Closed** (lazy `AnalyticsRuntime` in `register`) |
| Late-tick revision | **Open, broken** (F8) |
| End-of-bar close | Closed in replay (5 flush sites); live relies on teardown/flush caller |
| Clock | Injected in backtest; paper still has wall-clock defaults in `paper/adapter.py:158–162` — verify before live |
| Fees / pending expiry / sizer equity | Intentional, documented in `docs/DIVERGENCE.md` — needs owner + expiry |

---

## 4. Prioritized action queue

**P0 — correctness, do before any live session**
1. F8: unwrap `RevisedBar` in `_frame_publisher`, emit a distinct `BarRevised`, flip the strict xfail.
2. F1: make `is_market_open(dt)` timezone-aware and remove the naive default.

**P1 — structural, this phase**
3. F9: typed `SessionServices` instead of 14 `object` params.
4. F11: move the indicator stream engine out of `routes/` into `tradex_analytics`.
5. F5: guard test pinning both fill fingerprints.
6. ~~F2: collapse `market_calendar.IST` onto `IST_ZONE`.~~ **Retracted** — the fixed offset is load-bearing; see F2.

**P2 — hygiene**
7. F16 wire-or-delete `SQLEventLog`; F17 grow-or-document `application`; F14 one `close_bar()` contract; F13 close the remaining batch-fallback indicators.

---

## 5. Verification run

```
.venv/bin/python -m pytest tests/test_guardrails.py -q          → 17 passed
.venv/bin/python -m pytest trading/tests/parity/test_sba_late_tick_parity.py -q → 1 xfailed (A4)
grep -rn "-> dict|body: dict" interfaces/.../routes/*.py        → no matches
grep -rn "DurableFillDedup" … | grep -v test                   → startup.py:721, engine.py:282
grep -rn "ISETrading…|is_market_open" …                        → only paper/adapter.py:1013 passes `now`
```
