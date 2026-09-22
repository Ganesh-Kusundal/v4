# TradeX v4 Audit Remediation Design

**Date:** 2026-09-22
**Status:** Draft for review

## Goal

Restore a green validation baseline for the current worktree and remove the verified replay correctness defects found during the fresh audit, without resetting or overwriting unrelated user changes.

## Scope

### Phase 1: Validation baseline

1. Fix all seven current TypeScript errors in `frontend/src/main.ts` using explicit narrowing or null-safe values. Do not weaken compiler strictness.
2. Rebuild the frontend artifact after the source fixes so `frontend/dist/BUILD_STAMP.json` and served files match the current source.
3. Verify `/ui/` artifact integrity through the existing Python mount tests.
4. Correct the verified Python export/type issues:
   - dynamic compatibility exports must remain importable and accurately represented to tooling;
   - `Any` must be imported or the annotation replaced in `paper/adapter.py`.
5. Address only directly relevant lint failures. Style-only cleanup outside touched code is out of scope.

### Phase 2: Replay correctness

1. **Synthetic depth consistency:** generate the price path once per candle and use its final price for both emitted quote ticks and optional depth. `feed_bar()` must not call `_walk()` twice for one candle.
2. **Scoped flush:** associate replay with its `(instrument, interval)` aggregator and flush only that aggregator when replay completes. Other live subscriptions on the same WebSocket must remain forming.
3. **Explicit setup failure:** if the replay aggregator cannot be created, send a typed error acknowledgment and do not start the replay task or claim replay started.
4. **State reset:** on replay stop and before a new replay starts, clear transient `paused` and `step` state and restore a defined speed baseline. A stale step request must not affect a later replay.
5. **Regression coverage:** add focused tests for depth/quote midpoint consistency, scoped flushing, aggregator creation failure, and replay state reset. Preserve and extend existing replay WebSocket tests.

## Design

The existing per-WebSocket `bar_aggregators` map remains the ownership boundary. Replay setup records the exact aggregator key and passes that key into the replay task. Cleanup remains per connection. No new global state or cross-connection registry is introduced.

`SyntheticTickGenerator` will expose no new public API unless needed by tests. The simplest implementation is to compute `prices = self._walk(candle)` in `feed_bar()`, derive quotes from that path, and pass `prices[-1]` to `_emit_depth()`. `iter_ticks()` remains compatible for callers that only need quotes.

Replay lifecycle state will use explicit initialization and reset helpers or equivalent assignments. Stop must cancel and await the task, then clear all transient flags. Start must reset state before task creation and must validate the aggregator before spawning work.

Frontend fixes will preserve current replay behavior. Where an array lookup can be undefined, code will either guard the value or pass `null` to APIs that accept it. The speed-cycle lookup will use a safe fallback rather than a non-null assertion that could conceal an invalid state.

## Error handling

- Invalid replay setup produces an error message on the existing acknowledgment channel.
- Aggregator construction errors are logged with context and surfaced to the client.
- Replay completion flushes only the validated replay aggregator.
- Cancellation remains quiet for an internal stop, while an explicit client stop retains the existing stopped acknowledgment.
- No broad exception handler will be widened. Existing best-effort handlers remain unchanged unless directly required for the scoped fix.

## Tests and acceptance criteria

Phase 1 is complete when:

- `npm run typecheck` passes in `frontend/`.
- `npm run build` passes.
- `npm run verify-artifact` passes.
- `pytest -q` passes, including the served-artifact tests.
- Relevant changed Python files do not introduce new undefined-name or import errors.

Phase 2 is complete when:

- Existing replay WebSocket tests pass.
- New tests prove that depth uses the emitted final quote price.
- New tests prove unrelated aggregators are not flushed by replay completion.
- New tests prove invalid aggregator setup emits an error and no replay task.
- New tests prove stale pause/step state cannot leak between replay sessions.
- Full Python and frontend validation remains green.

## Non-goals

- Refactoring the frontend monolith.
- Redesigning the broker passthrough wall, uncertainty policy, or protocol typing.
- Broad lint cleanup unrelated to touched files.
- Changing replay price-path mathematics, spread policy, or public replay message names.
- Resetting, stashing, or rewriting existing user changes.

## Change isolation

Before implementation, record the existing worktree diff. During implementation, edit only files needed for this specification and new regression tests. Generated frontend artifacts may be regenerated as required by the existing build workflow. Final reporting will distinguish pre-existing user changes from changes made for this remediation.
