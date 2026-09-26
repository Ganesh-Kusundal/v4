"""OMS crash recovery integration tests (SDD checklist 2.3).

Uses real SQLite temp files (``tmp_path``).  No mocks of the event store.
Each test simulates one scenario where a process dies mid-operation and
the next boot must reconstruct correct OMS state from the durable log.

Scenarios
---------
1. Clean session: all events written, recovery rebuilds full projection.
2. Crash before first event: empty store, recovery is a no-op.
3. Crash mid-session after N events: those N events are visible post-restart.
4. Fill-only orphan: fill arrives (and is persisted) before OrderPlaced;
   recovery synthesises a minimal stub Order.
5. Partial fill sequence: a sequence of partial fills accumulates correctly.
6. Multi-session: events from a prior session remain durable when a new
   session appends on top.
7. Rejection survives restart: OrderRejected is replayed, rejected order is
   in the repository post-recovery.
8. Cancel survives restart: OrderCancelled is replayed.
9. Sequence ordering guarantee: events always replayed in append order,
   regardless of insertion interleaving.
10. SessionRecovery.last_sequence tracks total replayed events.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import (
    OrderCancelled,
    OrderFilled,
    OrderPlaced,
    OrderRejected,
)
from tradex_domain.execution import Fill, Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.execution.recovery import (
    SessionRecovery,
    SQLiteEventStore,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _eq() -> Equity:
    return Equity.of("NSE", "INFY")


def _order(
    order_id: str = "o-1",
    status: OrderStatus = OrderStatus.NEW,
    filled_qty: str = "0",
) -> Order:
    return Order(
        order_id=OrderId(order_id),
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("100")),
        price=Price(Decimal("1500")),
        time_in_force=TimeInForce.DAY,
        status=status,
        filled_quantity=Quantity(Decimal(filled_qty)),
    )


def _fill(order_id: str = "o-1", qty: str = "50") -> Fill:
    return Fill(
        order_id=OrderId(order_id),
        instrument=_eq(),
        side=OrderSide.BUY,
        quantity=Quantity(Decimal(qty)),
        price=Price(Decimal("1500")),
    )


class _Repo:
    """Minimal in-process OrderRepository for recovery projection tests."""

    def __init__(self) -> None:
        self._store: dict[str, Order] = {}

    def save(self, order: Order) -> None:
        self._store[order.order_id.value] = order

    def get(self, order_id: str) -> Order | None:
        return self._store.get(order_id)

    def all(self):
        return tuple(self._store.values())


def _db(tmp_path: Path) -> Path:
    return tmp_path / "oms.db"


# ---------------------------------------------------------------------------
# Scenario 1: clean session — full projection rebuild
# ---------------------------------------------------------------------------

def test_clean_session_full_recovery(tmp_path: Path) -> None:
    db = _db(tmp_path)

    # Session 1: two orders placed
    s1 = SQLiteEventStore(db)
    s1.append(OrderPlaced(order=_order("o-1")))
    s1.append(OrderPlaced(order=_order("o-2")))
    s1.close()

    # "Process restart" — reopen
    s2 = SQLiteEventStore(db)
    repo = _Repo()
    result = SessionRecovery(s2, repo).recover()
    s2.close()

    assert result.orders_recovered == 2
    assert result.events_replayed == 2
    assert repo.get("o-1") is not None
    assert repo.get("o-2") is not None


# ---------------------------------------------------------------------------
# Scenario 2: crash before any events — empty store
# ---------------------------------------------------------------------------

def test_crash_before_any_events(tmp_path: Path) -> None:
    db = _db(tmp_path)
    store = SQLiteEventStore(db)
    store.close()

    s2 = SQLiteEventStore(db)
    repo = _Repo()
    result = SessionRecovery(s2, repo).recover()
    s2.close()

    assert result.orders_recovered == 0
    assert result.events_replayed == 0
    assert result.last_sequence == 0


# ---------------------------------------------------------------------------
# Scenario 3: crash after N events mid-session
# ---------------------------------------------------------------------------

def test_partial_session_events_survive_crash(tmp_path: Path) -> None:
    db = _db(tmp_path)

    # Write 3 events then "crash" (close without graceful shutdown marker)
    s1 = SQLiteEventStore(db)
    s1.append(OrderPlaced(order=_order("o-1")))
    s1.append(OrderPlaced(order=_order("o-2")))
    s1.append(OrderFilled(fill=_fill("o-1")))
    s1.close()

    s2 = SQLiteEventStore(db)
    repo = _Repo()
    result = SessionRecovery(s2, repo).recover()
    s2.close()

    # 2 OrderPlaced → 2 orders saved; 1 OrderFilled for existing order → no new order
    assert result.events_replayed == 3
    assert result.orders_recovered == 2
    assert repo.get("o-1") is not None


# ---------------------------------------------------------------------------
# Scenario 4: orphan fill (fill before OrderPlaced is lost)
# ---------------------------------------------------------------------------

def test_orphan_fill_synthesises_stub_order(tmp_path: Path) -> None:
    """If only an OrderFilled event survived, recovery creates a stub order."""
    db = _db(tmp_path)

    s1 = SQLiteEventStore(db)
    # Only the fill is persisted — OrderPlaced was never appended (or was lost)
    s1.append(OrderFilled(fill=_fill("orphan-1", qty="100")))
    s1.close()

    s2 = SQLiteEventStore(db)
    repo = _Repo()
    result = SessionRecovery(s2, repo).recover()
    s2.close()

    assert result.events_replayed == 1
    assert result.orders_recovered == 1
    stub = repo.get("orphan-1")
    assert stub is not None
    assert stub.status == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# Scenario 5: partial fill sequence accumulation
# ---------------------------------------------------------------------------

def test_placed_then_partial_fill_sequence(tmp_path: Path) -> None:
    """OrderPlaced + two partial OrderFilled events recover with 1 unique order."""
    db = _db(tmp_path)

    s1 = SQLiteEventStore(db)
    s1.append(OrderPlaced(order=_order("o-3", status=OrderStatus.NEW)))
    # Partial fill 1
    s1.append(OrderFilled(fill=_fill("o-3", qty="30")))
    # Partial fill 2
    s1.append(OrderFilled(fill=_fill("o-3", qty="70")))
    s1.close()

    s2 = SQLiteEventStore(db)
    repo = _Repo()
    result = SessionRecovery(s2, repo).recover()
    s2.close()

    # 1 OrderPlaced → 1 saved; 2 fills for existing order → no new order stubs
    assert result.events_replayed == 3
    assert result.orders_recovered == 1
    assert repo.get("o-3") is not None


# ---------------------------------------------------------------------------
# Scenario 6: multi-session durability
# ---------------------------------------------------------------------------

def test_prior_session_events_remain_after_new_session_appends(tmp_path: Path) -> None:
    db = _db(tmp_path)

    # Session A
    sA = SQLiteEventStore(db)
    sA.append(OrderPlaced(order=_order("session-A-1")))
    sA.close()

    # Session B appends new events on top
    sB = SQLiteEventStore(db)
    sB.append(OrderPlaced(order=_order("session-B-1")))
    sB.close()

    # Recovery reads both sessions' events
    sC = SQLiteEventStore(db)
    repo = _Repo()
    result = SessionRecovery(sC, repo).recover()
    sC.close()

    assert result.events_replayed == 2
    assert result.orders_recovered == 2
    assert repo.get("session-A-1") is not None
    assert repo.get("session-B-1") is not None


# ---------------------------------------------------------------------------
# Scenario 7: rejection survives restart
# ---------------------------------------------------------------------------

def test_order_rejected_survives_restart(tmp_path: Path) -> None:
    db = _db(tmp_path)

    s1 = SQLiteEventStore(db)
    s1.append(OrderPlaced(order=_order("o-rej")))
    s1.append(OrderRejected(order=_order("o-rej", status=OrderStatus.REJECTED), reason="margin"))
    s1.close()

    s2 = SQLiteEventStore(db)
    repo = _Repo()
    result = SessionRecovery(s2, repo).recover()
    s2.close()

    # Two events carrying .order → both saved; second UPSERT overwrites first
    assert result.events_replayed == 2
    assert result.orders_recovered == 2  # placed + rejected both save
    recovered = repo.get("o-rej")
    assert recovered is not None
    # Final state is whatever the last save carried (rejected order)
    assert recovered.status == OrderStatus.REJECTED


# ---------------------------------------------------------------------------
# Scenario 8: cancel survives restart
# ---------------------------------------------------------------------------

def test_order_cancelled_survives_restart(tmp_path: Path) -> None:
    db = _db(tmp_path)

    s1 = SQLiteEventStore(db)
    s1.append(OrderPlaced(order=_order("o-can")))
    s1.append(OrderCancelled(order=_order("o-can", status=OrderStatus.CANCELLED)))
    s1.close()

    s2 = SQLiteEventStore(db)
    repo = _Repo()
    result = SessionRecovery(s2, repo).recover()
    s2.close()

    assert result.events_replayed == 2
    recovered = repo.get("o-can")
    assert recovered is not None
    assert recovered.status == OrderStatus.CANCELLED


# ---------------------------------------------------------------------------
# Scenario 9: replay sequence ordering is stable
# ---------------------------------------------------------------------------

def test_replay_sequence_order_is_stable_across_restart(tmp_path: Path) -> None:
    db = _db(tmp_path)
    order_ids = [f"o-seq-{i}" for i in range(10)]

    s1 = SQLiteEventStore(db)
    for oid in order_ids:
        s1.append(OrderPlaced(order=_order(oid)))
    s1.close()

    s2 = SQLiteEventStore(db)
    events = list(s2.replay("orders"))
    s2.close()

    replayed_ids = [e.order.order_id.value for e in events]  # type: ignore[union-attr]
    assert replayed_ids == order_ids


# ---------------------------------------------------------------------------
# Scenario 10: last_sequence tracks total events
# ---------------------------------------------------------------------------

def test_recovery_result_last_sequence_equals_events_replayed(tmp_path: Path) -> None:
    db = _db(tmp_path)

    s1 = SQLiteEventStore(db)
    for i in range(5):
        s1.append(OrderPlaced(order=_order(f"o-ls-{i}")))
    s1.close()

    s2 = SQLiteEventStore(db)
    repo = _Repo()
    result = SessionRecovery(s2, repo).recover()
    s2.close()

    assert result.last_sequence == 5
    assert result.events_replayed == 5
    assert result.orders_recovered == 5
