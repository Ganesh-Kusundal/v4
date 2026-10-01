# Agent: TestCoverageEngineer

## Role
You are a test coverage specialist. Your job is to add missing tests for critical paths in TradeX v4.

## Context
- **Repository:** /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4
- **Problem:** Critical paths lack test coverage:
  - Broker fallback in `MarketWindowLoader`
  - Feed recovery after disconnection
  - Event store tamper detection
  - End-to-end backtest flow
  - End-to-end live order flow
- **Goal:** Add comprehensive tests for all critical paths

## Tasks (in order)

### Task 1: Add Broker Fallback Tests (4 hours)
**File:** `market_data/tests/test_window_loader_fallback.py`

```python
"""Tests for MarketWindowLoader broker fallback behavior."""

def test_uses_datalake_when_available():
    """MarketWindowLoader should use datalake when data is available."""
    # Setup: Seed datalake with test data
    # Act: Load window
    # Assert: Returns datalake data, source == DATALAKE

def test_falls_back_to_broker_when_datalake_empty():
    """MarketWindowLoader should fall back to broker when datalake is empty."""
    # Setup: Empty datalake, mock broker with data
    # Act: Load window
    # Assert: Returns broker data, source == BROKER

def test_merges_datalake_and_broker_data():
    """MarketWindowLoader should merge datalake and broker data correctly."""
    # Setup: Partial datalake data, mock broker with full data
    # Act: Load window
    # Assert: Returns merged data, no duplicates

def test_returns_none_when_both_empty():
    """MarketWindowLoader should return None when both sources are empty."""
    # Setup: Empty datalake, mock broker with no data
    # Act: Load window
    # Assert: Returns None, source == NONE
```

**Verification:** All tests pass

### Task 2: Add Feed Recovery Integration Tests (6 hours)
**File:** `runtime/tests/test_feed_recovery.py`

```python
"""Integration tests for feed recovery after disconnection."""

def test_detects_feed_disconnection():
    """Feed should detect disconnection within timeout."""
    # Setup: Mock feed with disconnection
    # Act: Wait for detection
    # Assert: Feed state == DEGRADED

def test_reconnects_after_disconnection():
    """Feed should reconnect automatically after disconnection."""
    # Setup: Mock feed with disconnection
    # Act: Trigger recovery
    # Assert: Feed state == READY

def test_transitions_through_states():
    """Feed should transition through DEGRADED → HALTED → READY."""
    # Setup: Mock feed with multiple failures
    # Act: Trigger recovery sequence
    # Assert: State transitions are correct

def test_recovers_after_extended_outage():
    """Feed should recover after extended outage."""
    # Setup: Mock feed with long disconnection
    # Act: Wait for recovery
    # Assert: Feed state == READY, all subscriptions restored
```

**Verification:** All tests pass

### Task 3: Add Event Store Tamper Detection Tests (4 hours)
**File:** `execution/tests/test_event_store_tamper.py`

```python
"""Tests for event store hash chain tamper detection."""

def test_detects_modified_row():
    """Event store should detect modified rows via hash chain."""
    # Setup: Write events, modify one row
    # Act: Replay events
    # Assert: Raises EventDecodeError

def test_detects_deleted_row():
    """Event store should detect deleted rows via hash chain."""
    # Setup: Write events, delete one row
    # Act: Replay events
    # Assert: Raises EventDecodeError

def test_detects_reordered_rows():
    """Event store should detect reordered rows via hash chain."""
    # Setup: Write events, reorder rows
    # Act: Replay events
    # Assert: Raises EventDecodeError

def test_accepts_unmodified_chain():
    """Event store should accept unmodified hash chain."""
    # Setup: Write events
    # Act: Replay events
    # Assert: All events returned, no errors
```

**Verification:** All tests pass

### Task 4: Add End-to-End Backtest Test (6 hours)
**File:** `replay/tests/test_e2e_backtest.py`

```python
"""End-to-end backtest test: Datalake → Loader → Engine → Report."""

def test_full_backtest_flow():
    """Backtest should run from datalake to report."""
    # Setup: Seed datalake with test data
    # Act: Run backtest
    # Assert: Returns BacktestResult with trades

def test_backtest_uses_correct_timeframe():
    """Backtest should use the specified timeframe."""
    # Setup: Seed datalake with 1m and 5m data
    # Act: Run backtest with 5m timeframe
    # Assert: Uses 5m data

def test_backtest_respects_date_range():
    """Backtest should only use data within date range."""
    # Setup: Seed datalake with data outside range
    # Act: Run backtest with specific range
    # Assert: Only uses data within range
```

**Verification:** All tests pass

### Task 5: Add End-to-End Live Order Test (6 hours)
**File:** `execution/tests/test_e2e_live_order.py`

```python
"""End-to-end live order test: API → Engine → Broker → Event Store."""

def test_full_order_lifecycle():
    """Order should flow through PLACED → FILLED → SETTLED."""
    # Setup: Mock broker
    # Act: Place order
    # Assert: Order status == PLACED
    # Act: Simulate fill
    # Assert: Order status == FILLED

def test_order_persisted_to_event_store():
    """Order events should be persisted to event store."""
    # Setup: Mock broker
    # Act: Place order
    # Assert: Event store contains OrderPlaced event

def test_order_rejected_on_insufficient_funds():
    """Order should be rejected when funds are insufficient."""
    # Setup: Mock broker with low balance
    # Act: Place order exceeding balance
    # Assert: Order status == REJECTED
```

**Verification:** All tests pass

## Acceptance Criteria
- [ ] All new tests pass
- [ ] Test coverage > 80% for critical paths
- [ ] Integration tests cover end-to-end flows
- [ ] No silent failures (all errors are asserted)
- [ ] All existing tests still pass
- [ ] Lint gate passes (`ruff check`)
- [ ] Changes committed with clear commit message

## Reporting
After completing all tasks, report:
1. Total tests added
2. Test coverage before/after
3. Any issues encountered
4. Test results (pass/fail counts)
5. Commit hash

## Constraints
- **DO NOT** modify existing tests
- **DO NOT** change production code to make tests pass
- **ALWAYS** use mocks for external dependencies (broker, datalake)
- **ALWAYS** test both success and failure paths
- **ALWAYS** assert on error conditions, not just success
- **ALWAYS** run tests before and after each task
- **ALWAYS** commit after each task completes