# Money-Path Review — TradeX v4

**Date:** 2026-11-06 · **Commit:** `638b24e12dcf2b67552239c0b144143c12e62915` (HEAD)
**Method:** line-by-line adversarial read of the order/money spine: `execution/engine.py` (all 1,650 lines), `cash_ledger.py`, `idempotency.py`, `fill_sources.py`, `fill_model.py`, `kill_switch.py`, `cash_manager.py`-adjacent wiring, `position_accountant.py`, `mark_to_market.py`, `position_math.py`, `recovery.py`, `sqlite_event_store.py` (append/verify/dedup index), `reconciliation.py` (adoption path spot-checks). Runs: guard suite (`1 failed, 524 passed` — pre-existing gateway guard failure), `trading/tests/parity` + `trading/tests/contracts` (170 + 196 passed).
**Contrast class:** the spine is genuinely strong — AC-22 single door, three-state UNKNOWN submission policy, hash-chained event store, append-before-mutate, the one-lock `CashLedger.apply_fill`. This review found **six defects**, none of which breaks basic accounting arithmetic, but several of which corrupt state or strand the book in ways tests currently miss.

---

## 0. Verdict

**Not yet safe for real money — narrow, fixable reasons.** The three serious findings:

1. **MP-1** — a live fill can be applied twice: once to the book and once silently lost, or booked twice, depending on which of three doors delivered it and whether the process restarted. The durable dedup machinery exists, is tested, and is wired to nothing.
2. **MP-3** — the SELL fee gate can block a legitimate exit when the account holds zero idle cash, contradicting the platform's own "never block a sell" rule. Both doors share the formula.
3. **MP-4** — a venue-confirmed fill the local ledger deems unaffordable is dropped from the local book entirely (an error event, swallowed by bus isolation) instead of being adopted and flagged. Local book and venue diverge silently; only reconciliation would ever notice.

Plus an attribution-corruption bug (**MP-2**) that defeats per-strategy P&L within one quote tick, and memory growth in the idempotency guard (**MP-5**).

---

## Defects (ranked by exploitability/blast radius)

### MP-1 (P1) — Applied-fill dedup has a check→append→record window; the durable guard is unwired

The inbound live door (`_apply_fill`, `engine.py:1123`) sequences:

```
seen()          → LRU read, no commit           (line 1152)
require_cash…   → proof only                      (1189–1217)
append(OrderFilled)                               (1221)
record_applied_fill(fill) → LRU COMMIT            (1228)
book                                              (1229)
```

Between `seen()` and `record_applied_fill`, nothing prevents a second concurrent publisher of the *same* fill from passing `seen()` too. Note the window spans the `with scope:` block: `seen()` at line 1152 runs **before** the ledger `transaction()` opens at 1183, so even the one serialization live wiring provides does not cover the check — it only covers proof→append→record. Today's immunity is an accident of having exactly one (single-threaded) publisher; nothing in the engine enforces that:

- `ThreadSafeReactiveBus` serializes `publish`, so today's publishers don't race — **but the engine never guarantees single-threaded inbound delivery** (tests drive `_apply_fill` directly; any future multi-threaded venue handler breaks it), and
- without a `CashLedger` the `scope` is a `nullcontext()` (lines 1183–1187), removing even the intra-section serialization.

Consequences:
- **Duplicate application**: venue re-delivers a fill; two threads interleave → position booked 2×, cash debited 2×, fees 2×. The SQLite event store will happily store both rows (every append mints a fresh UUID; `append()` deliberately does not dedup — `sqlite_event_store.py:17, ~1008`).
- **Restart**: the LRU is empty after a crash. The venue re-publishes unsettled fills; the first live fill after every restart is treated as fresh. The store's `fill_seen`/`record_fill` dedup index (`sqlite_event_store.py:1338–1438`) exists for exactly this and has **zero production callers** — `DurableFillDedup` (`idempotency.py:210`) is implemented, documented, and unused (verified: engine builds plain `FillDedup` at `engine.py:277`).

**Fix (pick one, in order of preference):**
1. Wire `DurableFillDedup` into `_apply_fill` when an event store is configured (it is in live boot) — it subsumes the LRU (`FillDedup` stays as the write-through cache inside it); one class, one lock, restart-safe.
2. At minimum, make the window atomic: move `seen()`+`record` into a single `check_and_record()` call *inside* the ledger `transaction()` scope and treat the durable append as pre-commit evidence — with an explicit test that two concurrent `OrderFilled`s apply once.
3. If (1) lands, delete or assert-kill the unwired `FillDedupStore` seam question; also call `fingerprints_agree()` at startup when a store is configured (it exists for this; 0 callers today).
**Test:** kill -9 mid-`record_applied_fill`; restart; re-publish the venue fill; assert position/cash/fees move by exactly one fill.

### MP-2 (P1) — Three position rebuild sites silently erase `strategy_id`

The domain carries `strategy_id` through `position_math.apply_fill`/`apply_split`/`apply_dividend` deliberately (per-strategy P&L). Three rebuild sites drop it, all on the live/reactive path:

| Site | Loss trigger |
|---|---|
| `position_accountant.py:92` (`on_fee`) | every fee-charging fill — i.e. live/paper/backtest whenever fees are on |
| `position_accountant.py:194` (`_remark_unlocked`) | **every quote tick** in any MTM'd session |
| `mark_to_market.py:105` (direct `Position(` rebuild) | every quote in the live MTM service |

Broker-provided positions (`dhan/upstox/client.py`) and reconciliation adoption **preserve** attribution — the engine itself is what erases it, within one tick of the first fill. Any downstream per-strategy PnL breakdown silently degrades to unattributed positions; tests asserting on `Position` do not assert this field, so nothing catches it.

**Fix:** pass `strategy_id=position.strategy_id` at the three sites (+ a property test: `fold(fills ++ fees ++ marks) preserves attribution`).

### MP-3 (P1) — The SELL fee gate blocks exits with zero idle cash (contradicts the repo's own rule)

Both fill doors share `_require_cash_for_fill` (`engine.py:1007–1044`) and `CashLedger.apply_fill` (`cash_ledger.py:183–202`):

```
SELL: debit = 0 + fee;  require_debit(fee)   # proof runs BEFORE the credit lands
```

A SELL whose fee exceeds current idle cash is refused **before** its own proceeds are credited — even though filling and then paying the fee from the proceeds is self-funding for any sane fee rate. The docstring is honest ("a sell out of an empty account is refused on a fee its credit would have covered… it fails closed"), but this contradicts the platform's own constitutional rule (ARCHITECTURE.md §7.3, and cash_ledger's design intent): **"Sells are never margin-gated on either path: blocking an exit is the one thing a risk layer must never do."**

Real-money scenario: account all-in across positions, idle cash = 0, an instrument gaps down and the strategy tries to flatten. The exit is refused by the risk layer. That is exactly the failure mode the rule exists to prevent.

**Fix:** for `SELL`, prove against **net proceeds** (`notional − fee ≥ 0` allows the trade; notional credit lands first, then fee debit — the code already credits notional and debits fee in steps 3–4), or exempt sells from cash proofs and enforce only a "non-zero free collateral OR order is a reduce" invariant in `RiskManager`. Keep BUY proof unchanged. The docstring must be rewritten to match whichever invariant is chosen; today the code and the doctrine disagree, and the code wins by default.

### MP-4 (P1) — A venue-confirmed fill the ledger calls unaffordable is dropped, not adopted

In the async door (`_apply_fill`), if `_require_cash_for_fill` fails, the fill is **not** applied, no OMS row exists beyond what the sync door wrote, and the exception is re-published as `ErrorOccurred` — which `ReactiveBus._isolate` logs and drops. Net effect: a venue execution that the local ledger cannot afford vanishes locally; the venue book and the local book now disagree, with no local artifact other than a warn log and a metric nobody has pinned an alert to. Reconciliation will see an unexplained position drift; adoption (which refuses on unsettled/failed gates) may not fire.

The defensible invariant is: **affordability governs placement; it cannot veto a fill that already happened.** A venue-confirmed fill is unreconcilable history. The correct handling is: book it, flag it (metric + `DriftItem` severity HIGH + reconciliation result), never refuse silently.

**Fix:** in `_apply_fill`, when the proof fails: still record an OMS `OrderFilled`/`PositionUpdated` with a degraded flag (new `Position.adopted=True` or a `ReconciliationResult(severity=HIGH)` marker event), plus `bus.fill.unadoptable` metric wired to the `oms-recovery` runbook. Venue failures stay loud; local un-affordability must not erase history.

### MP-5 (P2) — `MemoryIdempotencyGuard._completed` grows without bound

`record_result` keeps every completed receipt forever; `_reserved` is bounded but `_completed` is not. A long-lived paper/live process accumulates one dict entry per order, forever. Fix: ring-buffer the completed map (e.g. last 10k keys, matching `applied_fills_max` philosophy), or checkpoint `_completed` into the event store on shutdown. (Memory, not money — but slow-leak in the process that holds your order book.)

### MP-6 (P3) — Recovery stubs lack strategy attribution (accepted, document)

`_stub_order` (`recovery.py:92`) builds a FILLED stub for a fill whose `OrderPlaced` is lost; no `strategy_id`. That is the only honest answer (the strategy is unknowable from a missing event), but the stub's position — built through `position_math.apply_fill` — carries no attribution and nothing flags it during folds. Attribution survives for event-logged fills (`Fill.strategy_id` round-trips through `position_math`) ✓; only the stub path orphans strategy PnL. Document, and count orphans (`cash_recovery_stub_fills_total` metric).

---

## Verified strengths (do not touch in remediation)

- **AC-22 single door** — `submit()` is the only intake; `_setup_pipeline` subscribes to nothing but `OrderFilled`; a published `OrderRequest` is inert. The bus is observation-only by construction, not convention [V].
- **Three-state submission classification** (`engine.py:747–816`) — absent latch/`reaches_venue=True` ⇒ UNKNOWN, never a bare REJECTED for possibly-venue traffic; the UNKNOWN path trips the kill switch and the venue order is still reachable by it [V].
- **Append-before-mutate everywhere that matters** — `OrderPlaced`/`OrderAcknowledged`/`OrderFilled` all persist before the cache moves; inbound door refuses before the log so a refused fill leaves no trace [V].
- **`CashLedger.apply_fill` atomicity** — proof → book → notional → fee under one lock; lock order ledger→position documented and consistent; `require_debit` is the same formula both doors use [V].
- **Hash-chained event store** — verify-on-read raises `EventChainError`; latest-anchor cash fold (re-anchoring restates, never double-counts); restricted unpickler allowlist; hash function pinned per `schema_version` so v1/v2 rows both verify [V].
- **Recovery fill identity** — `fill_id` authoritative; equal-lot fallback bounded by order totals; `running + fill > total` ⇒ re-delivery — cannot double-count partials across restart [V].
- **Cancel/Kill-switch ordering** — kill switch skips `TERMINAL` but deliberately includes `UNKNOWN` (venue may hold it); engine cancel is venue-first for plain orders and super-order endpoint for brackets; reservation released on every failure path probed [V].
- **Bracket integrity** — `BracketOrderRequest` validates side-aware SL/entry/target at construction; modify of a bracket requires full composite; venue dispatch rule matches [V].
- **Fees counted once** — sync and async doors both single-count via `_fee_processor` + `CashLedger.apply_fill` step 4; no double-charge path found [V].
- **q2 (paisa) quantization** on averages and realized PnL — drift bounded and equal in both doors [V].
- **Fill fingerprint convergence** — `FillDedup.fingerprint` ≡ `sqlite_event_store.fill_fingerprint` (same 3-sample scheme pinned by test); `fingerprints_agree()` exists but has 0 callers — wire it at boot (see MP-1 fix 3).

---

## Remediation plan (fits Phase 1–2 of the architecture review)

| # | Change | Where | Test that proves it |
|---|---|---|---|
| MP-1 | Wire `DurableFillDedup` when store configured; single-commit window | `engine.py:1123–1160` | restart/republish property test; concurrent duplicate test |
| MP-2 | Preserve `strategy_id` at 3 rebuild sites | `position_accountant.py`, `mark_to_market.py` | attribution property test (fold, fill, mark, fee ⇒ attribution stable) |
| MP-3 | Sells: proof against net proceeds (credit-then-fee) or exempt sells from cash proof; rewrite docstring to match doctrine | `engine.py:1043–1044`, `cash_ledger.py:183–202` | zero-idle-cash SELL books; buy-side unchanged |
| MP-4 | Book venue-confirmed fills always; degrade to adopted+flagged, never drop | `engine.py:1213–1226` | venue-fill-after-local-reject adoption test (also listed in §8.2 of the architecture review) |
| MP-5 | Ring-buffer `MemoryIdempotencyGuard._completed` | `idempotency.py` | 100k-order retention cap |
| MP-6 | Metric + docs for fold stubs | `recovery.py` | fold test asserts counter increments |

**Ordering:** MP-1 and MP-4 first (both are "book can diverge from venue" bugs). MP-3 and MP-2 next (exit safety and attribution). The rest in the normal Phase 1.

## Relationship to the architecture review (§docs/audit/2026-11-06-principal-architecture-review.md)
- MP-1 is the production defect behind "DurableFillDedup unwired" (that doc, §1.3) — now with the exact window, the restart consequence, and the fix.
- MP-3 partially contradicts that doc's §7.3-quoted doctrine — the doc should be patched in the same PR to state the new SELL rule.
- MP-4 is that doc's Phase 1 P0 test requirement, now with a named mechanism (bus-isolation swallowing + drop semantics).
- MP-2 is new to this pass.

## Appendix — what was *not* re-verified here
Broker adapter internals (dhan/upstox clients, WS reconnect), paper broker's book matcher and wall-clock session gate (known divergence, §4.3 of the architecture review), and `reconciliation.adopt_venue_positions` in full (only the adoption's strategy-attribution behavior was checked, and it preserves attribution correctly — `reconciliation.py:581–660` [V]).
