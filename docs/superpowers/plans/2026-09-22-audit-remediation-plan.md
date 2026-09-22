# Audit Remediation Implementation Plan

## Baseline and isolation

1. Record `git status --short` and the pre-existing diff hash/stat. Do not reset or stash current user changes.
2. Limit edits to the existing frontend replay files, replay implementation/tests, directly affected export/type files, and generated frontend artifacts produced by the build.

## Task 1: Frontend type safety

- Add a focused typecheck gate using `cd frontend && npm run typecheck`.
- Fix each current `RawBar | undefined` error with local narrowing or `?? null` at the call boundary.
- Guard the selected replay start bar before reading `.time`.
- Make replay speed cycling return a definite number through a safe fallback.
- Re-run typecheck before touching replay behavior.

## Task 2: Artifact and Python export baseline

- Add/import `Any` in `brokers/src/tradex_brokers/paper/adapter.py` or replace the annotation with an available type.
- Preserve dynamic compatibility behavior in `common/auth.py` and `common/capabilities.py`, while making exports valid for static tooling without eager broker imports.
- Add regression checks for importing compatibility names and resolving the paper adapter annotation.
- Run Ruff on touched files only and fix directly relevant errors.
- Run frontend build, artifact verification, and the existing UI mount tests. Treat generated `frontend/dist` changes as expected build output.

## Task 3: Synthetic depth consistency, TDD

- Add a failing unit test to `trading/tests/replay/` that captures emitted quotes and depth from one candle with depth enabled and asserts depth midpoint equals the final emitted quote LTP.
- Run the test and confirm it fails because `feed_bar()` calls `_walk()` twice.
- Refactor `SyntheticTickGenerator.feed_bar()` to compute one path and use it for quote generation and depth.
- Run the focused test and the existing synthetic tick test module.

## Task 4: Scoped replay flush, TDD

- Add a failing WebSocket replay test with two active bar subscriptions and instrument-specific aggregators. Assert replay completion flushes only the replay key.
- Implement replay key capture and flush only that aggregator.
- Run focused replay tests, then the full interface replay test module.

## Task 5: Replay setup and lifecycle state, TDD

- Add a failing test for aggregator construction failure: expect an error acknowledgment, no task, and no replay-started acknowledgment.
- Add a failing test that sets a pending step/pause state, stops replay, starts again, and verifies the new replay begins normally with the baseline speed and no inherited step.
- Implement explicit setup validation and a small reset helper or equivalent assignments. Reset task, paused, step, and speed consistently on stop and start.
- Preserve explicit stop acknowledgment behavior and internal silent-stop behavior.

## Task 6: Full validation and review

- Run focused Python tests after each task.
- Run `cd frontend && npm run typecheck && npm run build && npm run verify-artifact`.
- Run `pytest -q` from the repository root.
- Run `git diff --check` and Ruff on touched Python files.
- Inspect the final diff to ensure no unrelated user changes were overwritten.
- Request an independent code review before finalizing.

## Acceptance gates

- No production behavior change is made before its regression test fails first, except generated artifacts and purely mechanical type/import corrections.
- Full Python suite passes.
- Frontend typecheck, build, and artifact verification pass.
- Replay-specific regression tests pass.
- Existing user changes remain intact and are clearly separated in the final report.
