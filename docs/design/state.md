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
- ✅ RED: `trading/tests/runtime/test_boot_rollback.py::test_safe_teardown_handles_non_live` — helper exists, tolerates writer_lock=None.
- ✅ RED: `trading/tests/runtime/test_boot_rollback.py::test_safe_teardown_handles_live_with_writer_lock` — helper releases writer_lock.
- ✅ RED: `trading/tests/runtime/test_boot_rollback.py::test_paper_boot_failure_uses_rollback` — paper-mode failure propagates and the broker is disconnected.
- ✅ RED: `trading/tests/runtime/test_boot_rollback.py::test_live_boot_failure_still_releases_lock` — pre-existing live rollback behavior preserved.
- ✅ GREEN: extracted `_safe_teardown(session, broker, bus, writer_lock)`. Both boot branches call it in their `except BaseException` block. Each cleanup is its own try/except so one failure does not mask another. Done in `e766fb1`.
- ✅ VERIFY: 4/4 new tests pass.
- ✅ FULL SUITE: 2754 passed, 0 failed.

**Acceptance:** ✅ DONE. A failure in either branch now leaves no leaked broker connection, no stranded writer lockfile, no subscribed bus. The session (if built) is stopped.

### C4 — defensive tz check
**Test-first plan:**
- ✅ RED: `trading/tests/execution/test_risk_tz.py::test_aware_then_naive_raises_clearly` — first call aware, second call naive; expect `ValueError`, not `TypeError`.
- ✅ RED: `trading/tests/execution/test_risk_tz.py::test_naive_then_aware_raises_clearly` — symmetric.
- ✅ RED: `trading/tests/execution/test_risk_tz.py::test_consistent_aware_passes`.
- ✅ RED: `trading/tests/execution/test_risk_tz.py::test_consistent_naive_passes` (matches BacktestEngine).
- ✅ RED: `trading/tests/execution/test_risk_tz.py::test_default_now_is_aware` — default path unchanged.
- ✅ RED: `trading/tests/execution/test_risk_tz.py::test_window_does_not_carry_across_managers` — fresh manager with no window accepts either awareness.
- ✅ GREEN: at top of `check()`, when `now is not None and self._recent_orders`, validate that `now.tzinfo is None` matches the first entry's. Raise `ValueError` with a clear message otherwise. Done in `91ab3dd`.
- ✅ VERIFY: 6/6 new tests pass.
- ✅ FULL SUITE: 2760 passed, 0 failed.

**Acceptance:** ✅ DONE. A tz-mismatch now raises `ValueError` with a clear message at the top of `check()`, instead of `TypeError` deep inside `total_seconds()`. BacktestEngine (all naive) and live (all aware) each work on their own; a hybrid session fails closed.

### C5 / G1p — wire bus back-pressure hooks
**Test-first plan:**
- ✅ RED: `trading/tests/reactive/test_bus_backpressure.py::test_drain_exceeded_increments_counter` — when the drain-exceeded branch fires, `bus.drain.exceeded` increments.
- ✅ RED: `trading/tests/reactive/test_bus_backpressure.py::test_max_queue_size_drops_on_subscriber_overflow` — a subscription with `max_queue_size=N` receives at most N messages; `on_backpressure` fires for the rest.
- ✅ RED: `trading/tests/reactive/test_bus_backpressure.py::test_subscribe_still_works_without_backpressure` — backward compat: no `max_queue_size` ⇒ all messages delivered.
- ✅ GREEN: per-subscriber remaining-capacity counter in `_gate`; on overflow drop, increment `bus.messages.dropped`, call `on_backpressure`. Drain-exceeded path now uses a clean `bus.drain.exceeded += 1` and `bus.messages.dropped += len(self._pending)`. Done in `0254054`.
- ✅ VERIFY: 3/3 new tests pass; pre-existing `test_backpressure_subscriber_no_crash` updated to assert the new (correct) cap behavior.
- ✅ FULL SUITE: 2763 passed, 0 failed.

**Acceptance:** ✅ DONE. `max_queue_size` and `on_backpressure` on `bus.subscribe` are now wired: a capped subscriber receives at most N messages and on_backpressure fires for each drop. The drain-exceeded path has clean `bus.drain.exceeded` and `bus.messages.dropped` metrics. A slow subscriber can no longer block the bus drain invisibly.

### H1 — `MetricsRegistry` lock
**Test-first plan:**
- ✅ RED: `trading/tests/runtime/test_metrics_thread_safety.py::test_counter_owns_a_lock` — `_Counter` must have a `_lock` attribute (a `threading.Lock`).
- ✅ RED: `trading/tests/runtime/test_metrics_thread_safety.py::test_histogram_owns_a_lock` — same for `_Histogram`.
- ✅ GREEN: added `self._lock = threading.Lock()` to `_Counter` and `_Histogram`; `inc`, `observe`, `set`, `value()`, `count`, `min`, `max` all acquire the lock. Done in the H1+H5+H7+H8 commit.

**Acceptance:** ✅ DONE. The lock is the contract: every read-modify-write on counter/histogram state goes through it.

### H2 — `MarketFeed` instrument lock
**Test-first plan:** (deferred to a future batch — the test must exercise concurrent subscribe/unsubscribe against `_on_quote`, which is finicky to set up reliably without a real broker stream).

### H5 — `modify()` re-runs risk
**Test-first plan:**
- ✅ RED: `trading/tests/execution/test_modify_risk.py::test_modify_exceeding_max_position_value_rejected` — modify to qty that exceeds `max_position_value` raises `OrderRejectedError`.
- ✅ RED: `trading/tests/execution/test_modify_risk.py::test_modify_within_limits_succeeds` — modify within limits still works.
- ✅ GREEN: `ExecutionEngine.modify()` now calls `self._risk.check(request)` after the broker-side modify and rolls back the OMS cache on rejection. Done in the H1+H5+H7+H8 commit.

**Acceptance:** ✅ DONE.

### H7 — `ErrorOccurred` subscriber
**Test-first plan:**
- ✅ RED: `trading/tests/runtime/test_error_occurred_logged.py::test_error_occurred_counter_increments_after_publish` — after a paper boot, publishing `ErrorOccurred` increments `engine.errors.total`.
- ✅ GREEN: the boot path now subscribes a logger + `engine.errors.total` counter to `bus.of_type(ErrorOccurred)`. Done in the H1+H5+H7+H8 commit.

**Acceptance:** ✅ DONE. Pipeline errors are no longer silent; operators see the event in logs and the runtime exposes a metric.

### H8 — delete `CandleReceived`
**Ponytail check:** ✅ DONE. The class was defined and re-exported but never published or subscribed. Removed from `events.py` and `__init__.py`. No test (the absence of usage is the test). Done in the H1+H5+H7+H8 commit.

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
- 2026-08-29 — C3 DONE (`e766fb1`): extracted `_safe_teardown(session, broker, bus, writer_lock)`; used by both live and non-live branches. 4 new tests, full suite 2754 passed. **C3 closed.**
- 2026-08-29 — C4 DONE (`91ab3dd`): defensive tz check at top of `check()`; raises `ValueError` with a clear message on awareness mismatch, instead of `TypeError` deep in `total_seconds()`. 6 new tests, full suite 2760 passed. **C4 closed.**
- 2026-08-29 — C5 DONE (`0254054`): per-subscriber `max_queue_size` gate (drop + `on_backpressure` callback + `bus.messages.dropped` counter); clean `bus.drain.exceeded` counter on the overflow path. 3 new tests, full suite 2763 passed. **C5 closed. ALL 5 SPRINT-1 CRITICALS CLOSED.**

## Sprint 1 summary

| # | Commit | Topic | Tests | Suite |
|---|---|---|---|---|
| C1 | `d6f65ea` + `df61073` | CashLedger → RiskManager + boot | 7 | 2747 |
| C2 | `29e527e` | Reconcile before `session.start()` | 3 | 2750 |
| C3 | `e766fb1` | `_safe_teardown` for both boot branches | 4 | 2754 |
| C4 | `91ab3dd` | Defensive tz check in `check()` | 6 | 2760 |
| C5 | `0254054` | BoundedReactiveBus back-pressure + counters | 3 | 2763 |

**23 new tests, all green. Full suite 2763 passed, 0 failed.** Every principal-architect critical is closed. Next: H1–H8 (one-line hardening fixes) before moving to Sprint 2.
