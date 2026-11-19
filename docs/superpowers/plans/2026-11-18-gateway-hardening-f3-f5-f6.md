# Gateway Hardening (F3, F5, F6, L-list) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining verified findings from the gateway review: request deadlines (F6), bounded `/v1/quotes` (F5), WS connection cap (F3), and four cheap L-list fixes.

**Architecture:** Every limit is a `GatewayConfig` field read from a `GATEWAY_*` env var (same pattern as `max_subscriptions`), enforced at the one existing choke point (`_run`, `MarketService.quotes`, the WS handler). Failures use existing `GatewayError` subclasses so the uniform error envelope is unchanged.

**Tech Stack:** Python 3.13, FastAPI, pytest, `.venv/bin/python -m pytest gateway/tests -q` (baseline: 112 passed).

**Spec:** Verified findings table in this session (review of `tradex_gateway`, 2026-11-18); no separate design doc.

## Global Constraints

- Work in `gateway/` only. Imports stay inside the gateway boundary (no `tradex_interfaces`/`execute`/`strategy`).
- No new dependency. New limits are env-configurable with a safe default; `GatewayConfig.__post_init__`-style validation (`must be > 0`) like `GATEWAY_MAX_SUBSCRIPTIONS` (`config.py:98-99`).
- Wire messages stay canonical (no raw broker/exception text), per F4.
- `gateway/src/tradex_gateway/market.py` carries **uncommitted user edits** (`serialize_chain`, `ChainService._enrich`). Task 2 touches only `MarketService.quotes`/`_quotes_individually`; stage with `git add -p` so the user's hunks are never committed by this plan.
- Each task ends green on the full gateway suite and is its own commit.

## Review Focus

- Slow broker call → client gets a 504-style envelope within `GATEWAY_REQUEST_TIMEOUT`, not a hang (Task 1).
- `/v1/quotes` with 1 bad id out of N → good ids still returned, bad one in `errors` (Task 2).
- `/v1/quotes` on a no-`quote_batch` broker with 500 ids → at most the cap is fetched, the rest reported, not 500 calls (Task 2).
- WS: the (cap+1)th connection is refused before `accept`, and a closed socket frees its slot (Task 3).
- WS `unsubscribe` with `instruments: []` leaves existing subscriptions intact (Task 4).

---

### Task 1: Request deadline (F6)

**Files:**
- Modify: `gateway/src/tradex_gateway/config.py` (add `request_timeout`)
- Modify: `gateway/src/tradex_gateway/routes/deps.py:13-15` (`_run`)
- Modify: `gateway/src/tradex_gateway/history.py:171-173` (batch wait)
- Test: `gateway/tests/test_api.py`, `gateway/tests/test_config.py`

**Interfaces:**
- Produces: `GatewayConfig.request_timeout: float = 30.0` (env `GATEWAY_REQUEST_TIMEOUT`, must be > 0). `_run` raises `BrokerUnavailable` (existing, `errors.py:79`; reuse its constructor signature, message `"request timed out"`) when the deadline passes.

- [ ] **Step 1: Failing tests** — `test_config.py::test_request_timeout_default_and_validation` (default 30.0; `GATEWAY_REQUEST_TIMEOUT=0` raises `ValueError`); `test_api.py::test_slow_broker_call_returns_timeout_envelope` (fake broker `get_quote` sleeps past a `request_timeout=0.2` config; expect the error envelope with `retryable` true, response within ~1 s).
- [ ] **Step 2: Run, expect FAIL** — `.venv/bin/python -m pytest gateway/tests/test_config.py gateway/tests/test_api.py -q -k timeout`.
- [ ] **Step 3: Implement** — config field + env parse + validation; in `_run` wrap `loop.run_in_executor(...)` in `asyncio.wait_for(..., state.config.request_timeout)` converting `asyncio.TimeoutError` to `BrokerUnavailable`; in `history.batch` replace `future.result()` with `future.result(timeout=remaining)` against one shared deadline and, on timeout, fill that slot with the same error envelope the item path already builds. Add a code comment: `shortcut: the worker thread keeps running after the caller times out; only the wait is bounded. Upgrade when adapters expose cancellable calls.`
- [ ] **Step 4: Run full suite, expect PASS** — `.venv/bin/python -m pytest gateway/tests -q`.
- [ ] **Step 5: Commit** — `git add gateway && git commit -m "fix(gateway): bound request and batch wait with GATEWAY_REQUEST_TIMEOUT"`.

### Task 2: Per-item `/v1/quotes` errors and capped fallback (F5)

**Files:**
- Modify: `gateway/src/tradex_gateway/market.py` (`MarketService.quotes` ~line 152, `_quotes_individually` ~line 180) — **stage with `git add -p`**
- Modify: `gateway/src/tradex_gateway/config.py` (`quote_fallback_cap`)
- Test: `gateway/tests/test_api.py`

**Interfaces:**
- Produces: `GatewayConfig.quote_fallback_cap: int = 25` (env `GATEWAY_QUOTE_FALLBACK_CAP`, > 0). `quotes()` return shape unchanged: `{"quotes": {...}, "errors": {id: {code, message}}}`.

- [ ] **Step 1: Failing tests** — `test_quotes_bad_id_is_per_item_error` (one unknown id among valid ones: 200, valid ids in `quotes`, bad id in `errors` with `code == "instrument_not_found"`); `test_quotes_fallback_is_capped` (fake broker raising `CapabilityUnsupported` on `quote_batch`, 60 ids, cap 25: exactly 25 `get_quote` calls, other 35 in `errors` with a canonical message such as `"quote_batch unsupported; fallback capped"`).
- [ ] **Step 2: Run, expect FAIL** — `-k "quotes_bad_id or quotes_fallback"`.
- [ ] **Step 3: Implement** — resolve inside `try/except GatewayError`, recording `errors[raw_id]` and continuing with the resolvable subset (merge those errors into the final dict on both the batch and fallback paths); slice `resolved[:cap]` in `_quotes_individually`, adding the remainder to `errors`. Needs `MarketService` to receive the cap (pass `cfg.quote_fallback_cap` where the service is constructed in `app.py`).
- [ ] **Step 4: Run full suite, expect PASS.**
- [ ] **Step 5: Commit** — `git add -p gateway/src/tradex_gateway/market.py` (only the `quotes`/`_quotes_individually` hunks), then `git add` the rest and `git commit -m "fix(gateway): per-item errors and capped fallback for /v1/quotes"`.

### Task 3: WS connection cap (F3)

**Files:**
- Modify: `gateway/src/tradex_gateway/config.py` (`max_ws_connections`)
- Modify: `gateway/src/tradex_gateway/live.py` (`LiveHub.client_count()`)
- Modify: `gateway/src/tradex_gateway/routes/stream.py:28-36` (check before `ws.accept()`)
- Modify: `gateway/README.md` (state header auth is preferred over `?api_key=`, which lands in access logs)
- Test: `gateway/tests/test_stream*.py` (new `test_stream_connection_cap.py`)

**Interfaces:**
- Produces: `GatewayConfig.max_ws_connections: int = 100` (env `GATEWAY_MAX_WS_CONNECTIONS`, > 0); `LiveHub.client_count() -> int` (takes `self._lock`).

- [ ] **Step 1: Failing tests** — `test_ws_refused_beyond_cap` (cap 2: third connect is closed with code 1013 before any frame); `test_ws_slot_freed_on_disconnect` (close one, a new connect succeeds).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** — after the auth check and before `accept()`, `if hub.client_count() >= cfg.max_ws_connections: await ws.close(code=1013); return`. Race note: check-then-add is not atomic across awaits; accept the small overshoot (`shortcut: soft cap, tolerates a few over under a connect burst; make add_client enforce it if that matters`).
- [ ] **Step 4: Run full suite, expect PASS.**
- [ ] **Step 5: Commit** — `fix(gateway): cap concurrent WS connections`.

### Task 4: Cheap L-list fixes (L1, L4, L6, L7-date, L9)

**Files:**
- Modify: `gateway/src/tradex_gateway/live.py:~305` (L4) and `stop()` ~700-708 (L6)
- Modify: `gateway/src/tradex_gateway/directory.py:~176` (L7)
- Modify: `gateway/src/tradex_gateway/history.py:~317` (L9)
- Modify: `gateway/src/tradex_gateway/routes/__init__.py` (L1: import + `__all__` entry for `expired`)
- Test: `gateway/tests/test_live*.py`, `test_history.py`, `test_directory*.py`

- [ ] **Step 1: Failing tests** — `test_unsubscribe_empty_list_is_noop` (client with 2 subscriptions calls `hub.unsubscribe(cid, [])`; both remain); `test_stop_flags_clients_for_close` (after `hub.stop()` each client's `close_requested` is true before the registry clears); `test_nearest_expiry_uses_ist_date` (inject a clock/date near the UTC/IST midnight boundary); `test_fetch_chunks_tolerates_naive_candle_timestamp` (a naive timestamp is treated as IST/UTC per the adapter convention, no `TypeError`).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** — L4: `elif instrument_ids is not None:` (so `None` + no `subscription_id` still means clear-all, `[]` is a no-op); L6: set `close_requested` on each client in `stop()` before `clear()`; L7: use the repo's IST helper (`to_ist_naive` family in `tradex_domain`) instead of `date.today()`; L9: normalize `candle.timestamp` with the existing `_to_utc` before the comparison at `history.py:317` (decide naive = UTC, matching the "broker timestamps are UTC" rule in CLAUDE.md); L1: add the import/export.
- [ ] **Step 4: Run full suite, expect PASS.**
- [ ] **Step 5: Commit** — `fix(gateway): L-list robustness fixes`.

---

## Deliberately not planned (need your call)

- **L5 gap-on-drop:** needs a protocol decision (new frame vs counter on next frame). Skipped.
- **L3 `/v1/health` exposure:** gate it behind the key or coarsen it? Behavior change for external monitors. Skipped.
- **L2 (hash the rate-limit key), L8 (broker pin metric):** low value; skipped.
- **WS connect-rate token bucket:** the connection cap is the cheap 80%. Skipped.
- **L7 loader lock/close:** benign race; skipped.

## Self-review

Spec coverage: F3 → T3, F5 → T2, F6 → T1, L1/L4/L6/L7(date)/L9 → T4. Names consistent (`request_timeout`, `quote_fallback_cap`, `max_ws_connections`, `client_count`). Unverified assumptions the implementer must confirm in step 1: `BrokerUnavailable`'s constructor/retryable default, where `MarketService` is built in `app.py`, and the existing IST helper's exact name.
