# TradeX v4 Production-Readiness Programme — Design

**Date**: 2026-09-29
**Status**: Approved design (brainstorming path: architectural)
**Source**: Pre-production architecture review, findings ledger F-01…F-47, at
[`docs/audits/2026-09/2026-09-29-runtime-findings-ledger.md`](../../audits/2026-09/2026-09-29-runtime-findings-ledger.md)
(entries re-derived from the tree at `3a1797e`, with file:line evidence).

## Summary

This spec defines the programme that closes the architecture review's findings
(F-01…F-41) across Phases 0–7 and brings TradeX v4 to production readiness. It fixes
the *process*: how work is planned, parallelized, verified, reviewed, and gated. Each
phase gets its own implementation plan (via writing-plans) and is executed by a
parallel agent team under a hard gate chain.

## Context

The review established:

- The architecture is worth keeping: domain kernel, single-door order pipeline,
  hash-chained event log with crash recovery, adoption reconciliation, and a real
  parity discipline.
- Six CRITICAL defects block production:
  - **F-39** bar aggregation is never wired in production — `MarketFeed` publishes
    only `Quote`/`Depth`, so `on_bar` never fires in paper/live
    (`market_feed.py:490-507`; `wire_bar_aggregation` at `bar_aggregator.py:1013`
    has zero production callers).
  - **F-40** the production sizer is the placeholder — `startup.py:673` builds
    `ReactiveStrategyEngine` with no `sizer=`, so paper/live size at a fixed
    ~₹95k notional (`sizing.py:237-265`) while backtest uses risk budgets.
  - **F-12** `session.broker.submit_order` is an ungated venue door
    (`base.py:143/336-341`; slice fan-out `upstox/_orders.py:196`). Closed in two
    parts: **F-12a** engine-side permit issuance (Phase 0) and **F-12b** adapter
    wall requires the permit (Phase 3, `base.py`).
  - **F-01** the reconcile-check record path is a dead chain — the seam calls
    `engine.record_reconciliation` which `ExecutionEngine` does not have
    (`safety.py:_record_check`; `session_impl.py:372` owns the method);
    `last_checked_at` derives only from durable events (`safety.py:596`); report
    reads append markers as a side effect (`safety.py:604` → `engine.py:1407`).
  - **F-19** `adjust_series` is broken on real `Candle` objects (reads
    `getattr(c, "open")`, real field is `c.ohlc.open.value`,
    `market.py:155-159`) and masked by `MockCandle`-shaped tests
    (`test_corporate_actions_enhanced.py:9`).
  - **F-26** durable-before-mutate is advisory outside live — `_append` swallows
    store failures unless `require_durable_events` (`engine.py:329-364`), which
    boot sets only for `mode == "live"` (`startup.py:560-564`).
- 13 HIGH findings and a set of MEDIUM/LOW findings follow behind them (full
  ledger: [`2026-09-29-runtime-findings-ledger.md`](../../audits/2026-09/2026-09-29-runtime-findings-ledger.md)).
- False confidence is a first-class defect class in this repo: parity tests
  hand-wire the exact production wiring that `boot()` omits; mock shapes diverge
  from domain types. Verification must therefore prove **production wiring**, not
  fixture wiring.

Process decisions (taken with the human partner):

1. **Scope C** — one master plan covering Phases 0–7.
2. **Gates A** — hard gate chain: every wave is failing-first TDD → implementation →
   review → full CI gates green → commit → next phase. A red gate blocks the next
   phase. (Consistent with the project doctrine in
   `docs/specs/remediation-and-hardening.md`.)
3. **Verification B** — every phase additionally proves real wiring with a
   boot-level behavior smoke (real `boot()`, synthetic feed, fake clock), not just
   test suites.
4. **Approach 1** — phase-serial waves, intra-phase parallel agents on
   file-disjoint lanes; one reviewer agent per phase after integration.

## Architecture of the Programme

### Team topology (per wave)

- **Coordinator**: dispatches workers, reviews every diff before it counts
  (requesting-code-review), validates claims at source, integrates, runs gates,
  commits. No worker marks work complete — the coordinator verifies first.
- **3–4 worker agents per wave**, each owning exactly one file-disjoint lane.
  Test-driven development is mandatory: no production code without a watched
  failing test first.
- **1 reviewer agent per phase**, fired after integration, given
  `DESCRIPTION / PLAN_OR_REQUIREMENTS / BASE_SHA / HEAD_SHA` only (never the
  coordinator's session history).
- Workers inherit the coordinator session's model.

### Wave protocol (every phase, no exceptions)

```
0. git state clean (Wave 0 integrator lane first commits or stashes the dirty tree)
   → record BASE_SHA
1. RED      — each lane writes failing tests for its findings, watches them fail
2. GREEN    — minimal fix, watch pass, then refactor
3. Self-run — lane's tests + the gate set on the touched area
4. Report   — evidence per fix: test names, before/after behavior, file:line
5. Coordinator review — every diff reviewed; Criticals fixed before integration
6. Integrate — merge lanes; run the gate set
7. Behavior smoke — boot-level proof of the phase's wiring claims
8. Reviewer agent — on BASE_SHA..HEAD_SHA; Criticals fixed immediately,
   Importants before proceeding, Minors recorded
9. Commit    — one commit per lane where clean; one per phase guaranteed
10. Gate chain — red gate blocks the next phase
```

### Lanes and phases

File ownership is **exclusive per lane per wave**. A lane that needs a file outside
its ownership stops and reports; the coordinator re-lanes.

| Phase | Wave | Lanes (file-disjoint) | Extra proof |
|---|---|---|---|
| 0 Stop the bleeding | W0 | Integrator lane alone: commit/stash the dirty tree; land the F-05 contract fix with its test | clean tree; contracts green |
| | W0.5 | A `engine.py` + `interfaces/safety.py`: F-01 recorder + report-read side-effect removal (both sides of the chain), F-35 tri-state drift report, F-26 fail-closed appends, F-13 kill-switch on modify, F-12a engine-side permit issuance. B `runtime/startup.py` + `brokers/paper/adapter.py`: F-39 wire bars, F-40 wire `account_sizer`, F-34 `configure_runtime_cache`, **F-43** the feed-recovery missing-bars allowance enforced in `recover()` and threaded from a new `FeedConfig` field. C `market_data/corporate_actions.py`: F-19 real-`Candle` adjust keyed to ex-date + test rewrite with the domain `Candle` | boot smoke: synthetic quotes → `on_bar` fires and orders carry `account_sizer` sizing; a timer poll makes `reconciliation_checked` truthful with a fresh `last_checked_at`; a report read appends nothing; a replay with a hole over the allowance leaves the feed `HALTED` |
| 1 Domain corrections | W1 | A FSM truth: F-02/03/04 — `UNKNOWN` terminality, delete `SUBMITTED`, all status writes via `Order.transition_to` (`order_manager.py`, `_make_order`). **F-44** UNKNOWN chart tier, in the same lane and wave as F-02/03/04 so the status vocabulary moves once. B drift vocabulary: F-10 one vocabulary (`events.py` / `reconciliation.py` / `safety.py`). C lot-size + currency on `Money` | Hypothesis FSM property test |
| 2 OMS/risk boundaries | W2 | A engine split: intake/dispatch/fill_apply modules, `engine.py` ≤ 400 lines, behavior unchanged, **F-45** the deferred-order expiry bound sourced from config with a non-zero default plus an expired-order count, **F-47** per-strategy order-failure counters. B fill bookkeeping: F-27 overfill clamp before effect + one quantity to all three records, F-28 wire `DurableFillDedup` when a store is present, F-29 one dedup policy shared with recovery. C risk: F-14 explicit backtest no-risk opt-out + cash floor always, F-15 attributed-book enforcement, F-16 fail-closed baseline load, F-17 `now=` threading, F-18 trading-hours + CLI cid + instrument limits | crash-window matrix (kill -9 at each durable write boundary); risk-bypass property test over every venue path |
| 3 Broker/market-data | W3 | A wall fix F-22 (override detection) + F-12b adapter wall requires engine permit + F-24 cancel priority. B WS gaps F-20 → forced reconcile on reconnect + F-23 tracker durability. C PIT strict mode F-21 + CA ex-date strictness | paper/live adapter parity contract tests |
| 4 Strategy integration | W4 | A F-41 bracket single-ownership + emit-time position marking fix. B CA applied in live + warmup/state persistence + `on_start` wiring. C shared indicator library (replace hand-rolled SMA/RSI in `sma_cross` / `mean_reversion`) | same-strategy backtest-vs-paper tape parity on one fixed tape |
| 5 Testing programme | W5 | A hash-chain fuzz + permanent crash-matrix harness. B live-reconnect money-path test + de-mock `test_kill_switch`. C concurrency race tests (submit vs fill vs monitor) | the new tests gate the release |
| 6 Cleanup | W6 | A deletion ledger: `poc/`, `apps/top_gainers`, `persistence` shim, `position_manager` alias, `SUBMITTED`, dead `_attach_event_store` (F-06), F-32 modify pickling. B god-file splits remainder: `safety.py`, `startup.py`. C `graphify-out` gitignore + docs audit | zero-import proofs re-run; suite green |

### Landed ahead of their waves, at `3a1797e`

F-39, F-40 and F-43 (W0.5), F-45 and F-47 (W2), F-44 (W1) and F-46 (W6) were
closed together rather than in their scheduled waves, because they are small
seams around the same composition root and splitting them across four waves
would have meant four sequential edits to `startup.py` under a gate chain that
exists precisely to avoid that. Each carries a control demonstrated to fail when
its fix is removed; the per-finding list is in the
[ledger](../../audits/2026-09/2026-09-29-runtime-findings-ledger.md).

Two behaviour changes came with them, both consequences rather than accidents:

- **Paper/live order quantities change.** They are now the risk-policy size
  rather than a fixed ~₹95,000 notional. That is the point of F-40, but it is
  a real change to what paper places — run paper first, deliberately.
- **A reconnect can now halt a feed that previously recovered.** The allowance
  defaults to 20 bars, chosen to sit above Dhan's structural 15-minute
  session-tail hole (its NSE 1m series stops at 15:14). Lowering it and seeing
  recovery halt on the known tail is the lever working, not a regression.

F-34 (W0.5) and the rest of W1/W2/W6 remain open. When F-03 deletes
`SUBMITTED`, re-check the chart's status map: it still carries `SUBMITTED` on
the `pending` tier.

| 7 Production readiness | W7 | A JSON event schema v3 (decoder registry path already exists). B short-exposure gate + trading-hours hardening. C runbook + alert rules (`deploy/prometheus-rules.yml`): stale `reconciliation_checked`, `UNKNOWN > 0`, kill-switch tripped, append failures, tracker residue | live dry-run checklist (paper during market hours) |

### Conflict control and rollback

- Lane ownership is a contract, not a convention. Cross-lane needs escalate to the
  coordinator.
- `BASE_SHA`/`HEAD_SHA` recorded per review. Rollback = revert the phase commit.
- Hidden complexity discovered mid-task upgrades the plan (stop, report, re-plan
  that node). Nothing downgrades.

### Verification (decision B, made concrete)

Each phase carries a behavior-smoke test (committed, not throwaway) that boots a
real `PaperBroker` session via `boot()`, pushes synthetic quotes through the bus,
uses a fake clock, and asserts that phase's wiring claims. Phase 2 adds kill -9 +
restart fold checks. Phase 5 generalizes these into the permanent crash-matrix
harness. This directly counters the review's false-confidence class: tests must
exercise production wiring, never a fixture that re-creates it.

## Testing discipline

- TDD Iron Law per lane: failing test first, watched fail, minimal code, watch pass,
  refactor. A test that passes immediately proves nothing and must be rewritten.
- Assert on real behavior; mocks only where unavoidable (and never for the money
  paths — de-mocking `test_kill_switch` is a Phase 5 deliverable).
- **Gate set** (run at every wave boundary, on a clean tree):
  1. `quality-gate.yml` — ruff ratchet + mypy + full pytest (`trading/tests`,
     `tests/`, per-package) + branch-coverage floors ≥ 85.
  2. `parity.yml` — the parity suite **and** the `contracts` suite.
  3. `frontend-gate.yml` — vitest + Playwright + typecheck; required whenever
     `interfaces/`, `frontend/`, or WS contracts change.

## Alternatives considered

- **Approach 2 (dependency-DAG waves)**: fastest wall-clock, but conflicts with the
  hard gate chain and produces mixed-phase review diffs; rejected for exactly the
  failure mode that left `contracts` red on a dirty tree (F-05).
- **Approach 3 (persistent swimlanes)**: deep continuity but serializes the critical
  path through the engine lane across Phases 0–2; rejected on pace.
- **Ratchet gates**: rejected earlier by the project (correctness gates must not
  ratchet) and inconsistent with decision A.

## Requirements and acceptance criteria

- **AC-1**: Every finding F-01…F-47 is closed by a failing-first regression test
  named in its phase's implementation plan, or explicitly deferred with a written
  rationale in the phase plan.
- **AC-2**: After every wave, the gate set is green on a clean tree.
- **AC-3**: Every phase's behavior smoke proves production wiring (real `boot()`),
  not fixture wiring.
- **AC-4**: The risk-bypass property test proves no venue path (submit, modify,
  cancel, adapter mutations) escapes the risk + kill-switch gates.
- **AC-5**: The crash-window matrix passes: kill -9 at every durable-write boundary
  leaves the recovered book equal to the fold of the log.
- **AC-6**: The Definition of Done from the review (10 items) holds; each item maps
  to at least one test.
- **AC-7**: Each phase is reviewed by a reviewer agent before its commit lands;
  Critical review findings are fixed before the next phase starts.
- **AC-8**: No worker edits outside its lane without coordinator re-laning.

## Scope boundaries

In: the fixes, the splits, the deletions, the tests, runbook/alerts.
Out: new trading features (GTT/AMO/T+1/pledge), new frameworks, frontend redesign,
performance optimization (the review found no material bottleneck).
