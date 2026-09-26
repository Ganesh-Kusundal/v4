# TradeX v4 Remediation & Hardening Spec

## 1. Problem Statement & Scope
TradeX v4 is an algorithmic trading platform supporting Indian markets (NSE, NFO, BSE, MCX).
During a pre-deployment system review, several critical risks and architectural gaps were identified:
1. **Silent Drops on Control Messages:** `/ws/stream` uses `_enqueue_control_drop_oldest` which silently drops order fills and control frames when the control queue reaches capacity.
2. **Replay vs. Live Trading Boundary:** While frontend checks `replaying`, the server-side `/orders` check relied solely on `replay_guard.active`. Any replay run not coordinated through `replay_guard` or uncoordinated clients could submit live orders.
3. **Hardcoded Market Depth Venues:** `_DEPTH_SUPPORTED_EXCHANGES` in `tradex_domain/market.py` only permitted `NSE`, rejecting NFO, BSE, and MCX.
4. **Startup Cash Verification:** When broker cash cannot be queried at startup on live brokers, the system logs a warning instead of failing closed.
5. **Runtime Validation Test Suite:** Lack of automated regression tests validating the full pipeline from leaf engine to transport and failure injection.

## 2. Technical Decisions & Target Architecture

### A. Non-Dropping Control Queue
- In `tradex_interfaces/queueing.py`:
  - `ticks` queue remains lossy (drop-oldest), because obsolete market quotes/depth have no value when newer ones arrive.
  - `control` queue becomes **non-lossy and backpressure-protected**:
    - Increase default `CONTROL_QUEUE_MAX` from 256 to 4096.
    - If `control` queue ever encounters `QueueFull`, log `CRITICAL: WebSocket control queue full; cannot drop order/execution events` and disconnect the slow client (`ws.close(1008, "control queue backpressure")`) rather than silently dropping fill notifications.

### B. Hardened Replay / Order Boundary
- In `tradex_interfaces/routes/orders.py`:
  - Enforce `_require_trading_mode`:
    - Checks `session.mode != "live"` when live broker actions are attempted if replay is running.
    - If `replay_guard.active` is true OR `session.mode == "replay"`, immediately reject order placement with `HTTP 422 ("orders are disabled during replay")`.

### C. Multi-Exchange Market Depth Generalization
- In `tradex_domain/market.py`:
  - Expand `_DEPTH_SUPPORTED_EXCHANGES` to include `{ExchangeId.NSE, ExchangeId.NFO, ExchangeId.BSE, ExchangeId.BFO, ExchangeId.MCX}`.
  - Ensure `require_depth_supported` verifies exchange validity without unnecessarily blocking derivatives or commodities.

### D. Fail-Closed Startup Cash Gate
- In `tradex_runtime/startup.py`:
  - On live mode, if `_broker_available_cash(broker)` returns `None`, trip the kill switch or abort boot when strict cash gating is configured.

### E. Runtime Audit Test Suite
- Implement `tests/runtime_audit/`:
  - `test_phase1_bootstrap.py`: Verifies composition root fail-closed behavior.
  - `test_phase2_leaf_engine.py`: Tests execution engine, idempotency, risk, cash ledger.
  - `test_phase4_transport.py`: Tests WebSocket transport frames and guarantees zero control drops.
  - `test_phase9_failure_modes.py`: Tests broker gateway timeouts transitioning orders to UNKNOWN (never REJECTED).

## 3. Step-by-Step Remediation Plan

### Step 1: Fix Queueing Logic (`tradex_interfaces/queueing.py`)
Replace silent drop on control queue with high-capacity queue and fail-closed backpressure handling.

### Step 2: Broaden Depth Supported Venues (`tradex_domain/market.py`)
Add NFO, BSE, BFO, MCX to `_DEPTH_SUPPORTED_EXCHANGES`.

### Step 3: Harden Order Submission Against Replay (`tradex_interfaces/routes/orders.py`)
Ensure both session mode and replay guard are strictly validated.

### Step 4: Implement Runtime Audit Verification Suite
Create `tests/runtime_audit/` with automated pytest test cases covering leaf components, transport, and failure modes.
