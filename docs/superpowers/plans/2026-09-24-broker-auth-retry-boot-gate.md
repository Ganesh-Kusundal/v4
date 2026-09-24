# Broker Auth Retry + Boot Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or TDD). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close the Dhan LTP POST 401 hole and fail-closed live boot when the wire probe fails — without a new token subsystem.

**Architecture:** Keep `connect` / `health` / `verify_connection` roles. Extend 401-once eligibility to requests with `cache_read=True` (idempotent market POSTs). After live `broker.connect()`, require `verify_connection()`.

**Tech Stack:** `ProviderHttpClient`, `startup.boot`, existing pytest patterns in `brokers/tests/common` and `trading/tests/sdk`.

---

## File map

| File | Responsibility |
|------|----------------|
| `brokers/src/tradex_brokers/common/provider_client.py` | Auth-retry eligibility includes `cache_read=True` |
| `trading/src/tradex_trading/runtime/startup.py` | Live boot calls `verify_connection()` after `connect()` |
| `brokers/tests/common/test_status_code_chain.py` | POST `cache_read=True` retries; plain POST does not |
| `trading/tests/sdk/test_session_readiness.py` | Live boot fails when verify returns False |

---

### Task 1: Auth-retry for idempotent POSTs

**Files:**
- Modify: `brokers/src/tradex_brokers/common/provider_client.py`
- Test: `brokers/tests/common/test_status_code_chain.py`

- [x] **Step 1: RED** — Add `test_provider_client_auth_retry_on_401_post_cache_read` (expect 2 sends) and `test_provider_client_no_auth_retry_on_post_without_cache_read` (expect 1 send)
- [x] **Step 2: Verify RED** — run those two tests; first fails (1 send), second passes already
- [x] **Step 3: GREEN** — In `request()`, treat `cache_read is True` as auth-retry eligible alongside GET/HEAD/OPTIONS
- [x] **Step 4: Verify GREEN** — both tests pass; existing auth-retry suite still green

### Task 2: Live boot wire gate

**Files:**
- Modify: `trading/src/tradex_trading/runtime/startup.py`
- Test: `trading/tests/sdk/test_session_readiness.py`

- [x] **Step 1: RED** — `test_boot_live_fails_when_verify_connection_false` expects `RuntimeError` / auth probe message
- [x] **Step 2: Verify RED**
- [x] **Step 3: GREEN** — After `broker.connect()`, if `cfg.mode == "live"` and `not broker.verify_connection(): raise RuntimeError(...)`
- [x] **Step 4: Verify GREEN** — new test + `test_boot_live_returns_ready` (MagicMock verify is truthy)

### Task 3: Verification

- [x] Run: `pytest brokers/tests/common/test_status_code_chain.py trading/tests/sdk/test_session_readiness.py -q`
- [x] Spot-check: no auth retry on `submit_mutation` / order POSTs
