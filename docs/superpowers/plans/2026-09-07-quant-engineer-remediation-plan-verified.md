# TradeX v4 — Principal-Quant Remediation Plan (Verified)

**Date:** 2026-09-07
**Reviewer lens:** principal quant engineer
**Verification companion:** `docs/reviews/quant-finding-verification-2026-09-07.md`
**Baseline:** current shared working tree on `s0-phase1-followon`; reported suite **2885 passed, 2 skipped**.

This plan supersedes the earlier 2026-09-07 drafts. It is deliberately narrower: the codebase has already fixed several findings that the earlier plan mistakenly treated as open.

---

## 1. Verified posture

The following foundations are **not** rewrite targets:

- pure, immutable `tradex_domain` value objects and Decimal accounting;
- one production `ExecutionEngine` order spine;
- shared `FillModel`, `position_math`, fee/slippage seams;
- conservative MTM service and fail-closed live mark gate;
- one resilience pipeline;
- frontend `clientToken` → `Idempotency-Key` forwarding;
- live writer lock, startup reconciliation, SQLite order store/idempotency guard;
- parity and adapter-contract tests.

The earlier findings are corrected as follows:

| ID | Finding | Verified status | Correct next action |
|---|---|---|---|
| F1 | Missing/ambiguous live fill identity | **Partially mitigated** | Provider-neutral identity contract; wire Dhan and Upstox; no silent live downgrade |
| F2 | Wall-clock use in deterministic paths | **Partially confirmed; narrower than stated** | Classify clock uses; enforce explicit timestamps only in deterministic pipeline paths |
| F3 | End-to-end idempotency | **Frontend closed; submit/cancel hash binding open** | Bind operation/target/payload fingerprints at every mutation reservation |
| F4 | Duplicate `trading/events/` stack | **Already deleted in working tree** | Audit/wire current `SQLEventLog` and define/test actual recovery contract |
| F5 | Replay/live session isolation | **Interim route gate present** | Defer full replay driver; retain gate as release invariant |

---

## 2. Execution order

### Phase 0 — protect the shared working tree

The checkout contains extensive unrelated/uncommitted work and staged deletions. Before implementation:

1. Do not reset, stash, stage, overwrite, or delete other agents’ changes.
2. Record the files/hunks owned by each subtask.
3. Use focused tests before full-suite validation.
4. Treat the existing green-suite claim as a baseline to re-run, not as permission to broaden scope.

**using-superpowers gate:** before each non-trivial subtask, identify the relevant skill (adapter contract, TDD, parity, or deletion/refactor) and follow its red → green discipline.

**ponytail rule:** root cause first; fix the layer that owns the invariant; mark any compatibility fallback explicitly rather than disguising it as correctness.

---

### Phase 1 — P0 money-path fixes

#### 1A. Complete the live fill identity contract (F1)

**Root cause:** `ExecutionEngine._record_applied_fill()` correctly prefers `fill_id`, but when it is absent the fallback `(order_id, side, qty, price)` cannot distinguish two genuine equal-lot same-price partials. `TradeBookFillIdResolver` improves Dhan only and degrades to `None` on outage/lag. Upstox has `get_trade_book()` with different method/row names but startup only probes `trade_book`.

**Implementation plan:**

1. Define a provider-neutral trade identity seam. The adapter or fill bridge must expose a callable returning executed trades in a normalized shape, for example `{order_id, fill_id}`; do not make the engine know Dhan/Upstox field names.
2. Wire both providers:
   - Dhan: `trade_book()` + `orderId`/`tradeId` normalization.
   - Upstox: `get_trade_book()` + `order_id`/`trade_id` normalization; verify the actual production row key, not only test fixtures.
3. Fix cumulative-delta identity stability. A resolver may assign an ID once for a newly observed cumulative boundary, but a republished identical cumulative update must not consume a new trade ID. If multiple trade rows become visible between updates, map each emitted delta to a stable unseen trade identity; do not use a naïve counter invoked on every callback.
4. Choose and encode a live failure policy:
   - preferred: if a positive fill delta has no stable identity, do not publish it as an ordinary live fill; raise/emit a safety diagnostic and halt or require reconciliation;
   - if operations explicitly accept a temporary fallback, make it configurable, metrics-visible, and unavailable in live release configuration.
5. Keep the composite fingerprint only for legacy/test/replay compatibility until all live providers satisfy the contract. Label it as ambiguous defense-only behavior.
6. Add provider contract tests for Dhan and Upstox, including actual normalized trade rows, missing trade id, trade-book outage, and two equal-size/equal-price partials.

**Acceptance gates:**

- Dhan and Upstox live fill paths attach a non-empty stable identity to every accepted fill.
- Two equal-size/equal-price partials both book; an identical cumulative republish does not.
- Missing/outage identity cannot silently create an ordinary live fill.
- No provider-specific fields leak into `ExecutionEngine`.

#### 1B. Make idempotency conflict protection complete (F3)

**Root cause:** frontend forwarding and basic replay are already correct. The guard’s request-hash feature is not consistently used: `_run_pipeline()` and `cancel()` call `check_and_reserve(cid)` without a hash; `modify()` already supplies a request fingerprint.

**Implementation plan:**

1. Build a canonical, operation-specific fingerprint. It must include:
   - operation (`submit`, `modify`, `cancel`, bracket variant);
   - target order id where applicable;
   - all economically/mutably relevant request fields;
   - canonical Decimal/string representation.
2. Pass that fingerprint to `check_and_reserve()` for synchronous and reactive submit paths, and for cancel. Preserve the existing modify behavior while upgrading it to include operation/target if needed.
3. Ensure the route maps key reuse mismatch to HTTP 409 without any venue call.
4. Test all three mutation classes and SQLite restart behavior:
   - same key + same operation/target/payload → replay;
   - same key + changed payload → 409/no second venue call;
   - same key + changed target or operation → 409;
   - reserved/in-flight key → deterministic 409 response;
   - transport uncertainty preserves the key for reconciliation.
5. Do not change the already-correct frontend forwarding unless a regression test proves a gap. Existing `frontend/src/trade/integration.test.ts` already proves POST forwarding and generated-key behavior.

**Acceptance gates:**

- No mutation reservation occurs without an operation/target-aware fingerprint.
- Same-key altered requests cannot replay or reach the broker.
- Existing duplicate/retry behavior remains green.

---

### Phase 2 — P1 deterministic-clock hardening (F2)

**Root cause:** deterministic strategy/backtest paths usually propagate event timestamps, but APIs still permit omission and silently fall back to `datetime.now(UTC)`. Live-fill receipt time is intentionally wall-clock-based and should not be removed blindly.

**Implementation plan:**

1. Classify every clock use in `domain`, `execution`, `strategy`, `replay`, and broker ingest as:
   - **event/storage boundary:** keep, document;
   - **live receipt clock:** keep, preferably prefer broker execution timestamp;
   - **convenience default:** keep only outside deterministic execution;
   - **pipeline logic:** replace with injected clock or explicit event timestamp.
2. Introduce/use one `Clock` seam in execution/risk where it materially affects decisions. The domain already defines the protocol and backtest already has `FakeClock`.
3. Make deterministic execution explicit:
   - `FillModel` must not silently use wall clock in backtest/replay when `reference_timestamp` is absent;
   - strategy-generated orders must carry the triggering event timestamp;
   - risk rate-window/session-date decisions in deterministic modes must use the injected/event time.
4. Preserve live receipt timestamps for live events, but use the broker’s execution timestamp if available and retain receipt time separately if audit requires both. Do not conflate market-event time, execution time, and receipt time.
5. Fix or constrain `ScannerEngine`’s omitted-window fallback for replay/backtest; explicit windows are required in deterministic modes.
6. Add a focused CI lint/test rule for pipeline modules, with a small allowlist for documented live/boundary seams. Do not blanket-replace all repository `datetime.now(UTC)` calls.

**Acceptance gates:**

- Running the same backtest/replay twice with different wall-clock time produces identical fills, risk decisions, and results.
- Missing deterministic fill timestamp fails loudly or is supplied by the injected clock according to one documented policy.
- Live fill audit timestamps remain accurate.
- Every remaining pipeline `now()` has a boundary justification.

---

### Phase 3 — Durability and recovery truth (F4 corrected)

**Current fact:** `trading/events/` and its tests are already deleted in the working tree. `trading/scripts/probe_review_fixes.py` is now a non-importing AST boundary diagnostic, not a probe into the old OMS. `reactive/SQLEventLog` already exists; `startup.py` currently wires SQLite idempotency + order storage, not `SQLEventLog`.

**Do not:** re-port or re-delete the absent `trading/events/` package; do not create a second OMS or claim event-sourced recovery from a best-effort serialized message log.

**Implementation plan:**

1. Audit `SQLEventLog` for production suitability:
   - event type/version fidelity;
   - serialization of Decimal, datetime, and domain objects;
   - append atomicity and connection lifecycle;
   - ordering and concurrency;
   - retention/rotation and operational recovery.
2. Decide the actual recovery contract:
   - either wire a tested domain-event log into boot and implement reconstruction/projectors;
   - or explicitly state that current recovery is SQLite order/idempotency restore plus broker reconciliation, and defer event replay.
3. If wiring the log, attach it through the existing bus seam in the composition root and test restart/replay using actual domain event semantics. Do not use `repr()` payloads as authoritative recovery data.
4. Keep the existing deletion as part of the working-tree change set; only remove the probe script if the owner confirms it is no longer useful as a boundary diagnostic.
5. Update architecture/state docs to say what is actually wired, not what is merely available as a class.

**Acceptance gates:**

- Recovery behavior is tested from real persisted artifacts.
- No production import of `tradex_trading.events` exists.
- Documentation distinguishes order-store restore from event-sourced recovery.
- No second state authority is introduced.

---

## 3. Deferred work

These remain valid but are not prerequisites for the P0/P1 block:

1. **Replay-scoped session/driver:** retain `_require_trading_mode` as an invariant; schedule the full driver/clock abstraction separately.
2. **`SUBMITTED`/`UNKNOWN` cleanup:** remove from submission paths when touching the FSM; add exhaustive transition tests.
3. **Frontend intent-only controller:** demote the browser FSM after server-side mutation contracts are fully proven.
4. **Frontend `main.ts`/socket split:** performance and maintainability work, not a first-order money correctness fix.
5. **Broker protocol split:** useful ISP improvement, but avoid changing adapter seams during the P0 fill-identity work.

---

## 4. Release gates

Do not enable live-money release based solely on the aggregate test count. Require all of:

- live-provider fill identity contract green for Dhan and Upstox;
- no silent live fallback when fill identity is unavailable;
- submit/modify/cancel key mismatch tests green, including SQLite restart;
- deterministic replay/backtest invariance test green;
- startup reconciliation + writer lock + mark freshness tests green;
- full suite green and parity goldens unchanged unless an economic rule change is explicitly reviewed;
- architecture/state docs updated to current wiring.

**Ponytail shortcut register:**

- Composite fill fingerprint remains only as a temporary compatibility defense, never as proof of exact-once live fills.
- Live receipt wall clock remains intentionally, but is separated from deterministic pipeline time.
- Full replay-driver and frontend decomposition are deferred, explicitly—not silently ignored.
- Event-log recovery is not claimed until type-fidelity and reconstruction tests exist.

*This verified plan supersedes `2026-09-07-quant-engineer-remediation-plan.md`, `2026-09-07-quant-hardening-brief.md`, and `2026-09-07-phase-b-delete-events-brief.md` for execution planning.*
