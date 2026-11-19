# Split `ws_stream` and move backtest policy out of `routes/chart.py` (plan only)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Plan written 2026-11-19 from a structural read only (function inventory by grep); nothing here has been run. Step 1 of each task re-derives the claims.

**Goal:** Make the WS state machine and the backtest construction testable without a socket or HTTP, with no change to wire frames or status codes.

**Architecture:** (A) `ws_stream` (`routes/stream.py:84-1312`, ~40 nested functions sharing closed-over cells `dropped`, `control_overflow`, `disposables`, `status_tasks`) becomes a small `WsConnection` plus per-concern collaborators with explicit state objects. (B) `_build_strategy`, `_backtest_candles`, `_backtest_engine` (`routes/chart.py` ~163-268) move into an application use case that raises domain errors, with the route mapping them to HTTP.

**Spec:** none. Decisions owed: none beyond the scope choice already made (plan only).

## Global Constraints
- Wire frames, close codes (1011 no session, 1013 overflow) and HTTP statuses unchanged; pinned by `trading/tests/interface/test_ws_*.py` and chart tests.
- Work in place (editable venv, CLAUDE.md lesson 1). One commit per task. Baseline before each task: the interface suite's known failures (10 listed at `$TMPDIR/baseline_fail.txt` plus `test_cors_csrf_origin_parity::test_app_state_origin_without_env_var_passes_csrf_origin_check`, all auth/CSRF, none from order or stream code).
- Gateway must not import `tradex_interfaces`; application must not import FastAPI.

## Review Focus
- Closed-over cell semantics: `dropped[0]` and `control_overflow[0]` are written by producer callbacks from other threads (`_on_bar_quote_threadsafe`); the state object must keep the same thread-safety (no new lock-free races).
- Teardown order: `disposables` and `status_tasks` cleanup on disconnect, replay stop, and writer cancel must be unchanged.
- `_replay_error` is defined twice (`:999`, `:1008`); the later one wins today. Keep that behaviour, then delete the dead one in its own commit.
- Backtest: `HTTPException` 422 ("unknown params"), 500 ("strategy unavailable") must be preserved as the same statuses and messages.

### Task 1: Pin current behaviour (no code moves)
- Files: Test `interfaces/tests/test_ws_stream_characterization.py`.
- [ ] Step 1: One test per frame family driven through the existing `TestClient.websocket_connect`: quote, depth, order, bar, replay start/pause/stop, overflow close. Assert exact frames. Run; all PASS now (characterization).
- [ ] Step 2: Commit `test(stream): characterize ws_stream frames before split`.

### Task 2: Extract state and writer
- Files: Create `routes/_ws/state.py` (`ConnectionState`: `dropped`, `control_overflow`, `disposables`, `status_tasks`, `track_drop`), `routes/_ws/writer.py` (`_writer`, `_get_tagged`, `_close_on_control_overflow`). Modify `stream.py`.
- Interfaces: `ConnectionState` replaces the four closed-over cells; `Writer(ws, state, queues)` exposes `.run()`.
- [ ] Step 1: Failing unit test `test_connection_state_drop_counter` (thread-incremented counter equals N) and `test_writer_closes_1013_on_control_overflow` using a fake `ws` object (no socket).
- [ ] Step 2: Implement; Task 1 suite must stay green.
- [ ] Step 3: Commit.

### Task 3: Extract feeds (orders, bars, replay) one per commit
- Files: Create `_ws/orders_feed.py` (`_subscribe_orders`, `_unsubscribe_orders`, `_on_order`), `_ws/bars_feed.py` (`_normalize_ws_interval`, `_make_aggregator`, `_seed_*`, `_parse_bar_seed`, `_on_bar_quote*`, `_subscribe_bars`, `_unsubscribe_bars`, `_send_bar_frame`), `_ws/replay.py` (`_replay_*`, `_load_replay_candles`). `_handle_message` stays in `stream.py` as the dispatcher.
- Each collaborator takes `(state, send, session)`; no collaborator imports another.
- [ ] Per feed: failing unit test of its pure part first (`_normalize_ws_interval`, `_parse_bar_seed`, `_load_replay_candles` against a fake session), extract, run Task 1 suite, commit. Replay last (largest, owns `_replay_error` duplicate).

### Task 4: Backtest use case
- Files: Create `application/src/tradex_application/backtest.py` (`build_strategy(name, instrument, params)`, `load_candles(...)`, `build_engine(request)`; raises `ValueError`/`LookupError`/`RuntimeError` subtypes defined in the module). Modify `routes/chart.py:163-268` to call it and map errors to 422/500. Test `application/tests/test_backtest_policy.py`.
- [ ] Step 1: Failing tests: unknown strategy param -> `ValueError` naming the param; engine built with `FeeCalculator`, `RiskManager.fail_closed_cash is True`, `PercentageSlippageModel` default bps.
- [ ] Step 2: Move; keep `interfaces/tests/test_chart_backtest_off_loop.py` green. Confirm `tradex_application` may import `tradex_execution` in `tests/test_import_boundaries.py` before moving, or place the use case in `runtime` if not.
- [ ] Step 3: Commit.

## Not planned
- Splitting `create_chart_router`'s other 16 endpoints: no duplication shown; do after Task 4 if still worth it.
