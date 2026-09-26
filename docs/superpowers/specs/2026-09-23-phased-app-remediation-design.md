# Phased App Review Remediation Design

**Date:** 2026-09-23  
**Branch:** `chore/cleanup-overhaul`  
**Status:** Approved for planning  
**Scope source:** 2026-09-23 frontend/backend review

## 1. Context and disposition

The working tree contains a nine-file pivot away from the original chart-module entrypoint. The pivot is real: the current `main.ts` wires `replay`, `screener`, `workspace`, `backtest`, account, runs, trades, and Tier-2 modules directly. The committed review assumptions about the old 527-line entrypoint no longer describe the current application.

The pivot does not yet form a release-safe live-trading application. Confirmed issues include API-key disclosure, unguarded context-menu orders, replay state leaking into live aggregation, replay/chart series contamination, incomplete first live bars, thread-affinity violations, and missing browser coverage. The existing `2026-09-22-audit-remediation-design.md` covers validation and replay internals only; it does not cover the broader remediation program.

This design preserves the current uncommitted worktree. It does not reset, stash, or rewrite unrelated user changes.

## 2. Goals and decisions

### Goals

1. Make accidental live orders impossible from unsafe chart interactions.
2. Make replay, live aggregation, and chart series state explicit and recoverable.
3. Prevent stale or incomplete live bars after reconnects and subscription starts.
4. Preserve workspace, screener, and startup behavior under slow/failure conditions.
5. Restore the account workflows needed to manage orders and positions after guarded entry.
6. Preserve non-NSE instrument selection in the chart host.
7. Remove verified dead code and restore the repository quality gates.
8. Keep each phase independently testable and revertible.

### Decisions approved in design review

- Use **safety-first vertical slices**, not a protocol rewrite or a set of untracked one-line patches.
- Use an **explicit INTRADAY-only** order policy. Product selector support (CNC/NRML) is not part of this program.
- Defer production browser authentication to a follow-up design.
- Until that follow-up exists, live sessions must **fail closed on non-loopback bind addresses**.
- Treat the current API-key/page-injection model as development-only.
- Do not commit or rewrite the existing user diff as part of planning.

## 3. Non-goals

- Implementing HttpOnly session authentication, CSRF, or authenticated WebSocket upgrades.
- Restoring CNC/NRML product support.
- Rewriting the broker, execution, or feed registry architecture.
- Refactoring unrelated analytics, runtime-root, or cleanup-overhaul phases.
- Deleting orphaned modules before an import audit.
- Changing replay price-path mathematics, spread policy, or strategy behavior.
- Automatically resolving workspace conflicts by overwriting another tab's state.
- Making the chart read-only; explicit, guarded INTRADAY order entry remains in scope.

## 4. Phase architecture

### Phase 0 — Baseline and contract inventory

Record the current diff and establish a behavior matrix for:

- paper, backtest, replay, and live session modes;
- HTTP, WebSocket, chart, and workspace authentication boundaries;
- order lifecycle and replay-mode state;
- live bar seeding, forming/closed transitions, reconnect, and replay restore;
- current test, build, Ruff, and E2E commands.

No production behavior changes occur in this phase. Every later phase uses the recorded working-tree state and adds focused regression tests before implementation.

**Exit gate:** baseline commands and failures are recorded; the current nine modified files are explicitly identified as user-owned work.

### Phase 1 — Trading safety and interim deployment boundary

#### Order entry

Restore a visible, explicit order state in the chart shell:

- `INTRADAY` is displayed and included in the visible order context.
- Quantity is user-controlled and validated as a positive integer.
- Trading starts disarmed; arming is explicit and visibly time-bound by the UI state.
- Submission requires confirmation with instrument, side, type, quantity, and price/trigger.
- Only the price pane (`paneIndex === 0`) may produce an order. Indicator/time-scale clicks are ignored or rejected.
- Stop orders validate side/price relationship against the current quote before submission.
- Replay, stale, reconnecting, and unknown-mode states disable order submission.
- The context-menu hook must not silently use a clicked indicator value as a price.

The frontend is a usability guard, not the security boundary. The server must reject order mutations while a replay run is active or when the session mode is not order-capable. Because WebSocket replay state and HTTP order requests are separate connections, the server-side replay guard must live at the session/app boundary rather than only in `replay.ts`. A single-user safety-first guard may reject all order mutations while any replay run is active; later multi-client work can scope leases to an owner.

#### Account actions

Restore the read-only account panel's management path so a guarded order can be
managed without falling back to raw API calls:

- working orders expose cancel and modify actions;
- open positions expose an explicit exit action;
- every mutation sends its own idempotency key and the current INTRADAY/mode
  context;
- failed actions leave the row visible with a typed error and never optimistically
  remove it;
- replay, stale, and disconnected states disable mutation buttons.

This remains INTRADAY-only; it does not introduce product variants or a new
broker order API.

#### Interim live bind policy

At the server/session startup boundary:

- paper, backtest, replay, and development binds retain current behavior;
- live mode may bind to loopback interfaces for trusted local use;
- live mode bound to a non-loopback interface fails startup with an actionable message;
- there is no silent bypass in this phase.

The current API-key injection route remains available only inside that trusted-loopback boundary. Public deployment remains unsupported until the follow-up authentication design lands.

**Exit gate:** unit tests cover arm/quantity/price-pane/stop-side/replay checks and account-action idempotency/error handling; backend tests cover replay-active order rejection and live non-loopback rejection; E2E proves unsafe context-menu targets cannot submit and that cancel/modify/exit actions work.

### Phase 2 — Replay and feed correctness

#### Server-owned replay run

Replace loose replay fields with a per-connection run object:

- `run_id`;
- normalized instrument/timeframe;
- immutable replay dataset and target-bar cursor;
- speed, paused state, step request, and terminal status;
- the exact `(instrument, interval, run_id)` aggregator ownership.

A new run never reuses a partial aggregator. A seek or restart creates a fresh run/aggregator and preserves the user's paused/speed intent deliberately.

#### Cursor and frame contract

`replay_started` reports the number of target-interval bars. `replay_seek` accepts a target-bar index rather than a client-calculated `start_time + 60 * index`. The server maps that index to its own actual candle list, so gaps, weekends, and session boundaries do not make the scrubber lie.

A closed frame completes a step only when its instrument, interval, run ID, and `source="sim"` match the active run. Frames retain `run_id`, `source`, and `closed` through the WebSocket and `BarSocket` mapping.

#### Terminal cleanup

All terminal paths—explicit stop, normal completion, setup failure, background-task exception, and socket teardown—use one idempotent cleanup path:

1. cancel and drain the replay task;
2. flush/dispose only the run's aggregator;
3. clear live-feed suppression and session replay ownership;
4. emit one terminal frame (`done`, `error`, or `stopped`) when the connection remains writable;
5. leave unrelated aggregators untouched.

Setup errors are acknowledged before any suppression is activated. Invalid numeric, non-finite, negative, or excessively large replay parameters produce protocol errors rather than closing the socket or leaving a dead task.

#### Chart replay boundary

Extend the chart data owner with an explicit replay lifecycle API:

- entering replay snapshots the live series and creates a separate replay series/prefix;
- replay frames update only the replay series;
- seek replaces the replay prefix at the requested target bar;
- exit restores the live snapshot and restarts the live stream;
- live frames received during replay are buffered or ignored according to the run, then reconciled on restore.

The `DataController` must not drop replay frames merely because they are older than the current live tail. The chart must display the replay window deliberately, not by accident through merge behavior.

#### Live bar seeding

Forward `DataController`'s `seedFrom` option and an available current-bucket snapshot through `V4DataFeed` and `BarSocket` in the `subscribe_bars` message. The backend seeds a newly created aggregator from that snapshot before applying live quotes. A last closed bar without current-bucket data is not enough to reconstruct mid-bucket OHLCV; when the snapshot is unavailable, the stream remains provisional and performs an immediate history resync instead of publishing a falsely finalized bar.

All reactive-bus callbacks, including bar callbacks, cross into the WebSocket event loop with `call_soon_threadsafe` before touching the outbound queue or connection state.

**Exit gate:** backend unit tests cover run cleanup, fresh aggregators, target-bar seeking, scoped stepping, setup errors, live recovery, and seeded first bars; browser E2E covers replay start, seek, step, pause, resume, completion, failure, live recovery, and chart restore.

### Phase 3 — Feed, UI, and workspace reliability

#### Feed lifecycle

- Propagate caller abort signals into history requests.
- Replace the global datalake anchor with a per-instrument/interval anchor.
- Bound startup probes and fall back to a default instrument without blocking the entire UI.
- Enable bounded polling or explicit resync after WebSocket gaps/reconnects.
- Keep feed status visible and disable order entry while data is stale.
- Restore an explicit exchange selector (including NFO, BSE, BFO, and MCX) and
  carry the selected exchange through history, workspace, chart, and order
  requests; do not derive it from a datalake search result that hardcodes NSE.
- Update volume on both history and live bar events.

#### Workspace and panels

- Do not mark a workspace ready or persist `series: null` before the first non-null bar arrives.
- On a revision conflict, surface the conflict and require an explicit user choice; do not automatically clobber a newer tab.
- Make screener loading, empty, and error states mutually exclusive and recoverable.
- Add a bounded startup timeout/fallback so a slow strategy or workspace request does not leave a blank application indefinitely.
- Ensure slow history and replay errors do not create stale “ready” status.

**Exit gate:** frontend E2E covers delayed startup, feed disconnect/resync, live volume, non-NSE selection, screener empty/error recovery, and slow workspace load/save; Python tests cover any changed workspace or route contract.

### Phase 4 — Cleanup and quality

After all behavior is stable:

- audit imports of `chart-lifecycle.ts`, `chart-state.ts`, and `chart-types.ts` again;
- delete only modules proven dead, or reattach the required interval mapping to the production feed;
- remove duplicate interval mapping rather than retaining two sources of truth;
- fix only introduced/undefined-name and touched-file lint failures;
- update the audit remediation and deployment documentation with the interim loopback policy and the deferred authentication follow-up.

No generated `dist` artifact or unrelated user change is committed unless explicitly requested.

## 5. Data and control flow

### Normal order flow

1. Chart context menu creates an order request for the price pane.
2. Frontend validates arm, quantity, mode, pane, price, and stop side.
3. Confirmation dialog shows the exact INTRADAY intent.
4. Backend verifies session mode, replay guard, key policy, and order invariants.
5. Execution engine submits through the existing idempotent order path.
6. WS order/fill frames update the account panel; errors become visible state.

### Replay flow

1. Frontend requests a target instrument/timeframe and optional target-bar index.
2. Server validates and loads the immutable replay dataset.
3. Server creates a fresh run and sends `replay_started` with target-bar count.
4. Chart snapshots live data and enters replay mode.
5. Server sends scoped simulated bar frames; chart updates the replay series only.
6. Pause/resume/step/seek affect only the run cursor.
7. Terminal cleanup restores live ownership and the chart snapshot.
8. Backend order guard remains active for the entire run and clears only after terminal cleanup.

### Feed recovery flow

1. WebSocket close/error marks the feed stale and disables trading.
2. The client retains a bounded cached history while requesting a fresh window.
3. Abortable/per-instrument history requests replace the live series.
4. A new subscription is armed with the last closed bar as seed.
5. The feed becomes ready only after history and stream reconciliation.

## 6. Error handling and observability

Every failure must have a visible owner:

- server configuration errors: startup exception with bind address and remediation text;
- order validation errors: no network call for client-side failures; typed server rejection for backend failures;
- replay setup/task errors: typed WS error and guaranteed cleanup;
- feed disconnects: stale status, bounded retry/resync, no stale live-trading state;
- workspace conflicts: explicit conflict state, no silent overwrite;
- panel data errors: visible error and a retry path;
- malformed parameters: protocol-level error, no uncaught task exception or socket termination.

## 7. Testing and acceptance criteria

Each phase must run the narrow regression suite first, then the full repository gate. The phase plan must list the exact targeted test files and touched Python files; the shared full-suite commands are:

```text
.venv/bin/python -m pytest domain/tests brokers/tests trading/tests tests -p no:cacheprovider --import-mode=importlib -c pyproject.toml
cd frontend && npm run typecheck
cd frontend && npm run build
cd frontend && npm run e2e
```

The release gate requires:

- no new trading without explicit arm/confirmation and server-side replay/mode checks;
- no live non-loopback bind under the interim policy;
- no replay terminal path that suppresses live bars or leaves a partial aggregator;
- correct first live bars after history load and reconnect;
- replay chart state restored after stop/done/failure;
- no silently overwriting workspace revisions;
- all existing tests plus new phase tests pass;
- introduced-file Ruff errors are resolved or explicitly documented as baseline.

## 8. Implementation order and rollback

1. Baseline/contract tests.
2. Trading safety and live-bind policy.
3. Replay run protocol and terminal cleanup.
4. Chart replay boundary and live seeding.
5. Feed/UI/workspace reliability.
6. Orphan cleanup and documentation.

Each item is a separate change set with a green targeted gate. If a phase fails, stop at that phase and revert only its own changes; do not reset the user's existing working tree.

## 9. Follow-up specifications

After this program:

1. Production browser authentication: HttpOnly session, CSRF, WebSocket upgrade authentication, revocation, and threat model.
2. Multi-client replay ownership and leasing, if concurrent chart sessions are supported.
3. Product support beyond INTRADAY, if required by broker/account workflows.
4. Multi-client ownership and conflict resolution for account mutations.
