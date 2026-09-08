# TradeX v4 — Principal-Quant Remediation Plan

**Date:** 2026-09-07
**Author lens:** principal quant engineer
**Trigger:** top-down architecture/code review + green suite (2885 passed, 2 skipped)
**Scope:** money correctness, determinism, idempotency, dead-code cleanup
**Assumption:** the good spine (kernel, one pipeline, one fill model, one position math, one resilience stack, parity suite) is *not* up for rewrite. This plan attacks the specific residual risks the review named, in priority order.

---

## 1. Review-derived risk register (evidence, not opinion)

| ID | Finding | Severity | Where | Status |
|----|---------|----------|-------|--------|
| **F1** | Live-fill dedup ambiguity without a venue `fill_id` (equal-lot same-price partials indistinguishable from re-publish) | **P1 — live money** | `execution/engine.py:_record_applied_fill`; bridge `TradeBookFillIdResolver`; adapter order-stream decoders | Open |
| **F2** | Nondeterministic `datetime.now(UTC)` fallbacks still in the fill/OMS/risk path (`Fill.timestamp`, `Quote`, domain events, `fill_model.fill_timestamp`, `engine` risk defaults) | **P1 — replay/live parity + incident reproduction** | `domain/execution.py:265`, `domain/market.py:132`, `domain/events.py:23`, `fill_model.py:69`, `engine.py:473/644/678`, scanner/chart fallback | Open |
| **F3** | Backend idempotency contract is correct; need E2E proof that the frontend `clientToken` reaches `Idempotency-Key` and duplicate key ⇒ exactly one order, retry-with-same-key ⇒ replay | **P1 — trust the contract** | `interface/routes/orders.py` (done); frontend `trade-feed.ts` (verify); add test chain | Backend done; frontend + test open |
| **F4** | Dead second OMS stack `trading/events/` + probe script importing it; salvageable ideas = durable append-only EventLog + crash recovery | **P2 — cognitive + maintenance debt** | `trading/events/`; `trading/scripts/probe_review_fixes.py`; state file says physical delete blocked by tool policy | Port-then-delete pending |
| **F5** | Replay still shares the live session for trading; interim route gate `_require_trading_mode(mode=="replay")` exists | **P2 — mode semantics** | `interface/routes/orders.py`; session mode wiring | Interim gate present; full `SessionDriver ×4` is Phase-3 scope, defer |
| **F6** | `OrderStatus.SUBMITTED`/`UNKNOWN` still in the transition table; `SUBMITTED` row omits `REJECTED` (trap if anything routes through it) | **P3 — trap removal** | `domain/execution.py:_LEGAL_TRANSITIONS` | Low urgency; mostly avoided already |
| **F7** | Frontend still has a 9-state order FSM claiming broker truth; should become a 3-state intent layer | **P3 — frontend separation** | `frontend/src/trade/order-engine.ts` | Defer behind F3 |
| **F8** | `main.ts` ~1003 LoC owns replay transport + control socket + boot + chart settings + logs | **P3 — frontend hygiene** | `frontend/src/main.ts` | Defer behind frontend store/streams split |

**Priority rule (quant systems):** money and deterministic replay first; everything else second. F1 and F2 can silently corrupt the one thing we are most careful about (dollar accuracy and reproducible evidence). F3 is the test that tells us the money path is actually idempotent in the real request path. F4 is a clean-up that reduces blast radius of future changes.

---

## 2. Sequence (dependency order, not wish list)

### Phase A — Quant-correctness hardening (do first, together)

Because F1 and F2 are independent of each other but both are money/determinism, run them as two parallel workstreams with one shared rule: **no new public API, no domain-type changes, no broker adapter rewrites beyond the fill_id seam.** This keeps the parity suite honest.

#### A1 — Fill-id contract (F1)

**Root cause (ponytail):** the engine's dedup fallback `(order_id, side, qty, price)` is mathematically ambiguous for two equal-lot partial fills at the same price. The right layer to fix this is the *adapter boundary* (the one place that sees the venue's trade identity), not the engine.

**Steps:**

1. Decide the contract in one place: a `FillSource`/adapter order-stream decoder must always attach a `fill_id` to a `Fill`. Acceptable sources, in priority order:
   - Venue trade id from the broker order/trade stream (Dhan `tradeId`, Upstox trade id) — already partly wired via `TradeBookFillIdResolver`.
   - A synthesized monotonic id generated *in the adapter/bridge decoder* when the venue truly omits one, per-instrument per-order, deterministically replayable if driven by a recorded tape.
2. Make the engine treat `fill_id is None` as a *contract warning* today and an *error path* tomorrow: start by asserting in a contract test that a `Fill` entering `_apply_fill` / `_record_applied_fill` carries a non-empty identity; the test should fail if any live-path fill arrives without one.
3. Keep the existing composite-fingerprint fallback as an explicit *defense-only* path behind a feature gate or documented deprecation, not the default. The goal is: **no ambiguous dedup in live money mode**.
4. Add one regression test: two partial fills at the same price for the same order, both with distinct trade ids, both applied — verify avg price and realized PnL are correct and neither is silently dropped.

**Acceptance:**
- A recorded-tape or synthetic order-stream test can drive two same-price partials and the OMS books both.
- A contract test asserts `fill_id` presence on fills entering the engine from the live path.
- Parity suite unchanged (Decimal-exact).

#### A2 — Deterministic-clock audit (F2)

**Root cause (ponytail):** `datetime.now(UTC)` is used as a default factory and as a fallback in several places that sit on the fill/OMS/risk path. In deterministic modes (backtest/replay) any code path that can omit `reference_timestamp` or omit an explicit `now` becomes wall-clock-dependent, which breaks replay/live parity and incident reproduction.

**Steps:**

1. Inventory every `datetime.now(UTC)` in the trading + domain execution paths (the review already grep'd 55 hits; classify each as boundary vs logic):
   - *Boundary (keep, but mark):* boot, route times, UI-edge conversions, broker ingest where IST/UTC conversion is the actual concern, test fixtures.
   - *Logic (fix):* `Fill.timestamp` default, `Quote`/domain-event default factories, `fill_model.fill_timestamp` fallback, `engine` risk `_fresh_marks_available` / `_session_net` defaults, scanner/chart fallback defaults.
2. Make fills refuse to be built without a real timestamp source in deterministic modes:
   - In `FillModel.fill_timestamp`, when running in a deterministic mode and `request.reference_timestamp` is absent, that should be a hard error, not `now()`. The strategy bridge already stamps reference timestamps; the fix is to make the absence fail loudly where determinism matters.
   - Add a small `Clock` seam (the domain already has a `Clock` protocol) that the engine/risk/fill paths use instead of calling `now()` directly. Live mode binds a real UTC clock; backtest/replay bind the deterministic clock they already own.
3. Move the risk manager's default `now` behind the same seam: `RiskManager.check(request, now=clock.now())` rather than `now or datetime.now(UTC)`. Backtest already passes `now`; this just makes the default path safe and explicit.
4. Add a lint/CI check: no `datetime.now(UTC)` in `trading/src/tradex_trading/execution/`, `fill_model.py`, `sdk/live_fill_bridge.py`, and the reactive strategy path, except in explicitly annotated boundary seams. This is the "no wall-clock in pipeline" rule from the architecture review.

**Acceptance:**
- In replay/backtest, filling without a reference timestamp raises, not silently mints `now()`.
- The engine and risk paths use an injected clock in deterministic modes.
- CI lint flags remaining `now()` calls; each must be justified as a boundary.

#### A3 — E2E idempotency contract test chain (F3)

**Root cause (ponytail):** the backend now requires `Idempotency-Key` and correctly dedups/replays/inflight-errors. The only remaining question is the full request path: browser token → header → route → engine guard → duplicate suppression → retry replay.

**Steps:**

1. Verify the frontend `trade-feed.ts` actually forwards `clientToken` as `Idempotency-Key` on `POST /orders`, `PUT /orders/{id}`, `DELETE /orders/{id}`, `POST /orders/bracket`. If it does not, that is a one-line fix and the first thing to do — the backend is already correct.
2. Add one contract test per mutation:
   - Same `Idempotency-Key` twice ⇒ second returns the original order id (replay), no second order created.
   - Missing key ⇒ 422.
   - Key reuse with a different payload ⇒ 409 (IdempotencyKeyReuseMismatch).
   - In-flight key re-submit ⇒ 409 (IdempotencyInflight).
3. Add one integration test simulating a transport failure/retry with the same key ⇒ exactly one order in the OMS, and the receipt is the original one.
4. Preferably add the frontend assertion too: the browser's `clientToken` must appear as the wire header, not just live in JS heap.

**Acceptance:**
- E2E dedup and replay tests green.
- Frontend forwards the token (or the gap is documented and owned).
- This becomes the highest-leverage test in the repo for real-world failure (504, double-click, socket reset).

### Phase B — Dead-stack cleanup (after A1–A3 are green)

This phase is deletion-led. The rule from the architecture review: *deleting a duplicate beats refactoring it; a second implementation of anything with money semantics is a defect.* `trading/events/` is exactly that.

**Steps:**

1. Port the two genuinely good ideas out of `trading/events/` into `execution/`:
   - Durable append-only event log → `execution/event_log.py` (SQLite, session-scoped), wired as the engine lifecycle durability layer alongside the existing `SQLiteOrderStore`. This is the durability story for the *one* pipeline, not a second OMS.
   - Session recovery / replay-project-then-resume flow → `execution/recovery.py`, driven off that log.
2. After the port, run the full suite. If anything in `trading/events/` was genuinely providing behavior no other layer covers, the suite will tell you before you delete.
3. Then physically remove `trading/events/` and `trading/scripts/probe_review_fixes.py`. The state file already says the delete is blocked by tool security policy, so the actual `rm -rf` is manual; the coding work is the port + verification.
4. Remove any import of the dead stack from tracked code.

**Acceptance:**
- Suite green with the port in place.
- No tracked file imports `trading.events`.
- LOC delta negative; parity goldens byte-stable.

### Phase C — Mode semantics, trap cleanup, frontend separation (defer unless team asks)

These are real but lower priority for a quant-correctness sprint.

- **C1 (mode semantics, F5):** keep the interim `_require_trading_mode` gate; do not let replay trade against a live account. The full `SessionDriver ×4` abstraction is the real fix and is a bigger architectural change; schedule it separately.
- **C2 (transition table trap, F6):** demote `SUBMITTED`/`UNKNOWN` from submission paths; only allow `UNKNOWN` via reconciliation. Low urgency because the current code mostly avoids the trap; do it when touching the FSM for another reason.
- **C3 (frontend, F7/F8):** demote browser FSM to 3-state intent, server-computed PnL in book/WS, split `main.ts` into store/streams/panels. Defer until the backend quant-correctness work is done and the E2E idempotency chain is in place — frontend authority removal is pointless before the server is the dedupe authority (which is F3).

---

## 3. Discipline notes (using-superpowers + ponytail)

**Using-superpowers rule applied here:** before any non-trivial change in this plan, invoke the relevant skill. Concretely:
- A1/A2/A3 each start from a *skill-first* check: which existing skill covers the change (parity, execution, adapter contracts, frontend wire), and what does that skill require before code moves.
- Do not start code before the *red* step for anything that has a correctness claim. The parity suite and the new contract tests are the red/green harness.

**Ponytail rule applied here:** every fix in this plan starts from root cause, and every shortcut is marked.
- F1 root cause = ambiguous dedup key at the engine; fix layer = adapter boundary + contract test, not engine rewrite.
- F2 root cause = `now()` fallback in logic paths; fix = mandatory reference timestamp in deterministic modes + injected clock seam + CI lint; not a global "replace all now()" that would touch legitimate boundaries.
- F3 root cause = server already correct; remaining gap = frontend forwarding + proof; fix = verify/one-line + E2E chain, not a rethink of the guard.
- Shortcuts marked: Phase C items are explicitly deferred shortcuts away from the quant-correctness core; they are not ignored, they are sequenced.

---

## 4. What good looks like when this plan is done

1. Live partial fills are deduped by identity, not by an ambiguous composite, and two same-price partials both book.
2. Replay/backtest cannot mint wall-clock fills; deterministic modes fail loudly when a fill has no reference timestamp.
3. One E2E test chain proves duplicate request key ⇒ one order, retry-with-same-key ⇒ replay.
4. `trading/events/` is gone; its two good ideas live in `execution/` as the durability/recovery layer for the one pipeline.
5. Parity suite still green and Decimal-exact; no new public API surface; no domain-type churn.

---

## 5. Out of scope (say no now)

- Full `SessionDriver ×4` abstraction (Phase-3 architecture work; defer).
- Frontend store/streams/main.ts split (gated behind F3 and backend stability).
- New broker ports, new indicator registry reorganization, analytics folder cleanup — separate efforts.
- Any change to the kernel value objects or the one-pipeline spine.
- Any change that would make the parity goldens drift without a documented reason.

---

*Evidence base for this plan: top-down review of `domain/`, `brokers/`, `trading/`, `frontend/`, `docs/ARCHITECTURE.md`, `docs/reviews/principal-architecture-review-2026-09-04.md`, `docs/design/state.md`; suite green at 2885 passed / 2 skipped.*
