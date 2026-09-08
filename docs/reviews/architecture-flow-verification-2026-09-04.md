# TradeX v4 — Architecture & Flow Verification Review (Principal Quant Engineer)

**Date:** 2026-09-04
**Branch:** `refactor/execution-state` (working tree)
**Method:** independent source-level verification of the claimed architecture, flow-by-flow, with the dated reviews (`principal-architecture-review-2026-09-04.md`, `architecture-flows-deep-dive-2026-09-04.md`, `tradex-target-architecture-and-refactoring-plan-2026-09-04.md`) used as hypotheses to confirm or refute. Every claim below was traced to code in this working tree, not to the docs.
**Tests run green:** trading/tests/execution + interface (526), trading/tests/runtime + parity + sdk (363), brokers/tests/common contracts (35), domain/tests. (WS resilience tests need optional extras; unrelated to this branch's changes.)

---

## 1. What the source confirms (the dated reviews hold up)

| Claim | Verification |
|---|---|
| One composition root (`runtime.startup.boot()`) | Confirmed. `boot()`/`_boot_tail` is the only object-graph builder; paper/live/replay all funnel through it. Fail-closed with `_safe_teardown` rollback on every boot failure. |
| C1 end-to-end idempotency | **Confirmed closed.** `trade-feed.ts` forwards `Idempotency-Key` (lines 128–206); `OrderEngine` reuses `clientToken` on retry and blocks same-token duplicates; routes `_require_idempotency_key` → 422 when missing; `OrderRequest.correlation_id` → `guard.check_and_reserve` → replay of completed keys, reservation kept on uncertain submission. |
| C2 mark-to-market + fail-closed live risk | **Confirmed closed.** `MarkToMarketService` subscribes to `Quote`, conservative marks (long→bid, short→ask, LTP fallback), stale-quote rejection, `PositionUpdated` published. `RiskManager._equity_pnl()` sums realized+unrealized, so daily-loss/drawdown gates now see open losses. Live boot forces `require_fresh_marks`; opening exposure requires a fresh-marked book + fresh quote within `max_mark_age_seconds` (5 s). |
| Fill math single-sourced | Confirmed: `position_math.apply_fill` is the shared weighted-average model (PositionManager, BacktestEngine, splits/dividends). A fill invalidates the mark (`mark_price=None`) until the next quote re-marks — correct invalidation semantics. |
| F3 modify-risk ordering | **Confirmed fixed.** `engine.modify` runs the H5 risk re-check *before* the venue dispatch; a denied modify never reaches the broker. |
| Brackets through the canonical spine | Confirmed: `POST /orders/bracket` → engine → `BrokerFillSource` → `submit_super_order`; legs persist on the OMS record (SQLite schema too); modify/cancel dispatch to super-order endpoints; capability preflight returns 422 before the engine for non-capable brokers. |
| Fill identity (C3) | Partially hardened: `TradeBookFillIdResolver` stamps deltas with real exchange trade ids from `GET /trades`; engine dedups on `fill_id` when present. The ambiguous `(order, side, qty, price)` composite remains the fallback for venues without a trade book — accepted, but must stay a tracked risk. |
| Startup reconciliation + kill switch | Confirmed: live boot pulls orderbook+positions, `engine.reconcile`, HIGH/CRITICAL drift trips the switch *before* `session.start()`, so a diverged book never reaches READY. |
| Single-writer lock, durability | Confirmed: live boot acquires `runtime/live/<broker>.writer.lock` fail-closed; SQLite guard + order store restore and mirror lifecycle events. |
| `trading/events/` is production-dead | Confirmed: the ~2.7k LoC stack imports only itself; zero production imports outside `trading/scripts/probe_review_fixes.py` and its own tests. Deletion is Phase 3 of the plan, not a correctness risk today. |

## 2. Flow-by-flow assessment (principal lens)

**Order flow (submit).** Kill-switch → reserve → risk → fill → OMS → publish is the correct ordering. Reservation precedes the external mutation; risk rejection releases the key; a boundary-crossed failure keeps it reserved and raises `OrderSubmissionUnknownError`; ACK-only live submissions `record_result` immediately so a retry can never double-submit. This is textbook correct idempotency design.

**Fill flow.** Cumulative-delta with cache-anchored dedup + fingerprint LRU (50k, eviction metric) is sound. The unknown-order path (minimal FILLED record) means a fill for an order the OMS never saw still lands in the position book — good recovery behavior.

**Risk gate.** Correct conservative direction: reductions/flattening always allowed; `_increases_exposure` is sign-aware; per-strategy budgets; deterministic `now` injection for backtests with tz-mismatch guard. Daily-loss baseline is taken at first check of the day and rolls on date change — carried-over positions are banked at their mark, which is the right accounting.

**MTM math.** `(mark − avg) × signed_qty`, q2-quantized, Decimal throughout; stale/out-of-order quotes rejected; `marked_at` monotonic. Matches the design contract §2.6/§4.4.

**Replay/backtest.** Backtest drives the same engine spine via `SimulatedFillSource` and resets the rate window per run (reproducibility). Tick-replay uses a per-connection `mini_bus` + `SyntheticTickGenerator` → the same `BarAggregator` path as live (source parity). **But** tick-replay never touches a replay-scoped session (see N3).

## 3. New findings (not surfaced in the dated reviews)

### N1 🔴 P0 — Plain live cancels never reach the venue
`DELETE /orders/{id}` → `engine.cancel()` transitions the OMS cache to CANCELLED and stops. The venue cancel (`_fill.cancel` → `broker.cancel_order`) is invoked **only** from `trip_kill_switch` (engine.py:1171). Consequences in live mode:

- The user sees "cancelled"; the venue order is still working and can still fill.
- If it fills, `OrderManager.on_order_filled` uses `replace()` — not the FSM — so the CANCELLED order silently flips to PARTIALLY_FILLED/FILLED with a position update. The book lies twice: first about the cancel, then about the resurrection.

The deep-dive doc calls this "OMS-local semantics … by design," but design rule 18 ("local cancellation state is not proof of venue cancellation") makes the *normal user cancel path* the one place that rule is violated. Brackets already do it right (venue-first). **Fix:** in `engine.cancel`, dispatch `_fill.cancel` for plain orders (venue-first, OMS second), mirroring the bracket path; add a contract test: live-mode cancel must invoke `broker.cancel_order` exactly once and keep OMS ACK on venue failure.

### N2 🟠 P1 — Modify/cancel idempotency keys are validated, not deduplicated
`Idempotency-Key` on PUT/DELETE is required and validated, but `engine.modify`/`engine.cancel` never touch the guard. A double-fired PUT dispatches the venue modify twice; the key is also never bound to a request hash, so a retry with mutated payload still applies. C1's contract (reserve → replay → conflict-on-reuse) covers submit only. **Fix (Phase 1.5):** route the cid through `check_and_reserve`/`record_result` for modify and cancel as well.

### N3 🟠 P2 — Replay isolation still open (C4)
During WS tick-replay on a live session, `POST /orders` executes against the live account while the chart shows historical bars; there is no `session.mode` gate on order submission. The plan lists this for Phase 3; until then, a one-line gate (`mode == "replay" → reject/422`) in the routes would close the practical hazard without the full session-driver work.

### N4 🟡 P2 — Kill-switch cancel order: OMS first, venue second
`trip_kill_switch` cancels OMS-side first, then calls the venue, logging failures. On venue failure the OMS already claims CANCELLED — the same lie as N1. Flip to venue-first per order, and only then transition OMS; report failures per order_id with the OMS left ACK.

### N5 🟡 P2 — In-flight duplicate submit → RuntimeError → 500
A retry while the first request is still in flight hits `check_and_reserve`'s `RuntimeError("already reserved")`, surfaced as an opaque 500. The client cannot distinguish "still processing" from failure. Return a deterministic `409`/`202`-style "reserved/in-flight" response (or `IdempotencyDuplicate` with `pending=True`) so the retry policy can wait, not panic.

### N6 🟡 P2 — Chart-interface `placeOrder()` mints a fresh key per call
`TradexTradeFeed.placeOrder()` (openalgo `TradeFeed` interface) generates `crypto.randomUUID()` per call; only the `OrderEngine` path reuses `clientToken`. If anything retries through that interface, idempotency silently disappears. Route it through the same token-stable helper.

### N7 🟡 P2 — SELL notional gate can understate short exposure
`RiskManager._mark_for_trade` uses the request's limit price for both sides. A short limit below the market understates the notional/order-value gate vs. an ask-side mark. The MTM policy is already conservative (long→bid, short→ask); the notional gate should mirror it: BUY → request price or ask, SELL → request price or bid, for consistency.

### N8 🟢 P3 — Cleanups still open (all known, none new)
`modify_order` calls `_require_idempotency_key` twice (F4); `PaperBroker._update_position` duplicates the weighted-average math (extract to domain); writer-lock path is cwd-relative (F6); `trading/events/` deletion; `TradeBookFillIdResolver` does one REST call per delta (a 2 s TTL cache already exists — fine).

---

## 4. Scores (principal lens)

| Dimension | /10 | Basis |
|---|---:|---|
| Order-path correctness | 8 | Idempotency/risk/fill ordering is right; N1/N2 subtract |
| Risk & MTM | 9 | conservative marks, fail-closed live, reductions always allowed |
| Mode parity | 8 | one spine, one fill model, one MTM fn; replay trading isolation open |
| Durability/recovery | 8 | SQLite mirror + startup reconcile + kill-switch trip; in-flight ambiguity UX weak |
| Simplicity | 8 | −147 LoC migration, one root, one pipeline; dead `events/` stack pending deletion |
| Concurrency | 8 | TS bus, locks on shared state; `_cid_for_order` unlocked (benign today) |
| Testing | 9 | parity goldens, contract suites, boot-safety; missing: live-cancel-venue contract (N1) |

**Verdict.** The architecture is genuinely strong: one execution spine, one accounting model, conservative risk with fail-closed live dependencies, and end-to-end idempotency that actually reaches the engine. The dated reviews' claims verified true. The one real-money defect this pass found is **N1 — the user cancel path never tells the venue for plain live orders** — it is a small, well-contained fix (venue-first dispatch + one contract test) and should land before any live deployment alongside the Phase-1 blockers.