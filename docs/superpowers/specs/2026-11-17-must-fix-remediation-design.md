# Must-fix audit remediation — design

Date: 2026-11-17
Branch: `integration/wave1` (baseline pushed as `8cbb99c1`)

## Context

A whole-repo audit on 2026-11-17 (five parallel agents plus targeted checks)
found three money-safety defects in the execution engine, an HTTP auth/CSRF
test regression, and a CLI session-mixup bug. The full suite currently reports
**31 failed, 6359 passed, 17 skipped, 36 xfailed, 22 xpassed**.

This design covers only the Must-fix findings. The Should-fix and Nice-to-have
findings (SQL table-allowlist bypass, unlocked `MetricsRegistry` creation,
non-atomic universe/delisted state writes, teardown leaks, Upstox token/expiry
bugs, and lean cleanups) are explicitly out of scope and are left for a later
spec.

The just-pushed commit `8cbb99c1` removed `require_auth` from the data routes
while a tracked regression suite asserts those reads must be gated. This
design resolves that conflict in favour of restoring the guard.

## Goals

1. Make the durable event log agree with the live book after refused fills,
   overfills, and cancelled orders.
2. Restore the full suite to zero failures.
3. Ensure `--broker PAPER` can never operate a live session.
4. Add one focused regression test per finding so each defect fails loudly if
   it returns.

## Non-goals

- No changes to Should-fix/Nice-to-have findings.
- No broad refactors. Each change is the smallest that fixes its finding.
- No change to the `require_auth` dependency itself; it is reused as-is.

## Decisions

- **Auth direction.** Restore `require_auth` on the data routes. The
  dependency is open when no `--api-key` is configured, so the local no-auth
  developer posture is preserved while the credential leak is closed. The
  `docs/api/no-auth-api-setup.md` flow still works with no key configured.
- **Sequencing.** One finding per commit, in the order below. Each commit
  carries its regression test and is independently revertible.
- **Test style.** Red first where practical: add the failing test, confirm it
  fails, then fix. Never delete a failing auth test to make the suite green.

## Design

### F1 — Sync fill door writes the event before proving cash

**Where:** `execution/src/tradex_execution/engine.py:877,983,998,1036-1045`;
`execution/src/tradex_execution/cash_ledger.py:203-208`;
`execution/src/tradex_execution/risk.py:548-550`.

**Cause:** `_apply_fill_to_book()` appends `OrderFilled` at `:983` before
`_apply_fill_effect()` calls the cash ledger at `:998`. The lock around the
proof and debit does not cover the earlier append. With cash 1000 and a BUY of
10 @ 100 plus fee 0.42, `submit()` raises "insufficient buying power", the live
book has zero positions, but the log holds a durable fill. Recovery replays it
into a phantom position and negative cash.

**Fix:** in `_apply_fill_to_book`, compute the fee, then call
`_require_cash_for_fill(fill, charged_fee)` before `_append(OrderFilled)` — the
same order the inbound door uses at `engine.py:1237`. Make the risk cash gate
include fees so approval and ledger proof use one rule. Keep the compensating
`try` that releases the idempotency reservation around the whole block.

**Regression test:** refused BUY leaves no `OrderFilled` in the store; after
`recovery.rebuild()`, cash and positions are unchanged.

### F2 — Overfills clamped in the OMS but applied in full

**Where:** `execution/src/tradex_execution/order_manager.py:38-48`;
`engine.py:877,983,998,1095`;
`execution/src/tradex_execution/fill_sources/paper_book_fill_source.py:291-294`.

**Cause:** the OMS clamps `delta = min(fill.qty, remaining)` for its own
projection, but the engine computes fees, appends, and books the original
`fill`. A qty-10 order filled with qty-15 ends with OMS `filled_quantity=10`,
position +15, and cash −1500.

**Fix:** clamp the `Fill` once at the engine boundary in both doors, before fee
computation, append, and booking. Use `min(fill.qty, order.remaining_quantity)`;
skip the fill entirely when the clamped quantity is ≤ 0 (duplicate full fill).
`on_order_filled` then receives the already-clamped fill. Remove the
source-level workaround only if the engine clamp makes it redundant — otherwise
leave it.

**Regression test:** qty-10 order, qty-15 fill → position +10, cash −1000, OMS
FILLED; a second qty-15 fill is ignored.

### F3 — Cancel event-store failure re-enters the kill switch

**Where:** `execution/src/tradex_execution/engine.py:1530,1565,1668`;
`execution/src/tradex_execution/kill_switch.py:157-164`.

**Cause:** `_append()` trips the kill switch on write failure. `cancel()` ignores
the append's boolean return, and the still-stale cache makes `KillSwitch.trip()`
cancel the order already mid-cancel. One cancel can dispatch two venue cancels
and return success for a request absent from the log.

**Fix:** add an in-flight guard around `cancel()` and `modify()` so
`KillSwitch.trip()` skips the order being mutated. Check the `_append()` return
in cancel/modify and withhold the success response on failure, as `submit()`
already does at `engine.py:845-856`.

**Regression test:** with a failing event store, one cancel produces exactly one
venue cancel dispatch and does not return success.

### F4 — Auth/CSRF regression

**Where:** data route modules in `interfaces/src/tradex_interfaces/routes/`
(`account.py`, `portfolio.py`, `chart.py`, `market_data.py`, `orders.py`,
`extensions.py`); `tests/regression/test_http_auth_data_routes.py`;
`trading/tests/interface/test_csrf_enforcement.py`;
`trading/tests/interface/test_fastapi_app.py`;
`trading/tests/interface/test_cors_csrf_origin_parity.py`; `README.md`.

**Cause:** commit `8cbb99c1` removed `APIRouter(dependencies=[Depends(require_auth)])`
from data routes, breaking 12 data-read auth tests, 8 CSRF tests, and 5
fastapi-app auth tests.

**Fix:** restore the `APIRouter(dependencies=[Depends(require_auth)])` form that
commit `8cbb99c1` removed, importing `require_auth` unchanged from
`interfaces/src/tradex_interfaces/auth/deps.py`. Re-run the failing CSRF
and option-chain tests; fix any remaining genuine cause (do not weaken the
assertions). Correct the README line claiming all GET endpoints are public so
it matches the implemented, key-conditional behaviour.

**Regression test:** the existing `test_http_auth_data_routes.py` suite is the
regression test — it must pass unchanged. Add no duplicate coverage.

### F5 — `serve --broker PAPER` can reuse a live session

**Where:** `interfaces/src/tradex_interfaces/cli.py:304-334,465-476`.

**Cause:** `main()` boots a session from the environment unconditionally.
`serve` reuses that session for `--broker PAPER` without checking its mode; only
non-PAPER flags rebuild through `serve_config()`. With live env vars set,
`tradex serve --broker PAPER` operates a live session. The same unconditional
boot makes read-only CLI commands connect to the venue and contend on the
single-writer lock.

**Fix:** when `serve` reuses an env-booted session, compare its `broker_id` and
`mode` to the CLI flag; on mismatch, rebuild via `serve_config()` rather than
reusing. Boot lazily for commands that do not consume the bound runtime
(`health`, `positions`, `account`, `orders`, `watch`, `scanner`).

**Regression test:** live env + `--broker PAPER` yields a paper session;
a read-only command does not connect to the venue.

## Testing strategy

- Add or revive one regression test per finding, in the package that owns the
  code. The auth suite already exists and is reused unchanged.
- F1–F3 tests reproduce the defect first, then pass after the fix.
- No test is deleted, skipped, or xfailed to reach green. The xfailed
  `test_inbound_fill_cash_atomicity.py` is out of scope (it belongs to the
  inbound adopt-and-flag design, not these findings).

## Verification

1. `uv run --no-sync python -m pytest -c pyproject.toml -q -p no:cacheprovider`
   → expect 0 failed.
2. `bash scripts/predeploy_gate.sh` → full green gate (lint/type/import
   ratchets, parity, Docker build, paper boot).
3. Confirm `git status` is clean before each commit and that no runtime
   artifacts (`data/.sync.lock`, `data/.universe_metadata.json`, `*.backup`)
   are staged.

## Risks

- F1 changes the order of cash proof and event append on the hot order path.
  Risk is limited by keeping the existing lock and compensating release path;
  the parity suite covers backtest/replay/paper/live equivalence.
- F2's clamp changes how an overshooting venue fill is booked. This is the
  intended correction; the paper fill source's existing workaround is
  re-evaluated rather than blindly removed.
- Restoring `require_auth` changes HTTP behaviour only when `--api-key` is
  set. The no-key local flow is untouched.

## Rollout

Sequential commits on `integration/wave1`, each pushed after the relevant tests
pass. No separate release branch; this branch is already the integration line.
