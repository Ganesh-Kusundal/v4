# TradeX solid-platform mega remediation — design

**Date:** 2026-09-24  
**Status:** In Progress — gaps closing (dual FeedSupervisor fixed; runtime sdk allowlist + typing debt remain)  
**Approach:** C (fail-closed money + package DAG + bracket/recon parity)  
**Source review:** architecture review canvas + `docs/target-architecture-design.md`

## Summary

Close the gap between “shared ExecutionEngine exists” and “a system that may hold real capital.” Work ships as **three sequential waves**. Within each wave, independent workstreams run in parallel via subagents. No commit unless the human asks.

## Goals

1. **Fail closed** for live admission (risk, feed, durability, recon).
2. **Acyclic package graph** so interfaces/runtime do not depend on `tradex_trading` composition.
3. **Honest parity**: backtest protective exits and recon goldens match the claims we make in docs/CI.

## Non-goals

- Rewriting brokers or the chart library.
- New research/feature engines.
- Automatic broker heal of drift (halt + operator clear only).
- Full instrument-identity redesign (only document + harden contested-symbol path if a wave needs it).

## Wave A — Fail-closed money (P0)

### Expected behavior contract

| Concern | Inputs | Expected | Actual today | Change |
|---------|--------|----------|--------------|--------|
| Risk | live submit | Reject if unbound | `RiskManager=None` admits | Live mode **requires** risk; unbound → HALTED / reject |
| Feed | live submit | Reject if not READY | Optional supervisor | Live requires `FeedReady`; not ready → `OrderRejected` |
| Events | any append | Durable or halt | Append failure swallowed | Live: append failure trips kill switch (or rejects with UNKNOWN) |
| Reactive drop | bus OrderRequest | Always emit outcome | Kill/feed filter returns None | Emit `OrderRejected` with stable reason |
| Recon | live boot | HIGH/CRITICAL → no READY | Mostly present; broker fetch fail = continue | Broker book/position **both unavailable** → refuse READY (fail closed) |
| Paper | paper mode | May keep softer gates | Soft | Paper may omit feed READY; still requires risk if `live_enabled` paths shared |

### Components

- `tradex_execution/engine.py` — fail-closed pipeline + explicit rejects
- `tradex_runtime/startup.py` — live boot refuses READY when safety seams missing
- Tests under `trading/tests/execution` + `trading/tests/runtime` (or package-local when moved)

### Parallel workstreams (Wave A)

- **A1** Engine: unbound risk / missing feed / append failure / reactive OrderRejected  
- **A2** Boot: live Ready gate + broker-unavailable recon fails closed  
- **A3** Tests + metrics reason codes for new rejects  

A1 and A2 can start in parallel once contracts are locked; A3 follows.

## Wave B — Package DAG (P1)

### Expected behavior contract

- `tradex_interfaces` depends on ports / `tradex_application` / extracted packages — **not** `tradex_trading`.
- `tradex_runtime` depends on `tradex_config` (new thin package) or in-package schema — **not** `tradex_trading.config`.
- `tradex_trading` remains composition + shims + sdk bridge only.
- Import boundary tests encode the new `_ALLOWED` graph.
- Chart strategy loader imports `tradex_strategy…`, not shim path.

### Components

- New `config/` workspace member **or** move `AppConfig` into `tradex_runtime` (prefer thin `tradex_config` if sdk also needs it).
- Move or re-export `sdk/session.py` / `live_fill_bridge.py` carefully (prefer keep sdk under trading as composition façade; interfaces call application ports).
- Update `tests/test_import_boundaries.py`, package `pyproject.toml` deps, CI cov packages.

### Parallel workstreams (Wave B)

- **B1** Extract `tradex_config`; rewire runtime + interfaces imports  
- **B2** Break interfaces→trading imports (application ports / typed DTOs)  
- **B3** CI: mypy+cov for execution/analytics/strategy/interfaces (no continue-on-error for new packages)  
- **B4** Retarget strategy loader + golden imports to package authority  

B1 before B2 if AppConfig is the shared seam; B3/B4 parallel after B1.

## Wave C — Bracket + recon parity (P1/P2)

### Expected behavior contract

- **Brackets:** Choose one and enforce in code + docs:
  - **Option C1 (recommended):** Backtest simulates stop/target exits with the same geometry checks as `protective_request`, OR
  - **Option C2:** Backtest strips protective claims from metrics/UI (no SL/TP zones as “fills”).
- **Recon golden:** Recorded broker book + local cache → expected drift list + kill-switch trip; in CI `parity.yml`.
- Crash-restart: event store replay restores cache; test proves Ready only after recover+recon.

### Parallel workstreams (Wave C)

- **C1** Bracket decision implementation (C1 or C2)  
- **C2** Recon golden fixture + CI  
- **C3** Crash-restart recovery test  

After Wave A, C2/C3 may start before Wave B finishes if they only touch execution/runtime tests.

## Global constraints

- No commit unless human asks.
- Prefer fail closed over silent continue.
- Patch/test **package authority** (`tradex_execution`, …), not shims.
- Keep `apply_fill` / single OMS spine — no second position model.
- YAGNI: no new frameworks; extend existing KillSwitch / RiskManager / ReconciliationEngine.
- Integration-style tests preferred; paper/simulated fills OK; no fake venue for recon golden (use recorded snapshots).

## Success criteria

- Live boot without risk or event store → not READY.
- Unbound risk or not-READY feed → `OrderRejected` (never silent None on money path).
- Event append failure in live → kill switch or UNKNOWN halt (documented).
- Broker recon unavailable in live → refuse READY.
- Import boundary: interfaces do not import `tradex_trading`. Runtime may import
  only `tradex_trading.sdk.session` and `tradex_trading.sdk.live_fill_bridge`
  until `TradingSession` is extracted (AST allowlist).
- Parity gate includes recon golden; bracket contract documented and tested.
- quality-gate covers extracted packages (coverage required; mypy soft until typing debt cleared).

## Risks

- Fail-closed may break existing “boot returns READY without network” tests — update those tests to assert HALTED/NEW when seams missing; add explicit paper-mode exemptions.
- Config extraction touches many imports — do B1 as a mechanical move with boundary tests first.
- Bracket simulation changes research numbers — call out in release notes; regenerate affected goldens only if metrics claim protective exits.

## Out of scope follow-ups

- Canonical instrument ID redesign.
- Auth/session production hardening (separate workstream already documented).
- Graphify refresh after moves.
