# Recon/Adoption Subsystem - Quant Risk Review (QUANT lane)

Date: 2026-09-28 · Branch: `wave0-1-recovered` · Scope: `safety.py` adoption, `reconciliation_monitor.py`, `trading_cache.py` / `position_accountant.py` write paths, `trade_metrics.py` accounting inputs.

Source facts verified by direct file reads this session (line refs are current working tree). No source files edited.

---

## 1. Position-true-up: adopted `avg_price` vs the local fill log

### 1.1 What adoption actually writes

`_adopted_position` (safety.py:1037) writes **the venue's object fields** onto a fresh `Position`:

| Field | Value after adoption | Source |
|---|---|---|
| `quantity` | `_venue_quantity(venue)` | venue snapshot |
| `avg_price` | `_venue_avg_price_value(venue)` (raw, **not q2-ed**) | venue snapshot |
| `realized_pnl` | `_venue_realized(venue)` (**raw, not q2-ed**, default 0 when venue omits) | venue snapshot |
| `unrealized_pnl` | `0` | forced |
| `mark_price` | `None` | deliberately dropped (re-mark on next quote) |
| `strategy_id` | `local.strategy_id` (preserved) | local |

Local fill path (`PositionAccountant.on_fill` → `domain.position_math.apply_fill`) writes `avg_price = q2(total_cost/qty)` (fill price only, **fees are NOT capitalised into avg_price** — they hit `realized_pnl` via `on_fee`, position_accountant.py:81-96). So "local avg 100.00 incl fees" is not how the code books basis; fees are a realized-P&L deduction, not a basis adjustment. The restatement issue below stands regardless (venue avg differs from local avg for price/vwap/quantization reasons).

### 1.2 Worked example (as posed): 10 shrs, local avg 100.00, venue avg 99.95, mark 102

Let F = fees already deducted from `realized_pnl` via `on_fee` (per-fill, `q2`).

**Local book (fill log state):**

- `avg_price = 100.00` (q2 of fill arithmetic)
- `realized_pnl_local = -F` (only fees so far; position still open)
- `unrealized = (102.00 - 100.00) × 10 = 20.00`
- `total_pnl_local = 20.00 - F`

**After adoption of venue row (qty agrees = 10, price disagrees 0.05 > tolerance... see §2 — 0.05 > 0.01 so this row IS adopted):**

- `avg_price = 99.95` (venue raw, not q2)
- `realized_pnl_adopted = R_v` where `R_v` is **whatever the venue's realized figure is** — Dhan/Upstox position payloads often carry no per-position realized P&L, in which case `_venue_realized` defaults to **0** (safety.py:1088-1100).
- `unrealized = 0` (forced; re-marked to `(102.00 - 99.95) × 10 = 20.50` on the next quote)
- `total_pnl_adopted = R_v + 20.50`

**Reconciliation of the two:**

```
total_pnl_adopted - total_pnl_local = (R_v + 20.50) - (20.00 - F)
                                    = R_v + F + 0.50
                                    = R_v + F + (100.00 - 99.95) × 10     # basis restatement
```

- If venue omits realized (R_v = 0): P&L **jumps by F + 0.50** on adoption. The fee deduction F that `on_fee` booked into `realized_pnl` is **destroyed** (replaced by 0).
- If venue reports its own lifetime realized R_v: the local fill-log realized (fees + any prior round-trip closes booked by `apply_fill`, q2-ed per fill) is **replaced wholesale** by a venue figure computed under a different fee model and quantization. Neither equals `trade_metrics`' FIFO figure.

### 1.3 Is the adopted `realized_pnl` consistent with the fill log for `trade_metrics`?

**No, structurally.** `trade_metrics.round_trip_trades` (trade_metrics.py:135) re-derives P&L **purely from the fill stream** (`gross = (price - lot.price) × matched`, `net = gross - costs` from `cost_by_order`). `positions_pnl_by_strategy` instead reads `pos.total_pnl` off cached `Position` objects. The two ledgers after an adoption:

```
Δ(realized) per adopted instrument
  = R_venue(venue fee model, venue quantization, lifetime)
    − Σ fills apply_fill realized (q2 per fill) − Σ on_fee fees (q2 per fill)
```

Unbounded and un-auditable; it is a **whole-ledger swap**, not a basis delta. Three concrete inconsistencies after one adoption:

1. `pnl_by_strategy` (fill log) ≠ `positions_pnl_by_strategy` (book) for the same strategy.
2. Later local closes use the **adopted venue avg** as `old_avg` in `apply_fill` (position_math.py:131-152), so subsequent realized = `(fill_price − venue_avg) × qty`, diverging from the FIFO trade log by `(local_avg − venue_avg) × qty_closed` **per adoption generation**.
3. The venue's `avg_price` is stored **un-quantized** while the local kernel q2-s everything; downstream `q2` consumers (risk, MTM) now read a non-paisa basis.

**Fix shape (recommend):** adoption should transfer **quantity** (the venue is authoritative for "how many"), but re-derive basis and realized from the local fill log, or at minimum carry `realized_pnl = local.realized_pnl` and set `avg_price` only when `local is None` (venue-only rows). If true-up of avg_price is desired, write the basis delta into `realized_pnl` as a restatement entry, not a silent swap.

---

## 2. `_ADOPTION_TOLERANCE = 0.01` INR on avg_price

Current rule (safety.py:864, `_agrees_on_price` :988-993):

```
agree ⇔ |avg_local − avg_venue| ≤ 0.01        # per-share, absolute
```

### 2.1 The hidden error the prompt quantifies — confirmed and extended

The tolerance does **two** jobs, and the per-share constant is wrong for both:

| Effect | Formula | 100,000-shr @ 0.01/shr | 1-share lot @ 0.01/shr |
|---|---|---|---|
| **(a) Dormant basis error**: rows within tolerance are `retained`, never corrected | `abs(Δavg) × qty` ≤ `0.01 × qty` | **1,000 INR silently unreconciled** | 0.01 INR |
| **(b) Ping-pong threshold** (see §4): if venue quantizes avg to 2dp and `Δavg` sits just over 0.01 (e.g. 0.015, which rounds to 0.02 vs local 0.00/0.05-grid) | flip magnitude per flip = `abs(Δavg) × qty` | **1,500 INR per flip** | 0.015 INR |

So the constant bounds per-share error but the **risk is notional**: `max_hidden = 0.01 × qty_max`. On an index-futures-scale position that is 4 digits of INR per instrument and unbounded across instruments (`Σ 0.01 × qty_i`).

### 2.2 Recommendation: notional-based tolerance with a per-share floor

```
agree ⇔ |Δavg| × qty ≤ max(NOTIONAL_TOL_INR, PS_TOL_INR × qty)

Suggested: NOTIONAL_TOL_INR = 25.00 (≈ one round-trip's q2 noise × margin),
           PS_TOL_INR = 0.005 (half-paisa-grid, catches venue 2dp quantization skew).
```

Properties:
- Large books: absolute hidden error capped at **NOTIONAL_TOL_INR per instrument** (25 INR, not 1,000) regardless of qty.
- Small lots: per-share floor keeps idempotency (a 1-share lot is never adopted over 0.01 of q2 rounding), preserving the "re-derive after adoption reports no change" invariant.
- Justification for keeping *any* tolerance: `_agrees_on_price`'s equality branch is what makes adoption idempotent and keeps the log from churning on venue quantization noise. Removing tolerance entirely trades 1,000 INR of bounded, monotone dormancy for unbounded log churn and the §4 oscillation at scale. A **notional** cap is the right axis, not a per-share one.

Also fix: `_adopted_position` stores venue avg **un-quantized** (safety.py:1070 `Price(value=_venue_avg_price_value(...))`); q2 it so tolerance comparisons are apples-to-apples with the q2-ed local grid.

---

## 3. Fill/adoption race: exact stale-snapshot window

### 3.1 The locking hole (confirmed, worse than "no atomicity")

Write-path lock analysis:

| Writer | Lock held across read-modify-write | File |
|---|---|---|
| Fill path (`PositionAccountant.on_fill` / `fill_with_remark` / `on_fee`) | `cache.position_lock(instrument)` (per-instrument RLock) | position_accountant.py:46-52, 64-68 |
| Mark-to-market | `cache.position_lock(instrument)` | (per trading_cache.py:33-36 contract) |
| **Adoption** (`_apply_venue_adoption` → `cache.set_position`) | **only `TradingCache._positions_lock`** (dict-level) | safety.py:1319-1340; trading_cache.py:138-141 |

`set_position` never touches `position_lock(key)`. The dict lock serialises dict writes against each other but **not** against the fill path's read-modify-write, which calls `cache.get_position` / `update_position` (each individually under `_positions_lock`) **while holding `position_lock`**. Interleaving:

```
Fill (holds position_lock):  read pos@10        ←
Adoption (holds only dict):                    write pos@venue(qty 0/old)
Fill (holds position_lock):                    write pos@apply_fill(stale base)   → fill booked on pre-adoption base, adoption lost OR fill lost
```

Last-writer-wins per key with **two different lock domains**. The fill's read-modify-write is only mutually exclusive with other `position_lock` holders; the adoption bypasses it entirely. Cash is separately consistent (fill runs inside `CashLedger.apply_fill`'s single ledger lock via the `book` hook, engine.py:1064-1096) — so a lost fill produces **cash ≠ position** divergence too.

Note the docstring contract at trading_cache.py:33-37 ("every component that mutates a position ... must take the same lock") is violated by `_apply_venue_adoption`. Fix is 2 lines: in `_apply_venue_adoption`, wrap each `write(position)` in `cache.position_lock(position.instrument)`.

### 3.2 Order-status timeline and where the guard lifts

`_UNSETTLED_ORDER_STATUSES` = {NEW, PENDING, ACK, SUBMITTED, PARTIALLY_FILLED, UNKNOWN} (safety.py:805-813). Protection ends the moment the last in-flight order on the instrument leaves that set.

```
t0  submit          → NEW/PENDING        guard ON
t1  venue ack       → ACK                guard ON
t2  partial fill    → PARTIALLY_FILLED   guard ON   (resting leg may fill post-snapshot)
t3  FINAL fill applied locally:
       _apply_fill_effect → order.transition_to(FILLED)      (engine.py:796-800 marks FILLED
                                                              BEFORE OrderFilled is published)
       → status = FILLED ∉ _UNSETTLED_ORDER_STATUSES
       → guard OFF at t3                        ★ GUARD LIFTS HERE
t3+  venue get_positions still returns the snapshot assembled before t3's venue-side
     position update is readable
```

For an **UNKNOWN** boundary order (engine.py:735 filings): the guard covers it while UNKNOWN, and a late resolution to FILLED has the same lift point t3. But the venue-side position for a fill the venue "accepted and has not reported" (safety.py:833-836 admits exactly this) can be absent from `get_positions` for the whole venue-side posting latency — so for UNKNOWN resolves the stale window below is measured from venue posting, not local status change.

### 3.3 The window, in the code's constants

The monitor polls on `_stop.wait(RECONCILIATION_INTERVAL_SECONDS)` **before** the first poll (reconciliation_monitor.py:~250 `_run`; `RECONCILIATION_INTERVAL_SECONDS = 30.0` at :60). Each poll reads `get_positions()` then applies. A snapshot taken at poll k (t = 30k) can carry a venue state from up to one `get_positions()` read-latency `L_read` before t = 30k.

Define:
- `T_venue` = venue-side posting latency for the fill (fill executed at venue before t3, but position row visible on `get_positions` only after `T_venue`).
- The dangerous condition: **guard OFF ∧ snapshot reflects pre-fill venue state ∧ apply overwrites the fill**.

```
Window W = (t_snap + L_stale) − t3   where t_snap = the first poll-snapshot
                                          whose pre-fill staleness survives,
                                          bounded by the worst placement of t3:

   t3 ∈ (30k + L_read, 30(k+1)]  ⇒  poll at 30(k+1) is pre-fill ⇒ W ≤ 30 s + T_venue + L_read
   t3 ∈ [30k, 30k + L_read]     ⇒  that poll may catch the fill ⇒ W ≈ T_venue
```

**Worst case (measured in code constants):**

```
W_max = RECONCILIATION_INTERVAL_SECONDS + T_venue + L_read
      = 30 s + T_venue + L_read          (T_venue, L_read external, ~0.1–3 s live)
      ≈ 30–36 s                          for Dhan/Upstox REST posting latency
```

**With Finding 2's gap (venue-only branch, safety.py:~956-973, unsettled guard NOT applied)** the window is worse in kind, not just duration: for an instrument with **no local position row yet** (first fill of a new name, or a cleared-then-refilled name) `derive_venue_adoption` takes the `set(venue_by_id) - set(local_by_id)` branch which **never consults `unsettled` at all**. There, a stale venue row (pre-fill quantity, or an unrelated manual venue trade) is adopted **unconditionally** — the guard that exists only shortens W on the both-sides branch to `T_venue`-free form for settled instruments. The `skipped` reason `unsettled_fill` is unreachable on that branch even when `iid ∈ unsettled`.

Additional amplifier: `_unsettled()` fail-opens (safety.py:1422-1433: exceptions return `frozenset()`), so during any order-read hiccup W becomes **the full stale window with no guard at all** on both branches.

---

## 4. Worst-case P&L misstatement per poll, current code (Findings 1 + 2 unfixed)

### 4.1 Per-poll components

| # | Mechanism | Per-poll magnitude | Direction |
|---|---|---|---|
| A | Basis restatement on adopted row (§1) | `R_v − realized_local + (avg_v − avg_l) × qty` | one-way level shift |
| B | Fee-deduction destruction (`realized` 0-default) | `+ Σ on_fee fees` (F) | upward (flatters) |
| C | Race overwrite (§3): fill lost or adoption lost | up to `qty_fill × abs(fill_price − snap_avg) + fees` per raced fill | either |
| D | Venue-only gap (Finding 2): stale/foreign row over live fill | `Δqty_snapshot × (mark − snap_basis)` | either |
| E | Refused/failed poll (Finding 1's world: reconcile no-op) | 0 written | drift **unreported** (monitor `last_drifts` permanently empty, engine.py:1387 appends a 0-drift event every 30 s) |

Numeric worst case for a single instrument per poll: `|A| + C + D` where the prompt's example scales linearly:

```
basis term   : |avg_v − avg_l| × qty        e.g. 0.05 × 100,000 = 5,000 INR
race term    : entire fill notional          e.g. 100.00 × 10 shrs = 1,000 INR (unbounded in qty)
venue-only D : whole stale position restated (unbounded in position size)
```

**Unbounded per instrument; linear in Σ across instruments.** Nothing in the code caps it (FUND_DRIFT_THRESHOLD_INR gates boot, not adoption).

### 4.2 Does idempotent convergence bound cumulative error?

**No. Idempotency is conditional, and the condition is exactly what the race breaks.**

- **Convergent regime**: for a settled instrument and a *converged* venue row (venue itself stable across polls), `derive_venue_adoption` is idempotent (re-derive after apply ⇒ all `retained`, per the tests). Cumulative error = **one** application of A+B per instrument per adoption event. Bounded.
- **Oscillating regime** — two distinct cycles are reachable under current code:

**Cycle 1 — fill/adoption lost-update ping-pong (needs the §3.1 lock hole):**
```
poll k   : adoption writes venue row (pre-fill base) over fill F's result   [C: fill lost]
fill path: next fill/mark does read-modify-write on its stale read base,
           resurrecting F or writing an inconsistent composite
poll k+1 : book now disagrees with venue ⇒ adoption rewrites venue row       [adoption wins]
poll k+2 : fill-path RMW again on a raced read base ...
```
Each cycle re-applies `|Δavg| × qty + Δqty × basis`. **Not convergent** — period-2 oscillation with amplitude set by the race rate (fill rate × W_max window overlap). Expected misstatement rate ≈ `fill_rate × P(fill lands in W) × per-fill notional`, `P ≈ W_max/30s` per fill under continuous polling.

**Cycle 2 — venue quantization flip-flop (needs no race):**
```
local avg  = q2 grid (paisa), e.g. 99.90
venue avg  = 99.915 reported stably
|Δ| = 0.015 > 0.01 ⇒ adopt ⇒ local 99.915 (un-quantized!)
next fill applies q2 → local snaps to 99.92 or 99.91
|Δ| = 0.005 ≤ 0.01 ⇒ retained (settled) ... until next fill shifts avg again over the line
```
Adoption writes **un-quantized** venue avg (§2.2), so the adopted value is itself off the q2 grid and the next fill's `q2(total_cost/qty)` lands on the other side of the tolerance line. Amplitude per flip `= |Δavg| × qty` (the prompt's 1,000 INR scale at 0.015 and 100k shares). Low frequency (requires an in-between fill) but **unbounded cumulative** over a long session.

**Verdict:** cumulative error is bounded only in the quiet-book regime. Under live fill flow the worst case is `O(polls × Δqty × basis)` — an **oscillation, not a convergence**. Mitigations, in leverage order: (1) take `position_lock` in `_apply_venue_adoption` (kills Cycle 1's lost update), (2) apply the unsettled guard to the venue-only branch (kills D), (3) notional tolerance + q2 the adopted avg (shrinks Cycle 2 amplitude below noise), (4) carry `local.realized_pnl` through adoption (kills B outright).

---

## 5. `venue_adoption_last_adopted` gauge staleness

`_write_metrics` (reconciliation_monitor.py:344-374):

```
if applied:  gauge(venue_adoption_last_adopted).set(len(plan.positions))
else:        counter(refused, reason) … ; return   # gauge untouched
```

Semantics actually emitted: **"how many instruments the last APPLIED adoption wrote"** — a per-event magnitude, not a timestamp and not a state. Three staleness hazards for any alert keyed on it:

1. **Freeze on refusal**: a refused poll (failed/partial/stale read) leaves the last value indefinitely. A sustained venue outage freezes the gauge at its last figure while the book silently diverges. An alert like `gauge > 0 for 5m` ("adoption keeps rewriting") goes dark exactly when adoption is broken; `gauge == 0` ("all quiet") reads healthy through an outage.
2. **Silent zeros**: an applied poll with an empty plan sets `0` — indistinguishable from "converged" and from "adopted nothing because Finding 2 cleared everything as stale". The gauge cannot express "adoption wanted to write 3 but was refused".
3. **No freshness signal**: the metric family has `venue_adoption_refused_total` and `venue_read_failed_total` counters (monotone, need `rate()` joins to detect current state), but no `last_success_timestamp`. Prometheus staleness (`absent()`/`staleness` heuristics) cannot distinguish "monitor thread dead" from "all polls refused" from "nothing to do" — all three leave the same flat line.

**Recommendations:**
- Rename or re-key the alert to `time() - venue_adoption_last_success_timestamp_seconds` (new gauge set on every `applied` poll), alert on `> 2 × RECONCILIATION_INTERVAL_SECONDS = 60 s`. That single gauge separates the three cases above.
- If the magnitude semantics are wanted, split: `venue_adoption_last_planned_total` (set on EVERY poll, from the plan size) vs `venue_adoption_last_applied_total` (set on applied). The gap `planned − applied` is precisely the un-actioned drift an operator must see.
- Any alert using the current gauge must be paired with `increase(venue_adoption_refused_total[5m]) > 0` as a muting condition, or it will read healthy through every outage.

Note the same freeze applies to `monitor.adopted_count` / `last_adoption` introspection (reconciliation_monitor.py:~215-235) which reads the same outcome object — the safety surface (`build_safety_report`) can render a stale "adopted N" with no age indicator.

---

## Summary of quantified risk (ranked)

| # | Risk | Bound | Fix cost |
|---|---|---|---|
| 1 | Realized-P&L swap on adoption (§1) | unbounded per instrument; destroys fee ledger | carry `local.realized_pnl`; re-derive basis |
| 2 | Fill/adoption lost update (§3.1) | whole fill notional per race; oscillatory (§4 Cycle 1) | 2-line `position_lock` wrap |
| 3 | Venue-only unsettled gap (Finding 2) | whole stale position restated; guard unreachable | add `if iid in unsettled: skip` on that branch |
| 4 | Per-share tolerance (§2) | 0.01 × qty = 1,000 INR @ 100k shares | notional tolerance + q2 adopted avg |
| 5 | Gauge staleness (§5) | alert blindness during outages | last-success timestamp gauge |
