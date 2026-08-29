# TradeX v4 — Living Design State

**Purpose:** Single source for the platform's current design posture. Update after every architectural commit.
**Source of truth for:** invariants, known gaps, in-flight work, sequencing.
**Companion:** `docs/reviews/architecture-design-review-2026-08-29.md` is the *baseline* design review; this file is *current state*.

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

## Done — Sprints 1 + 2 closed

Every item below has a test pin and a commit. Suite at completion: **2808 passed, 0 failed**.

### Sprint 1 — live safety (5/5 🔴)
- ✅ **C1** `d6f65ea` + `df61073` — `RiskManager.bind_cash_provider` + cash check; `RiskConfig.cash_provider`; bound at boot for paper/live.
- ✅ **C2** `29e527e` — `_run_startup_reconciliation` runs before `session.start()`; refuses to start on a *new* critical-drift trip.
- ✅ **C3** `e766fb1` — `_safe_teardown(session, broker, bus, writer_lock)` for both boot branches.
- ✅ **C4** `91ab3dd` — defensive tz check raises `ValueError` with a clear message on awareness mismatch.
- ✅ **C5 / G1p** `0254054` — per-subscriber `max_queue_size` gate (drop + `on_backpressure` + `bus.messages.dropped`); clean `bus.drain.exceeded` counter.

### Sprint 1 hardening (5/8 🟠)
- ✅ **H1** `ab0808b` — `MetricsRegistry` `_Counter` / `_Histogram` own a `threading.Lock`; `inc`, `observe`, `set`, `value()`, `count`, `min`, `max` all acquire.
- ✅ **H2** `dc5e0aa` — `MarketFeed._stream_lock` (RLock) on `_instruments` + `_depth_instruments`; new `snapshot_instruments()`.
- ✅ **H5** `ab0808b` — `modify()` re-runs `risk_manager.check()`; rolls back the OMS cache on rejection.
- ✅ **H7** `ab0808b` — `bus.of_type(ErrorOccurred)` subscriber in boot; logger + `engine.errors.total` counter.
- ✅ **H8** `ab0808b` — `CandleReceived` YAGNI-deleted from `events.py` and `__init__.py`.

### Baseline gap tests + behavior (3)
- ✅ **G2** `aeb7c7b` — `RiskBudget` per-strategy envelope; `RiskManager(budgets=...)`; `_active_budget(request)` resolves from `request.tag`.
- ✅ **G3** `0c9678e` — `_record_applied_fill()` fingerprint dedup; sync path records fingerprint pre-publish; the FILLED-status band-aid was a bug; **2 real bugs found and fixed** by the RED step.
- ✅ **H3** `9f95c2e` — `applied_fills_max` is an `__init__` parameter on `ExecutionEngine`; docstring on `_record_applied_fill` spells out the LRU cap.

### Sprint 2 — durability (7/7)
- ✅ **R1** `bb62e39` — `SQLEventLog`: durable SQLite append-only event log replacing the in-memory `deque(maxlen=10_000)`. `ReactiveBus.set_message_log` accepts the new log.
- ✅ **G5** `69c5aa9` — `ReactiveStrategyEngine._pending` indexed by `InstrumentId`; `pending_max_age_bars` cancels stale orders; `dispose_all` clears the dict.
- ✅ **H4** `fb6395f` — `SQLiteOrderStore.upsert_from_event(OrderFilled)`; live fill path writes to SQLite; `get_recent(limit)` for post-crash recovery.
- ✅ **H6** `75aed48` — `LiveFillBridge._engine_order_index: dict[OrderId, str]` for O(1) order lookup; populated from `OrderPlaced` events.
- ✅ **M1** `d00a3ea` — `FeeCalculator._calculate_legacy` deleted; canonical formula is the only path.
- ✅ **M2** `9f95c2e` — `cancel()` releases the idempotency reservation via `self._cid_for_order` side-table; risk-rejection path also releases.
- ✅ **H7** (already in hardening) — see above.

---

## Pending — what remains

### Sprint 0 — pre-flight (highest leverage)
- 🟢 **G17** `pyproject.toml` files mostly empty; `uv.lock` is a 4-line stub. CI runs `uv sync --frozen` and gets nothing. *The prerequisite for every other CI-gated fix; without it, no other change is verifiable end-to-end.* **Status:** unstarted.

### Sprint 3 — correctness at scale
- 🟠 **G4** `BaseBroker` has ~30 pass-throughs. Refactor to a generated wall. **Status:** unstarted.
- 🟠 **G6** Promote `AnalyticsEngine` to a first-class `IndicatorRegistry`. Co-locate goldens with specs. **Status:** unstarted.
- 🟠 **G8** SDK service layer is a thin pass-through. Remove it or give it a real job. **Status:** unstarted.
- 🟡 **M7** Global `_ACTIVE_WRITER_LOCK` (`startup.py:43`); two live sessions overwrite each other. **Status:** unstarted.
- 🟢 **G15** Update `docs/ARCHITECTURE.md` after G10 lands. **Status:** unstarted (depends on G10).
- 🟢 **G16** `tests/test_import_boundaries.py` — keep, update when G10 lands. **Status:** unstarted (depends on G10).

### Sprint 4 — design hygiene (deferrable)
- 🟡 **G10** `trading/` is 25.6k LOC across 10 modules. Split into `trading_core / _datalake / _strategy / _runtime / _interface / _sdk`. **Status:** unstarted; *only when team size justifies.*
- 🟡 **G11** `analytics/` flat dir of 30 modules. Sub-folders by family. **Status:** unstarted.
- 🟡 **G12** `replay/` mixes driver, walk-forward, optimization, synthetic ticks. **Status:** unstarted.
- 🟡 **M3** Post-dispose publish silently enqueues to a completed Subject. **Status:** unstarted.
- 🟡 **M4** `broker.close()` called twice (`RuntimeContext.close` and `TradingSession.stop`); relies on broker `close()` being idempotent, not enforced. **Status:** unstarted.
- 🟡 **M5** `RiskManager._session_date: Any` — mypy can't catch tz-mismatches. **Status:** unstarted.
- 🟡 **M6** `BrokerFillSource.submit` wraps raw string to `OrderId` without validation. **Status:** unstarted.

### Sprint 5 — observability
- 🟠 **R2** Partition the bus: `OrderPipeline` / `MarketData` / `Diagnostics`. **Status:** unstarted.
- 🟠 **R3** Promote `IndicatorSpec` / `IndicatorRegistry` to first-class contracts. **Status:** unstarted.
- 🟢 **G14** `MetricsRegistry` is in-process and never exported. Wire Prometheus exposition or drop. **Status:** unstarted.

### Backlog
- 🟡 **G7** `ScannerEngine._history` is snapshot-only. Stream consumer. **Status:** unstarted.
- 🟠 **G9** `services/duckdb-analytics` re-implements `ScannerEngine` with no shared contract. **Status:** unstarted.
- 🟡 **G13** Frontend has 11 `if (!resp.ok) throw new Error(...)`; backend has no typed-error contract. **Status:** unstarted.

---

## Process rules (from loaded skills)

From `using-superpowers`: invoke a relevant skill *before* any non-trivial action.
From `ponytail` (full): the ladder; bug fix = root cause; mark shortcuts with `ponytail:`.
From `test-driven-development`: red → verify red → green minimal → refactor.
From `tradexv2-org`: architecture first, then contracts, then tests, then implementation; TDD; no partial architectural branches.

---

## Update log

- 2026-08-29 — design state initialized; baseline = `docs/reviews/architecture-design-review-2026-08-29.md`.
- 2026-08-29 — Sprint 1 revised: principal-architect review surfaced 5 new 🔴 (C1–C5) and 8 🟠 (H1–H8) findings.
- 2026-08-29 — C1 GREEN (`d6f65ea`); C1 follow-up (`df61073`); C2 (`29e527e`); C3 (`e766fb1`); C4 (`91ab3dd`); C5 (`0254054`). All 5 Sprint-1 criticals closed.
- 2026-08-29 — H1+H5+H7+H8 batch (`ab0808b`); H2 (`dc5e0aa`); G3 (`0c9678e`); G2 (`aeb7c7b`).
- 2026-08-29 — Sprint 2 closed via parallel agent team: R1 (`bb62e39`), M1 (`d00a3ea`), H4 (`fb6395f`), H6 (`75aed48`), H3+M2 (`9f95c2e`), G5 (`69c5aa9`). 7/7 items done. Suite 2808 passed, 0 failed.
- 2026-08-29 — state file consolidated: every closed item moved to a single "Done" section with commit hashes; per-item RED/GREEN test plan sections removed (they were stale). Sprint 3 / 4 / 5 / Backlog pending items preserved.
