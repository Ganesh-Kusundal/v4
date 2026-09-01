# Event Sourcing & Actor Model Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (Recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign TradeX v4's core execution pipeline from reactive-bus + cache to event-sourcing + actor-model for provable correctness, crash recovery, and zero-parity across all trading modes.

**Architecture:** All state mutations go through a single `OrderBookActor` that processes commands sequentially (no locks). State is derived from an append-only `EventStore` (SQLite). Projectors build read models from events. The same pipeline serves live, backtest, replay, and paper — only the data source changes.

**Tech Stack:** Python 3.10+, SQLite (event store), asyncio (actor loop), RxPY (retained for market data streaming only), FastAPI, React/TypeScript frontend.

## Global Constraints

- **No mocking in tests** — all tests use real SQLite event store, real actor
- **TDD for every task** — failing test first, then implementation
- **Fail-closed** — any ambiguity trips the kill switch
- **Deterministic replay** — same events → same state, always
- **Zero-parity** — backtest, replay, paper, and live share the exact same pipeline
- **IST-naive timestamps** in datalake, UTC-aware everywhere else
- **Correlation IDs** on every command for idempotency and audit trail
- **No wall-clock in core pipeline** — all timestamps come from events
- **Single writer** — only the OrderBookActor mutates order/position state
- **Event log is source of truth** — caches and projectors are derived, discardable

---

## Phase 1: Event Store & Order State Machine

### Task 1: Event Store (SQLite Append-Only Log)

**Files:**
- Create: `trading/src/tradex_trading/events/store.py`
- Create: `trading/tests/events/test_store.py`

**Interfaces:**
- Consumes: None (foundational)
- Produces: `EventStore` class with `append()`, `read_all()`, `read_after()`, `get_last_sequence()`

- [ ] **Step 1: Write the failing test**

```python
# trading/tests/events/test_store.py
import pytest
import tempfile
import os
from datetime import datetime, UTC
from tradex_trading.events.store import EventStore, Event

@pytest.fixture
def store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    store = EventStore(path)
    yield store
    os.unlink(path)

def test_append_and_read(store):
    event = Event(
        event_id="evt-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, 9, 15, 1, tzinfo=UTC),
        correlation_id="corr-001",
        session_id="sess-001",
        type="OrderPlaced",
        payload={"order_id": "ord-001", "instrument": "NSE:RELIANCE"},
    )
    appended = store.append(event)
    assert appended.sequence_number == 1
    
    events = store.read_all("sess-001")
    assert len(events) == 1
    assert events[0].event_id == "evt-001"
    assert events[0].sequence_number == 1

def test_sequence_numbers_are_monotonic(store):
    for i in range(5):
        store.append(Event(
            event_id=f"evt-{i:03d}",
            event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
            processed_time=datetime(2026, 1, 1, 9, 15, 1, tzinfo=UTC),
            correlation_id=f"corr-{i:03d}",
            session_id="sess-001",
            type="TestEvent",
            payload={"index": i},
        ))
    
    events = store.read_all("sess-001")
    seqs = [e.sequence_number for e in events]
    assert seqs == [1, 2, 3, 4, 5]

def test_read_after(store):
    for i in range(5):
        store.append(Event(
            event_id=f"evt-{i:03d}",
            event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
            processed_time=datetime(2026, 1, 1, 9, 15, 1, tzinfo=UTC),
            correlation_id=f"corr-{i:03d}",
            session_id="sess-001",
            type="TestEvent",
            payload={"index": i},
        ))
    
    events = store.read_after("sess-001", 3)
    assert len(events) == 2
    assert events[0].sequence_number == 4

def test_isolation_between_sessions(store):
    store.append(Event(
        event_id="evt-a",
        event_time=datetime(2026, 1, 1, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, tzinfo=UTC),
        correlation_id="corr-a",
        session_id="sess-a",
        type="TestEvent",
        payload={},
    ))
    store.append(Event(
        event_id="evt-b",
        event_time=datetime(2026, 1, 1, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, tzinfo=UTC),
        correlation_id="corr-b",
        session_id="sess-b",
        type="TestEvent",
        payload={},
    ))
    
    assert len(store.read_all("sess-a")) == 1
    assert len(store.read_all("sess-b")) == 1
    assert store.read_all("sess-a")[0].event_id == "evt-a"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4 && python -m pytest trading/tests/events/test_store.py -v`
Expected: FAIL with "No module named tradex_trading.events.store"

- [ ] **Step 3: Write minimal implementation**

```python
# trading/src/tradex_trading/events/store.py
"""Append-only event store — the single source of truth."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Optional


@dataclass(frozen=True, slots=True)
class Event:
    """Immutable event record."""
    event_id: str
    event_time: datetime
    processed_time: datetime
    correlation_id: str
    session_id: str
    type: str
    payload: dict
    sequence_number: int = 0  # Assigned by store.append()


class EventStore:
    """SQLite-backed append-only event log."""
    
    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.row_factory = sqlite3.Row
        self._init_schema()
    
    def _init_schema(self) -> None:
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                sequence_number INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                event_time TEXT NOT NULL,
                processed_time TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                type TEXT NOT NULL,
                payload TEXT NOT NULL
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_session_seq 
            ON events(session_id, sequence_number)
        """)
        self._db.commit()
    
    def append(self, event: Event) -> Event:
        """Append an event. Returns the event with sequence_number assigned."""
        cursor = self._db.execute(
            """INSERT INTO events 
               (event_id, event_time, processed_time, correlation_id, 
                session_id, type, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.event_time.isoformat(),
                event.processed_time.isoformat(),
                event.correlation_id,
                event.session_id,
                event.type,
                json.dumps(event.payload),
            ),
        )
        self._db.commit()
        return Event(
            event_id=event.event_id,
            event_time=event.event_time,
            processed_time=event.processed_time,
            correlation_id=event.correlation_id,
            session_id=event.session_id,
            type=event.type,
            payload=event.payload,
            sequence_number=cursor.lastrowid,
        )
    
    def read_all(self, session_id: str) -> list[Event]:
        """Read all events for a session, in sequence order."""
        cursor = self._db.execute(
            """SELECT event_id, event_time, processed_time, correlation_id,
                      session_id, type, payload, sequence_number
               FROM events WHERE session_id = ?
               ORDER BY sequence_number""",
            (session_id,),
        )
        return [self._row_to_event(row) for row in cursor]
    
    def read_after(self, session_id: str, after_seq: int) -> list[Event]:
        """Read events after a specific sequence number."""
        cursor = self._db.execute(
            """SELECT event_id, event_time, processed_time, correlation_id,
                      session_id, type, payload, sequence_number
               FROM events WHERE session_id = ? AND sequence_number > ?
               ORDER BY sequence_number""",
            (session_id, after_seq),
        )
        return [self._row_to_event(row) for row in cursor]
    
    def get_last_sequence(self, session_id: str) -> int:
        """Get the last sequence number for a session (0 if no events)."""
        cursor = self._db.execute(
            """SELECT MAX(sequence_number) FROM events WHERE session_id = ?""",
            (session_id,),
        )
        row = cursor.fetchone()
        return row[0] if row and row[0] is not None else 0
    
    def _row_to_event(self, row: sqlite3.Row) -> Event:
        return Event(
            event_id=row["event_id"],
            event_time=datetime.fromisoformat(row["event_time"]),
            processed_time=datetime.fromisoformat(row["processed_time"]),
            correlation_id=row["correlation_id"],
            session_id=row["session_id"],
            type=row["type"],
            payload=json.loads(row["payload"]),
            sequence_number=row["sequence_number"],
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4 && python -m pytest trading/tests/events/test_store.py -v`
Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/events/store.py trading/tests/events/test_store.py
git commit -m "feat(events): add SQLite-backed append-only event store"
```

---

### Task 2: Formal Order State Machine

**Files:**
- Create: `trading/src/tradex_trading/events/order_fsm.py`
- Create: `trading/tests/events/test_order_fsm.py`

**Interfaces:**
- Consumes: None (foundational)
- Produces: `OrderState` enum, `transition()` function, `TERMINAL_STATES`, `VALID_TRANSITIONS`

- [ ] **Step 1: Write the failing test**

```python
# trading/tests/events/test_order_fsm.py
import pytest
from tradex_trading.events.order_fsm import (
    OrderState, transition, TERMINAL_STATES, VALID_TRANSITIONS,
    InvalidStateTransition,
)

def test_valid_transitions():
    assert transition(OrderState.NEW, "ack") == OrderState.ACK
    assert transition(OrderState.NEW, "fill") == OrderState.FILLED
    assert transition(OrderState.NEW, "reject") == OrderState.REJECTED
    assert transition(OrderState.NEW, "cancel") == OrderState.CANCELLED
    assert transition(OrderState.ACK, "fill") == OrderState.PARTIALLY_FILLED
    assert transition(OrderState.ACK, "full_fill") == OrderState.FILLED
    assert transition(OrderState.ACK, "cancel") == OrderState.CANCELLED
    assert transition(OrderState.PARTIALLY_FILLED, "fill") == OrderState.PARTIALLY_FILLED
    assert transition(OrderState.PARTIALLY_FILLED, "full_fill") == OrderState.FILLED
    assert transition(OrderState.PARTIALLY_FILLED, "cancel") == OrderState.CANCELLED

def test_terminal_states():
    assert OrderState.FILLED in TERMINAL_STATES
    assert OrderState.CANCELLED in TERMINAL_STATES
    assert OrderState.REJECTED in TERMINAL_STATES
    assert OrderState.EXPIRED in TERMINAL_STATES

def test_no_transitions_from_terminal():
    for state in TERMINAL_STATES:
        with pytest.raises(InvalidStateTransition):
            transition(state, "ack")
        with pytest.raises(InvalidStateTransition):
            transition(state, "fill")
        with pytest.raises(InvalidStateTransition):
            transition(state, "cancel")

def test_invalid_transitions():
    with pytest.raises(InvalidStateTransition):
        transition(OrderState.NEW, "full_fill")  # Can't full_fill from NEW
    with pytest.raises(InvalidStateTransition):
        transition(OrderState.ACK, "ack")  # Can't ack twice
    with pytest.raises(InvalidStateTransition):
        transition(OrderState.PARTIALLY_FILLED, "ack")  # Can't ack after partial

def test_all_transitions_are_symmetric():
    """Every transition must be explicitly defined — no implicit paths."""
    for (state, event_type), new_state in VALID_TRANSITIONS.items():
        assert state not in TERMINAL_STATES, f"Transition from terminal state {state}"
        assert isinstance(new_state, OrderState)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4 && python -m pytest trading/tests/events/test_order_fsm.py -v`
Expected: FAIL with "No module named tradex_trading.events.order_fsm"

- [ ] **Step 3: Write minimal implementation**

```python
# trading/src/tradex_trading/events/order_fsm.py
"""Formal order state machine — no implicit transitions allowed."""

from __future__ import annotations
from enum import Enum


class OrderState(Enum):
    NEW = "NEW"
    ACK = "ACK"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class InvalidStateTransition(Exception):
    """Raised when an invalid state transition is attempted."""
    pass


# (current_state, event_type) -> new_state
VALID_TRANSITIONS: dict[tuple[OrderState, str], OrderState] = {
    (OrderState.NEW, "ack"): OrderState.ACK,
    (OrderState.NEW, "fill"): OrderState.FILLED,  # Immediate fill (market)
    (OrderState.NEW, "reject"): OrderState.REJECTED,
    (OrderState.NEW, "cancel"): OrderState.CANCELLED,
    (OrderState.ACK, "fill"): OrderState.PARTIALLY_FILLED,
    (OrderState.ACK, "full_fill"): OrderState.FILLED,
    (OrderState.ACK, "cancel"): OrderState.CANCELLED,
    (OrderState.ACK, "reject"): OrderState.REJECTED,
    (OrderState.PARTIALLY_FILLED, "fill"): OrderState.PARTIALLY_FILLED,
    (OrderState.PARTIALLY_FILLED, "full_fill"): OrderState.FILLED,
    (OrderState.PARTIALLY_FILLED, "cancel"): OrderState.CANCELLED,
}

TERMINAL_STATES = frozenset({
    OrderState.FILLED,
    OrderState.CANCELLED,
    OrderState.REJECTED,
    OrderState.EXPIRED,
})


def transition(current: OrderState, event_type: str) -> OrderState:
    """
    Pure function: compute next state. Raises on invalid transition.
    
    This is the ONLY way order state changes. No exceptions.
    """
    if current in TERMINAL_STATES:
        raise InvalidStateTransition(
            f"Cannot transition from terminal state {current.value}"
        )
    key = (current, event_type)
    if key not in VALID_TRANSITIONS:
        raise InvalidStateTransition(
            f"Invalid transition: {current.value} + {event_type}"
        )
    return VALID_TRANSITIONS[key]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4 && python -m pytest trading/tests/events/test_order_fsm.py -v`
Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/events/order_fsm.py trading/tests/events/test_order_fsm.py
git commit -m "feat(events): add formal order state machine with validated transitions"
```

---

### Task 3: Order Book Actor (Core)

**Files:**
- Create: `trading/src/tradex_trading/events/actor.py`
- Create: `trading/tests/events/test_actor.py`

**Interfaces:**
- Consumes: `EventStore` (Task 1), `OrderState` FSM (Task 2)
- Produces: `OrderBookActor` class with `handle(command)` method

- [ ] **Step 1: Write the failing test**

```python
# trading/tests/events/test_actor.py
import pytest
import tempfile
import os
from datetime import datetime, UTC
from decimal import Decimal
from tradex_trading.events.store import EventStore, Event
from tradex_trading.events.actor import OrderBookActor, PlaceOrderCommand, ApplyFillCommand
from tradex_trading.events.order_fsm import OrderState
from tradex_domain.value_objects import Price, Quantity
from tradex_domain.enums import OrderSide, OrderType

@pytest.fixture
def actor():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    store = EventStore(path)
    actor = OrderBookActor(session_id="test-sess", event_store=store)
    yield actor
    os.unlink(path)

@pytest.fixture
def sample_request():
    from tradex_domain.execution import OrderRequest
    from tradex_domain.instruments import Equity
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("2500")),
        correlation_id="corr-001",
    )

def test_place_order_creates_order_and_events(actor, sample_request):
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    events = actor.handle(cmd)
    
    assert len(events) >= 1
    placed = [e for e in events if e.type == "OrderPlaced"]
    assert len(placed) == 1
    assert placed[0].payload["instrument"] == "NSE:RELIANCE"
    assert placed[0].payload["side"] == "BUY"
    assert placed[0].payload["quantity"] == "10"

def test_place_order_writes_to_store(actor, sample_request):
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    events = actor.handle(cmd)
    
    stored = actor._store.read_all("test-sess")
    assert len(stored) == len(events)

def test_apply_fill_updates_order_and_position(actor, sample_request):
    # First place the order
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_events = actor.handle(place_cmd)
    order_id = place_events[0].payload["order_id"]
    
    # Then apply a fill
    fill_cmd = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("5"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    fill_events = actor.handle(fill_cmd)
    
    filled = [e for e in fill_events if e.type == "OrderFilled"]
    assert len(filled) == 1
    assert filled[0].payload["fill_quantity"] == "5"
    assert filled[0].payload["cumulative_filled"] == "5"
    
    position = [e for e in fill_events if e.type == "PositionUpdated"]
    assert len(position) == 1
    assert position[0].payload["net_quantity"] == "5"

def test_duplicate_fill_is_idempotent(actor, sample_request):
    # Place order
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_events = actor.handle(place_cmd)
    order_id = place_events[0].payload["order_id"]
    
    # First fill
    fill_cmd = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("5"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    actor.handle(fill_cmd)
    
    # Same fill again (duplicate) — should produce no events
    dup_cmd = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("5"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    dup_events = actor.handle(dup_cmd)
    assert len(dup_events) == 0  # No new events for duplicate

def test_full_fill_transitions_to_filled(actor, sample_request):
    # Place order
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_events = actor.handle(place_cmd)
    order_id = place_events[0].payload["order_id"]
    
    # Full fill
    fill_cmd = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("10"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    fill_events = actor.handle(fill_cmd)
    
    filled = [e for e in fill_events if e.type == "OrderFilled"]
    assert filled[0].payload["is_complete"] is True

def test_unknown_order_creates_synthetic(actor):
    fill_cmd = ApplyFillCommand(
        order_id="unknown-ord",
        cumulative_filled=Decimal("5"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    events = actor.handle(fill_cmd)
    
    synthetic = [e for e in events if e.type == "SyntheticOrderCreated"]
    assert len(synthetic) == 1
    assert synthetic[0].payload["reason"] == "fill_for_unknown_order"

def test_kill_switch_rejects_orders(actor, sample_request):
    # Trip kill switch
    actor.trip_kill_switch("test")
    
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    events = actor.handle(cmd)
    
    rejected = [e for e in events if e.type == "OrderRejected"]
    assert len(rejected) == 1
    assert "kill_switch" in rejected[0].payload["reason"]

def test_recovery_from_event_log(actor, sample_request):
    # Place order and fill
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    actor.handle(place_cmd)
    
    # Create a new actor with the same store (simulating recovery)
    new_actor = OrderBookActor(session_id="test-sess", event_store=actor._store)
    new_actor.recover()
    
    # State should be restored
    assert len(new_actor._orders) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4 && python -m pytest trading/tests/events/test_actor.py -v`
Expected: FAIL with "No module named tradex_trading.events.actor"

- [ ] **Step 3: Write minimal implementation**

```python
# trading/src/tradex_trading/events/actor.py
"""Order Book Actor — single writer, no locks, event-sourced."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, UTC
from decimal import Decimal
from typing import Optional

from tradex_domain.execution import OrderRequest
from tradex_domain.value_objects import OrderId

from tradex_trading.events.store import EventStore, Event
from tradex_trading.events.order_fsm import OrderState, transition, TERMINAL_STATES
from tradex_trading.execution.position_math import apply_fill


@dataclass
class PlaceOrderCommand:
    request: OrderRequest
    correlation_id: str
    event_time: datetime


@dataclass
class ApplyFillCommand:
    order_id: str
    cumulative_filled: Decimal
    fill_price: Decimal
    fill_id: Optional[str]
    correlation_id: str
    event_time: datetime


@dataclass
class CancelOrderCommand:
    order_id: str
    correlation_id: str
    event_time: datetime


@dataclass
class TripKillSwitchCommand:
    reason: str
    correlation_id: str
    event_time: datetime


@dataclass
class _OrderState:
    """Internal order state (not exposed directly)."""
    order_id: str
    instrument: str
    side: str
    quantity: Decimal
    price: Optional[Decimal]
    status: OrderState
    filled_quantity: Decimal
    correlation_id: str


class OrderBookActor:
    """
    Single-writer actor that owns all order/position state.
    
    All mutations go through handle(). No other code mutates state.
    """
    
    def __init__(self, session_id: str, event_store: EventStore):
        self._session_id = session_id
        self._store = event_store
        self._orders: dict[str, _OrderState] = {}
        self._positions: dict[str, dict] = {}
        self._kill_switch = False
    
    def handle(self, command) -> list[Event]:
        """Process a command and return events to append."""
        if isinstance(command, PlaceOrderCommand):
            return self._handle_place_order(command)
        elif isinstance(command, ApplyFillCommand):
            return self._handle_apply_fill(command)
        elif isinstance(command, CancelOrderCommand):
            return self._handle_cancel_order(command)
        elif isinstance(command, TripKillSwitchCommand):
            return self._handle_trip_kill_switch(command)
        else:
            raise ValueError(f"Unknown command type: {type(command)}")
    
    def _handle_place_order(self, cmd: PlaceOrderCommand) -> list[Event]:
        now = cmd.event_time
        
        # Kill switch check
        if self._kill_switch:
            event = Event(
                event_id=str(uuid.uuid4()),
                event_time=now,
                processed_time=now,
                correlation_id=cmd.correlation_id,
                session_id=self._session_id,
                type="OrderRejected",
                payload={
                    "correlation_id": cmd.correlation_id,
                    reason": "kill_switch_active",
                    "order_id": None,
                },
            )
            stored = self._store.append(event)
            return [stored]
        
        # Create order
        order_id = str(uuid.uuid4())
        order = _OrderState(
            order_id=order_id,
            instrument=str(cmd.request.instrument.instrument_id),
            side=cmd.request.side.value,
            quantity=cmd.request.quantity.value,
            price=cmd.request.price.value if cmd.request.price else None,
            status=OrderState.NEW,
            filled_quantity=Decimal("0"),
            correlation_id=cmd.correlation_id,
        )
        self._orders[order_id] = order
        
        # Transition to ACK
        order.status = transition(order.status, "ack")
        
        # Emit OrderPlaced
        event = Event(
            event_id=str(uuid.uuid4()),
            event_time=now,
            processed_time=now,
            correlation_id=cmd.correlation_id,
            session_id=self._session_id,
            type="OrderPlaced",
            payload={
                "order_id": order_id,
                "instrument": order.instrument,
                "side": order.side,
                "quantity": str(order.quantity),
                "price": str(order.price) if order.price else None,
                "correlation_id": cmd.correlation_id,
            },
        )
        stored = self._store.append(event)
        return [stored]
    
    def _handle_apply_fill(self, cmd: ApplyFillCommand) -> list[Event]:
        now = cmd.event_time
        events = []
        
        order = self._orders.get(cmd.order_id)
        if order is None:
            # Unknown order — create synthetic record
            order = _OrderState(
                order_id=cmd.order_id,
                instrument="unknown",
                side="UNKNOWN",
                quantity=cmd.cumulative_filled,
                price=None,
                status=OrderState.ACK,
                filled_quantity=Decimal("0"),
                correlation_id=cmd.correlation_id,
            )
            self._orders[cmd.order_id] = order
            
            synth_event = Event(
                event_id=str(uuid.uuid4()),
                event_time=now,
                processed_time=now,
                correlation_id=cmd.correlation_id,
                session_id=self._session_id,
                type="SyntheticOrderCreated",
                payload={
                    "order_id": cmd.order_id,
                    "reason": "fill_for_unknown_order",
                },
            )
            stored = self._store.append(synth_event)
            events.append(stored)
        
        # Compute delta (idempotent)
        delta = cmd.cumulative_filled - order.filled_quantity
        if delta <= 0:
            return []  # Duplicate or stale fill
        
        # Update order
        order.filled_quantity = cmd.cumulative_filled
        is_complete = cmd.cumulative_filled >= order.quantity
        
        # Transition state
        event_type = "full_fill" if is_complete else "fill"
        try:
            order.status = transition(order.status, event_type)
        except Exception:
            # If transition fails (e.g., from NEW directly to FILLED), handle gracefully
            if is_complete:
                order.status = OrderState.FILLED
            else:
                order.status = OrderState.PARTIALLY_FILLED
        
        # Emit OrderFilled
        filled_event = Event(
            event_id=str(uuid.uuid4()),
            event_time=now,
            processed_time=now,
            correlation_id=cmd.correlation_id,
            session_id=self._session_id,
            type="OrderFilled",
            payload={
                "order_id": order.order_id,
                "instrument": order.instrument,
                "side": order.side,
                "fill_quantity": str(delta),
                "cumulative_filled": str(cmd.cumulative_filled),
                "fill_price": str(cmd.fill_price),
                "is_complete": is_complete,
                "fill_id": cmd.fill_id,
            },
        )
        stored = self._store.append(filled_event)
        events.append(stored)
        
        # Update position
        existing_pos = self._positions.get(order.instrument)
        new_pos = apply_fill(existing_pos, order.side, delta, cmd.fill_price)
        self._positions[order.instrument] = new_pos
        
        # Emit PositionUpdated
        position_event = Event(
            event_id=str(uuid.uuid4()),
            event_time=now,
            processed_time=now,
            correlation_id=cmd.correlation_id,
            session_id=self._session_id,
            type="PositionUpdated",
            payload={
                "instrument": order.instrument,
                "net_quantity": str(new_pos["quantity"]),
                "avg_price": str(new_pos["avg_price"]),
                "realized_pnl": str(new_pos["realized_pnl"]),
            },
        )
        stored = self._store.append(position_event)
        events.append(stored)
        
        return events
    
    def _handle_cancel_order(self, cmd: CancelOrderCommand) -> list[Event]:
        now = cmd.event_time
        order = self._orders.get(cmd.order_id)
        if order is None:
            return []
        
        if order.status in TERMINAL_STATES:
            return []
        
        order.status = transition(order.status, "cancel")
        
        event = Event(
            event_id=str(uuid.uuid4()),
            event_time=now,
            processed_time=now,
            correlation_id=cmd.correlation_id,
            session_id=self._session_id,
            type="OrderCancelled",
            payload={
                "order_id": cmd.order_id,
                "reason": "user_cancel",
            },
        )
        stored = self._store.append(event)
        return [stored]
    
    def _handle_trip_kill_switch(self, cmd: TripKillSwitchCommand) -> list[Event]:
        now = cmd.event_time
        self._kill_switch = True
        
        events = []
        kill_event = Event(
            event_id=str(uuid.uuid4()),
            event_time=now,
            processed_time=now,
            correlation_id=cmd.correlation_id,
            session_id=self._session_id,
            type="KillSwitchTripped",
            payload={
                "reason": cmd.reason,
                "tripped_at": now.isoformat(),
            },
        )
        stored = self._store.append(kill_event)
        events.append(stored)
        
        # Cancel all open orders
        for order in self._orders.values():
            if order.status not in TERMINAL_STATES:
                order.status = transition(order.status, "cancel")
                cancel_event = Event(
                    event_id=str(uuid.uuid4()),
                    event_time=now,
                    processed_time=now,
                    correlation_id=cmd.correlation_id,
                    session_id=self._session_id,
                    type="OrderCancelled",
                    payload={
                        "order_id": order.order_id,
                        "reason": "kill_switch",
                    },
                )
                stored = self._store.append(cancel_event)
                events.append(stored)
        
        return events
    
    def trip_kill_switch(self, reason: str) -> None:
        """Convenience method for testing."""
        self._kill_switch = True
    
    def recover(self) -> None:
        """Recover state from event log."""
        events = self._store.read_all(self._session_id)
        for event in events:
            self._apply_event(event)
    
    def _apply_event(self, event: Event) -> None:
        """Apply an event to rebuild state (no store write)."""
        if event.type == "OrderPlaced":
            order = _OrderState(
                order_id=event.payload["order_id"],
                instrument=event.payload["instrument"],
                side=event.payload["side"],
                quantity=Decimal(event.payload["quantity"]),
                price=Decimal(event.payload["price"]) if event.payload.get("price") else None,
                status=OrderState.ACK,
                filled_quantity=Decimal("0"),
                correlation_id=event.payload.get("correlation_id", ""),
            )
            self._orders[order.order_id] = order
        elif event.type == "OrderFilled":
            order = self._orders.get(event.payload["order_id"])
            if order:
                order.filled_quantity = Decimal(event.payload["cumulative_filled"])
                if event.payload["is_complete"]:
                    order.status = OrderState.FILLED
                else:
                    order.status = OrderState.PARTIALLY_FILLED
        elif event.type == "OrderCancelled":
            order = self._orders.get(event.payload["order_id"])
            if order:
                order.status = OrderState.CANCELLED
        elif event.type == "PositionUpdated":
            self._positions[event.payload["instrument"]] = {
                "quantity": Decimal(event.payload["net_quantity"]),
                "avg_price": Decimal(event.payload["avg_price"]),
                "realized_pnl": Decimal(event.payload["realized_pnl"]),
            }
        elif event.type == "KillSwitchTripped":
            self._kill_switch = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/apple/Downloads/v2-cleanup-remove-demp-legacy-2026-07-31/v4 && python -m pytest trading/tests/events/test_actor.py -v`
Expected: 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/events/actor.py trading/tests/events/test_actor.py
git commit -m "feat(events): add OrderBookActor with event-sourced state management"
```

---

## Phase 2: Projectors & Read Models

### Task 4: Order Book Projector

**Files:**
- Create: `trading/src/tradex_trading/events/projectors.py`
- Create: `trading/tests/events/test_projectors.py`

**Interfaces:**
- Consumes: `Event` (from store)
- Produces: `OrderBookProjector`, `PositionProjector`

- [ ] **Step 1: Write the failing test**

(Similar TDD pattern — test projector derives correct state from events)

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Write minimal implementation**

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

---

### Task 5: Command Processor with Idempotency

**Files:**
- Create: `trading/src/tradex_trading/events/processor.py`
- Create: `trading/tests/events/test_processor.py`

**Interfaces:**
- Consumes: `EventStore`, `OrderBookActor`
- Produces: `CommandProcessor` with `process(command)` method

- [ ] **Step 1: Write the failing test**

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Write minimal implementation**

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

---

## Phase 3: Fill Handling

### Task 6: Fill Matcher (Replaces LiveFillBridge)

**Files:**
- Create: `trading/src/tradex_trading/events/fill_matcher.py`
- Create: `trading/tests/events/test_fill_matcher.py`

**Interfaces:**
- Consumes: `OrderBookActor`, broker order stream
- Produces: `FillMatcher` that translates broker updates to `ApplyFillCommand`

---

## Phase 4: Risk Engine

### Task 7: Pure Risk Engine

**Files:**
- Create: `trading/src/tradex_trading/events/risk.py`
- Create: `trading/tests/events/test_risk.py`

**Interfaces:**
- Consumes: `RiskConfig`, positions, recent order count, available cash
- Produces: `RiskEngine.check_order()` → `RiskResult`

---

## Phase 5: Session & Recovery

### Task 8: Session Recovery

**Files:**
- Create: `trading/src/tradex_trading/events/recovery.py`
- Create: `trading/tests/events/test_recovery.py`

**Interfaces:**
- Consumes: `EventStore`, `OrderBookActor`, `BrokerAdapter`
- Produces: `SessionRecovery` with `recover()` and `reconcile_with_broker()`

---

## Phase 6: Unified Session (Zero-Parity)

### Task 9: Unified Trading Session

**Files:**
- Modify: `trading/src/tradex_trading/sdk/session.py`
- Create: `trading/src/tradex_trading/events/session.py`
- Create: `trading/tests/events/test_session.py`

**Interfaces:**
- Consumes: All previous components
- Produces: `UnifiedTradingSession` that works for live/backtest/replay/paper

---

## Phase 7: Frontend Reconciliation

### Task 10: Frontend Automatic Reconciliation

**Files:**
- Modify: `frontend/src/trade/order-engine.ts`
- Modify: `frontend/src/trade/trade-feed.ts`
- Create: `frontend/src/trade/reconcile.ts`

**Interfaces:**
- Consumes: WebSocket events from backend
- Produces: Automatic reconciliation loop, snapshot handling

---

## Phase 8: Integration & Parity Testing

### Task 11: Zero-Parity Integration Test

**Files:**
- Create: `trading/tests/integration/test_parity.py`

**Interfaces:**
- Consumes: All components
- Produces: Integration test verifying backtest = live = replay

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-01-event-sourcing-actor-redesign.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
