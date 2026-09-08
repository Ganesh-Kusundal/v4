# Quant-Hardening Brief — Block A (fill_id + deterministic clock + E2E idempotency)

**One-line goal:** make the money path deterministic and deduped by identity before any live-money expansion.

**Why this block first:** these are the two P1 findings from the top-down review that can silently corrupt dollars or break replay/live parity. Everything else in the plan is downstream of “can we trust a fill and a replay?”

**Success:** after this block, a replayed tape and a live path cannot produce an ambiguous dedup, a fill cannot secretly mint `now()` in a deterministic mode, and the idempotency contract is proven end to end instead of only in the backend.

---

## The three items, root cause first (ponytail)

### 1. Fill-id contract (F1)
- **Root cause:** engine dedup falls back to `(order_id, side, qty, price)` when `fill_id` is absent; that is ambiguous for two equal-lot same-price partials.
- **Fix layer:** adapter/bridge order-stream boundary, not the engine. The one place that sees venue trade identity must attach a `fill_id` — venue id when available, synthesized monotonic id when the venue omits one.
- **Marked shortcut:** keep the composite-fingerprint fallback only as an explicit defense path behind a documented deprecation, not the default live-money path.

### 2. Deterministic-clock audit (F2)
- **Root cause:** `datetime.now(UTC)` appears as a default factory / fallback in `Fill`, `Quote`/domain events, `fill_model.fill_timestamp`, and several engine/risk defaults.
- **Fix layer:** make deterministic modes fail when a fill has no reference timestamp; inject a `Clock` seam into the execution/risk/fill paths; lint out `now()` from logic paths and only allow it at annotated boundaries.
- **Marked shortcut:** do not do a blanket “replace all now()” — classify each hit as boundary vs logic and only move the logic ones.

### 3. E2E idempotency chain (F3)
- **Root cause:** backend guard is correct; the remaining gap is the full path (frontend token → header → route → guard → dedup/replay/inflight).
- **Fix layer:** verify/repair the frontend forwarding, then add contract + integration tests for duplicate key, missing key, key reuse with different payload, in-flight re-submit, and transport-retry-with-same-key.
- **Marked shortcut:** if the frontend already forwards the token, this block is mostly tests; if it does not, the one-line fix comes before the tests.

---

## What each item must prove (acceptance, not activity)

- **F1:** a recorded-tape or synthetic stream can deliver two same-price partial fills with distinct trade ids and both book correctly; a contract test asserts live-path fills carry a non-empty identity.
- **F2:** in deterministic modes, building a fill without a reference timestamp raises; engine/risk use an injected clock; CI flags remaining `now()` in logic paths.
- **F3:** same key twice returns the original order id; missing key → 422; mismatched key → 409; in-flight key → 409; transport retry with same key ⇒ one order.

---

## Order and coupling

1. A1 and A2 are independent and can go in parallel with one shared constraint: **no new public API, no domain-type changes, no broker adapter rewrite beyond the fill_id seam.** This keeps the parity suite honest.
2. A3 can start once the route layer is confirmed correct (it is) and the frontend forwarding is known; the E2E chain is the proof that A1/A2 did not break the real request path.
3. Phase B (port event log + recovery, delete `trading/events/`) waits until A1–A3 are green, because that deletion should not touch any layer that A1–A3 are hardening.

---

## Out of scope for this block

- Full `SessionDriver ×4` abstraction.
- Frontend store/streams/main.ts split.
- `SUBMITTED`/`UNKNOWN` transition-table cleanup (do only when touching the FSM for another reason).
- New broker ports, analytics folder cleanup, indicator registry reorganization.

---

*Skill posture: using-superpowers — invoke the right skill before each sub-change and start from red for any correctness claim. Ponytail — root cause stated, fix layer chosen, shortcuts marked.*
