# TradeX v4 — Living Design State

**Purpose:** Single source for the platform's current design posture. Update after every architectural commit.
**Source of truth for:** invariants, known gaps, in-flight work, sequencing.
**Companion:** `docs/reviews/architecture-design-review-2026-08-29.md` is the *baseline* design review (what was true at one point in time); this file is *current state*.

---

## Invariants (do not regress)

The 10 design invariants from the baseline review. If a change threatens one, escalate to Architecture Review.

1. One order path (`ExecutionEngine._run_pipeline`).
2. Money is `Decimal`.
3. Frozen domain (`@dataclass(frozen=True, slots=True)`).
4. Capability-loud adapters (`require_capability` + `Literal["supports_*"]`).
5. Single composition root (`runtime/startup.boot()`).
6. One resilience stack (`ProviderHttpClient` → `ResiliencePipeline`).
7. Fail-closed live gate (`confirm=True`, `_allow_order_operations`).
8. Parity evidence (CI parity gate).
9. Capability drift fails CI.
10. Single-writer live (`SingleWriterLock`).

---

## Known gaps (from baseline review + 2026-08-29 principal-architect verification)

Severity tags: 🔴 critical · 🟠 high · 🟡 medium · 🟢 low.
**C1–C5, H1–H8, M1–M7** are findings from the principal-architect review, every claim verified against code at cited line numbers.
**G1–G17** are findings from the baseline architecture review.

### Sprint 0 — pre-flight (must complete before any other work)
- 🟢 **G17** `pyproject.toml` files mostly empty; `uv.lock` is a 4-line stub. CI runs `uv sync --frozen` and gets nothing. *This is the prerequisite for every other CI-gated fix; without it, no other change is verifiable end-to-end.*

### Sprint 1 — live safety (REVISED 2026-08-29, principal-architect review)
The principal-architect review surfaced 5 criticals I missed in the baseline. Reordering by leverage:

- 🔴 **C1** Wire `CashLedger` into the reactive `ExecutionEngine`. Currently only `BacktestEngine` uses it; a buy in paper/live passes risk checks (per-order notional OK) and the broker rejects async, after positions may be partially projected. **Highest financial-leverage fix in the entire plan.**
- 🔴 **C2** Move reconciliation before `session.start()`. Currently `startup.py:456 session.start()` then 461-503 reconciliation; a strategy reacting during the race window trades on a diverged book.
- 🔴 **C3** Wrap non-live `_boot_tail` in try/except with `session.stop()`. The handwritten rollback at `startup.py:231-256` is live-only; non-live failure leaks the engine.
- 🔴 **C4** Defensive tz check in `RiskManager.check`. `engine.py:354-360` mixes tz-aware (default) and naive (backtest) timestamps; docstring warns, code never validates. `TypeError` or silent rate-limit drift.
- 🔴 **C5 / G1p** Wire existing `BoundedReactiveBus` back-pressure hooks (`max_queue_size`, `on_backpressure`) — currently no-ops per `bus.py:130-132`. Add `bus.messages.dropped` and `bus.drain.exceeded` counters.
- 🟠 **H1** `MetricsRegistry._Counter.inc()` non-atomic; undercounts under concurrency.
- 🟠 **H2** `MarketFeed._instruments` / `_depth_instruments` mutated without a single lock; only `_last_tick_lock` exists, on a different field.
- 🟠 **H5** `modify()` does not re-run `risk_manager.check()`. An order within limits at entry can be modified to exceed `max_position_value`.
- 🟠 **H7** `ErrorOccurred` published (`engine.py:517, 524, 533`) but never subscribed.
- 🟠 **H8** `CandleReceived` defined in `events.py` but never published or subscribed — dead code in the public surface.
- 🔴 **G3** (carry from baseline) No test for the live-fill race between `_run_pipeline` and `_apply_fill`. Load-bearing comment at `engine.py:812` documents the invariant; no test pins it.

### Sprint 2 — durability
- 🔴 **R1** Replace in-memory `deque(maxlen=10_000)` in `ThreadSafeReactiveBus` with SQLite append-only event log. Replay on boot.
- 🟠 **G5** `ReactiveStrategyEngine._pending` linear scan. `dict[InstrumentId, list]` + `pending_max_age` + last-bar-of-dataset test.
- 🟠 **H3** Document or raise ceiling on `_applied_fills` LRU eviction (50k). Comment at `engine.py:495` says "add latency histograms when dashboard needs them."
- 🟠 **H4** SQLite store is mirror-only; never read during active trading. Mid-session crash loses recent fills.
- 🟠 **H6** `LiveFillBridge._engine_order_id` O(n) scan of `cache.all_orders()` per order-stream event. With 500 open orders + busy fill stream, this is the bottleneck.

### Sprint 3 — correctness at scale
- 🟠 **G4** `BaseBroker` has ~30 pass-throughs. Refactor to a generated wall.
- 🟠 **G6** Promote `AnalyticsEngine` to a first-class `IndicatorRegistry`. Co-locate goldens with specs.
- 🟠 **G8** SDK service layer is a thin pass-through. Remove it or give it a real job.
- 🟡 **M1** `FeeCalculator._calculate_legacy` GST base differs from canonical — `fees.py:135` explicitly flags for removal.
- 🟡 **M2** `cancel()` does not release idempotency reservation. `MemoryIdempotencyGuard._reserved` grows on every cancelled order whose cid was reserved in the pipeline.
- 🟡 **M7** Global `_ACTIVE_WRITER_LOCK` (`startup.py:43`); two live sessions overwrite.

### Sprint 4 — design hygiene
- 🟡 **G10** `trading/` is 25.6k LOC across 10 modules. Split into `trading_core / _datalake / _strategy / _runtime / _interface / _sdk`. Only when team size justifies.
- 🟡 **G11** `analytics/` flat dir of 30 modules. Sub-folders by family.
- 🟡 **G12** `replay/` mixes driver, walk-forward, optimization, synthetic ticks. Split.
- 🟡 **M3** Post-dispose publish silently enqueues to a completed Subject.
- 🟡 **M4** `broker.close()` called twice (`RuntimeContext.close` and `TradingSession.stop`); relies on broker `close()` being idempotent, not enforced.
- 🟡 **M5** `RiskManager._session_date: Any` — mypy can't catch tz-mismatches.
- 🟡 **M6** `BrokerFillSource.submit` wraps raw string to `OrderId` without validation.

### Sprint 5 — observability
- 🟠 **R2** Partition the bus: `OrderPipeline` / `MarketData` / `Diagnostics`.
- 🟢 **G14** `MetricsRegistry` is in-process and never exported. Wire Prometheus exposition or drop.
- 🟠 **R3** Promote `IndicatorSpec`/`IndicatorRegistry` to first-class contracts.

### Backlog
- 🟡 **G7** `ScannerEngine._history` is snapshot-only. Stream consumer.
- 🟠 **G9** `services/duckdb-analytics` re-implements `ScannerEngine` with no shared contract.
- 🟡 **G13** Frontend has 11 `if (!resp.ok) throw new Error(...)`; backend has no typed-error contract.
- 🟢 **G15** Update `docs/ARCHITECTURE.md` after G10 lands.
- 🟢 **G16** `tests/test_import_boundaries.py` — keep, update when G10 lands.

---

## In-flight

### C1 — wire `CashLedger` into the reactive path
**Test-first plan (highest-leverage fix in the plan):**
- ✅ RED: `trading/tests/execution/test_risk_cash_check.py::test_buy_rejected_when_cash_insufficient` — bind a `CashLedger(cash=Decimal("100000"))`, submit a buy at `mark=Decimal("2500"), qty=100` (₹2,50,000 notional), expect rejection.
- ✅ RED: `trading/tests/execution/test_risk_cash_check.py::test_sell_uses_proceeds_credit` — bind a `CashLedger(cash=0)` with a long position; a sell that brings cash positive is allowed.
- ✅ RED: `trading/tests/execution/test_risk_cash_check.py::test_buy_within_cash_passes` — a buy that fits within cash must still pass (no regression).
- ✅ RED: `trading/tests/execution/test_risk_cash_check.py::test_no_cash_provider_means_no_cash_check` — backward compat: no provider bound ⇒ no gate.
- ✅ RED: `trading/tests/execution/test_risk_cash_check.py::test_existing_notional_gate_still_works` — `max_order_value` gate still applies when cash is bound.
- ✅ GREEN: `RiskManager.bind_cash_provider(provider)` + cash check in `check()`. Done in `d6f65ea`.
- ✅ VERIFY: 5/5 new tests pass; full suite 2745 passed, 0 failed.
- 🟡 FOLLOW-UP: wire `bind_cash_provider` from `startup.boot` so paper/live sessions actually enforce the gate. Paper source: `Account.cash` from `broker.get_account()`. Live source: broker `fund_limits()`. NEXT.

**Acceptance (core):** all 5 tests green; existing risk tests still pass; an unpriced MARKET order in live mode with `reject_unknown_market_value=True` rejects with `insufficient_cash` (no price to compare).

**Acceptance (full):** ✅ DONE (`df61073`). `RiskConfig.cash_provider` added; `boot()` binds it for paper/live sessions. 2 new tests, 7/7 total in this gap, full suite 2747 passed.

### C2 — reconcile before session.start()
**Test-first plan:**
- ✅ RED: `trading/tests/runtime/test_boot_reconcile_before_start.py::test_critical_drift_prevents_session_from_reaching_ready` — boot a live session; assert that if reconciliation produces a CRITICAL drift, the session never reaches READY.
- ✅ RED: `trading/tests/runtime/test_boot_reconcile_before_start.py::test_no_drift_lets_session_reach_ready` — assert no drift ⇒ session reaches READY (regression guard).
- ✅ RED: `trading/tests/runtime/test_boot_reconcile_before_start.py::test_low_drift_does_not_block_ready` — only HIGH/CRITICAL drift blocks; LOW/MEDIUM may proceed.
- ✅ GREEN: extracted `_run_startup_reconciliation(broker, engine) -> bool` (True iff reconciliation *newly* tripped the kill switch). The boot calls it before `session.start()`; if it returns True, skip `session.start()` and skip the master scheduler. Done in `29e527e`.
- ✅ VERIFY: 3/3 new tests pass; pre-existing `test_kill_switch_default_from_config` regression test still passes (config-set kill switch still goes READY).
- ✅ FULL SUITE: 2750 passed, 0 failed.

**Acceptance:** ✅ DONE. A critical drift at startup now leaves the session in NEW state with the kill switch tripped; a caller can inspect `session.state`, `engine.kill_switch`, and the drift list before deciding to stop or reset.

### C3 — non-live `_boot_tail` rollback
**Test-first plan:**
- RED: `trading/tests/runtime/test_boot_rollback.py::test_non_live_boot_failure_releases_session` — paper mode with a deliberately broken fill source; assert `session.stop()` was called and `bus.dispose()` ran.
- RED: `trading/tests/runtime/test_boot_rollback.py::test_live_boot_failure_releases_writer_lock_and_disconnects` — live mode with a broken env; assert writer lock released and broker disconnected.
- GREEN: extract the existing live-mode rollback into a `_safe_teardown(session, broker, writer_lock)` helper; wrap the non-live `_boot_tail` call in try/except that calls the helper.
- REFACTOR: both branches now share the same teardown helper.

**Acceptance:** both tests green; no resource leak in non-live failure paths.

### C4 — defensive tz check
**Test-first plan:**
- RED: `trading/tests/execution/test_risk_tz.py::test_mixed_tz_in_rate_window_raises_clearly` — first call with aware datetime, second call with naive; expect `ValueError` with the documented message, not `TypeError` deep in `total_seconds()`.
- RED: `trading/tests/execution/test_risk_tz.py::test_naive_then_aware_consistent` — first call naive, all subsequent naive; no error.
- GREEN: at the top of `check`, when `now` is provided, assert `now.tzinfo is None` matches the awareness of `self._recent_orders[0]` (if any); raise `ValueError` with a clear message.
- REFACTOR: extract `_validate_tz(now)` private method.

**Acceptance:** both tests green; existing backtest reproducibility tests still pass.

### C5 / G1p — wire bus back-pressure hooks
**Test-first plan:**
- RED: `trading/tests/reactive/test_bus_backpressure.py::test_dropped_message_emits_metric_and_error_event` — publish 11 messages to a slow subscriber, expect `bus.messages.dropped == 1` and an `ErrorOccurred` on the bus.
- RED: `trading/tests/reactive/test_bus_backpressure.py::test_drain_exceeded_emits_critical_log_and_metric` — install a subscriber that republishes forever, expect `bus.drain.exceeded` counter incremented and log.critical emitted.
- GREEN: minimal wiring in `ReactiveBus.subscribe` (drop on cap; emit `ErrorOccurred`) and `publish` (count overflow).
- REFACTOR: extract `BoundedReactiveBus` only if duplicated.

**Acceptance:** tests green; counter visible in `MetricsRegistry.snapshot()`; bus delivery never blocks longer than `_MAX_NESTED_DELIVERIES` iterations.

### H1 — `MetricsRegistry` lock
**Test-first plan:**
- RED: `trading/tests/runtime/test_metrics_thread_safety.py::test_counter_increment_is_atomic` — spawn 100 threads, each inc 1000 times; expect counter value == 100000.
- GREEN: `threading.Lock()` in `_Counter.inc` and `_Histogram.observe`; acquire once per call.

**Acceptance:** test green; metric value matches expected under concurrency.

### H2 — `MarketFeed` instrument lock
**Test-first plan:**
- RED: `trading/tests/runtime/test_market_feed_thread_safety.py::test_subscribe_unsubscribe_does_not_lose_quotes` — concurrent `subscribe` / `unsubscribe` while `_on_quote` is firing; assert no `KeyError`, no missed instrument.
- GREEN: single `threading.RLock` around `_instruments` and `_depth_instruments` reads and writes.

**Acceptance:** test green; no `KeyError` in 10k iterations.

### H5 — `modify()` re-runs risk
**Test-first plan:**
- RED: `trading/tests/execution/test_modify_risk.py::test_modify_exceeding_max_position_value_rejected` — order within limits, then modify to exceed `max_position_value`; expect `OrderRejectedError`.
- GREEN: in `modify()`, after the broker-side modify call, call `self._risk.check(request)`; if false, raise `OrderRejectedError("risk_check_failed")` and roll back the cache.

**Acceptance:** test green; existing `test_modify_*` tests pass.

### H7 — `ErrorOccurred` subscriber
**Test-first plan:**
- RED: `trading/tests/runtime/test_error_occurred_logged.py::test_pipeline_error_publishes_error_occurred_and_logs` — trigger a pipeline error; expect log line + counter.
- GREEN: in `_boot_tail` (live), add `bus.subscribe(lambda e: log.error("pipeline error: %s", e.error))` on `ErrorOccurred`; add a `bus.errors.total` counter.

**Acceptance:** test green; pipeline errors no longer silent.

### H8 — delete `CandleReceived`
**Ponytail check:** YAGNI. The codebase publishes raw `Candle` everywhere; `CandleReceived` is never used. Delete from `events.py` and `__init__.py`. The removal is a 2-line diff; no test is needed (the absence of usage is the test).

### G3 — test the live-fill race
**Test-first plan:**
- RED: `trading/tests/execution/test_engine_fill_race.py::test_apply_fill_idempotent_when_sync_path_already_filled` — synchronous pipeline marks FILLED and publishes `OrderFilled`; the live-fill bridge then re-publishes the same `OrderFilled`; engine must not double-apply.
- RED: `trading/tests/execution/test_engine_fill_race.py::test_apply_fill_idempotent_with_fill_id` — same scenario, `fill.fill_id` set, two distinct equal-lot partials both apply.
- RED: `trading/tests/execution/test_engine_fill_race.py::test_apply_fill_skips_rejected_order` — order was rejected before any fill; an inbound `OrderFilled` for that order id does not move the position.
- GREEN: fix any races surfaced; otherwise document the invariant.
- REFACTOR: consolidate the two code paths in `_run_pipeline` and `_apply_fill` into one idempotent `apply(fill)` method that takes a fingerprint.

**Acceptance:** all three tests green; the comment at `engine.py:812` is replaced by code that doesn't need the comment.

### G2 — margin + per-strategy budget
**Test-first plan (largest of the three):**
- RED: `trading/tests/execution/test_risk_manager.py::test_margin_check_rejects_when_exceeds_buying_power` — bind a `MarginProvider` returning ₹10L available, request a ₹50L order; expect rejection.
- RED: `trading/tests/execution/test_risk_manager.py::test_per_strategy_budget_isolates_allocations` — two strategies, each with a ₹5L budget; strategy A's order exhausting its budget does not block strategy B.
- RED: `trading/tests/execution/test_risk_manager.py::test_daily_loss_does_not_lock_exits` — daily loss at limit, an order that *reduces* the position is approved.
- RED: `trading/tests/execution/test_risk_manager.py::test_partial_close_of_short_is_reduction` — short position, a buy smaller than the short size is approved even at daily-loss limit.
- RED: `trading/tests/execution/test_risk_manager.py::test_live_default_rejects_unknown_market_value` — invert the default; live mode rejects an unpriced MARKET order.
- GREEN: introduce `RiskBudget`, `MarginProvider` protocol, and the `strategy_id` parameter on `RiskManager.check`.
- REFACTOR: pass `strategy_id` from `OrderRequest.tag` (`strategy_id@version`) or from the bus event; avoid the engine having to know about strategy identity.

**Acceptance:** all five tests green; existing risk tests still green; `RiskManager` constructor backward-compatible with the existing single-budget path.

---

## Process rules (from loaded skills)

From `using-superpowers`: invoke a relevant skill *before* any non-trivial action. Red flags: "I can check git quickly", "this is just a small fix", "the skill is overkill".

From `ponytail` (default: full intensity):
- The ladder: does it need to exist? → already in this codebase? → stdlib? → native feature? → installed dep? → one line? → only then minimum code.
- Bug fix = root cause, not symptom. Grep every caller before editing.
- Mark deliberate shortcuts with a `ponytail:` comment naming the ceiling and the upgrade path.
- Trivial one-liners need no test; non-trivial logic leaves one runnable check.

From `test-driven-development`:
- Iron law: NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST.
- Write code before the test? Delete it. Start over.
- Red → verify red fails for the right reason → green minimal → verify green all-clean → refactor.
- "I already manually tested" is a red flag; "tests after" is a red flag.

From `tradexv2-org`:
- Architecture first, then contracts, then tests, then implementation, then peer review, then integration validation, then workflow validation.
- No partially completed architectural branches.
- Final question: *Would an experienced trader trust and use this?*

---

## Update log

- 2026-08-29 — created; baseline = `docs/reviews/architecture-design-review-2026-08-29.md`.
- 2026-08-29 — Sprint 1 revised: principal-architect review surfaced 5 new 🔴 (C1–C5) and 8 🟠 (H1–H8) findings. Reordered by leverage; C1 is now the highest-priority fix.
- 2026-08-29 — C1 GREEN (`d6f65ea`): `RiskManager.bind_cash_provider` + cash check in `check()`. 5 new tests, 5/5 green, full suite 2745 passed. Follow-up: wire it from `startup.boot` so paper/live sessions actually enforce the gate.
- 2026-08-29 — C1 follow-up DONE (`df61073`): `RiskConfig.cash_provider`; boot binds it for paper/live. 2 more tests, 7/7 in this gap, full suite 2747 passed. **C1 fully closed.**
- 2026-08-29 — C2 DONE (`29e527e`): extracted `_run_startup_reconciliation`; runs before `session.start()`; refuses to start on a *new* critical-drift trip. 3 new tests, full suite 2750 passed. **C2 closed.**
