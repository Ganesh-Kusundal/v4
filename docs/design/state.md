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

## Known gaps (from baseline review)

Severity tags: 🔴 critical · 🟠 high · 🟡 medium · 🟢 low.

### Sprint 1 — live safety (in progress)
- 🔴 **G2** No margin / buying-power / per-strategy budget. RiskManager has only notional, position, rate, daily-loss, drawdown.
- 🔴 **G1p** Wire existing `BoundedReactiveBus` back-pressure hooks (`max_queue_size`, `on_backpressure`) — currently no-ops. Add `bus.messages.dropped` and `bus.drain.exceeded` counters.
- 🔴 **G3** No test for the live-fill race between `_run_pipeline` and `_apply_fill`. Load-bearing comment at `engine.py:812` documents an invariant; no test pins it.

### Sprint 2 — durability
- 🔴 **R1** Replace in-memory `deque(maxlen=10_000)` in `ThreadSafeReactiveBus` with SQLite append-only event log. Replay on boot.
- 🟠 **G5** `ReactiveStrategyEngine._pending` linear scan. `dict[InstrumentId, list]` + `pending_max_age` + last-bar-of-dataset test.
- 🟢 **G17** `pyproject.toml` files mostly empty; `uv.lock` is a 4-line stub. CI runs `uv sync --frozen` and gets nothing. *This is the prerequisite for every other CI-gated fix.*

### Sprint 3 — correctness at scale
- 🟠 **G4** `BaseBroker` has ~30 pass-throughs. Refactor to a generated wall.
- 🟠 **G6** Promote `AnalyticsEngine` to a first-class `IndicatorRegistry`. Co-locate goldens with specs.
- 🟠 **G8** SDK service layer is a thin pass-through. Remove it or give it a real job.

### Sprint 4 — design hygiene
- 🟡 **G10** `trading/` is 25.6k LOC across 10 modules. Split into `trading_core / _datalake / _strategy / _runtime / _interface / _sdk`. Only when team size justifies.
- 🟡 **G11** `analytics/` flat dir of 30 modules. Sub-folders by family.
- 🟡 **G12** `replay/` mixes driver, walk-forward, optimization, synthetic ticks. Split.

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

### G1p — wire bus back-pressure hooks
**Test-first plan:**
- RED: `trading/tests/reactive/test_bus_backpressure.py::test_dropped_message_emits_metric_and_error_event` — publish 11 messages to a slow subscriber, expect `bus.messages.dropped == 1` and an `ErrorOccurred` on the bus.
- RED: `trading/tests/reactive/test_bus_backpressure.py::test_drain_exceeded_emits_critical_log_and_metric` — install a subscriber that republishes forever, expect `bus.drain.exceeded` counter incremented and log.critical emitted.
- GREEN: minimal wiring in `ReactiveBus.subscribe` (drop on cap; emit `ErrorOccurred`) and `publish` (count overflow).
- REFACTOR: extract `BoundedReactiveBus` only if duplicated.

**Acceptance:** tests green; counter visible in `MetricsRegistry.snapshot()`; bus delivery never blocks longer than `_MAX_NESTED_DELIVERIES` iterations.

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
