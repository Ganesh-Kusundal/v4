"""Integration tests for SQLiteEventStore — real SQLite files, no mocks.

All temp databases are created with ``tmp_path`` (pytest built-in) and
cleaned up automatically after each test.  The store is exercised end-to-end:
append → close → reopen → replay, ensuring WAL durability across "crashes".
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import (
    OrderCancelled,
    OrderFilled,
    OrderModified,
    OrderPlaced,
    OrderRejected,
    PlaceOrderCommand,
)
from tradex_domain.execution import Fill, Order, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

from tradex_trading.execution.sqlite_event_store import SQLiteEventStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _order(order_id: str = "o-1", status: OrderStatus = OrderStatus.NEW) -> Order:
    return Order(
        order_id=OrderId(order_id),
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        status=status,
    )


def _fill(order_id: str = "o-1", qty: str = "10") -> Fill:
    return Fill(
        order_id=OrderId(order_id),
        instrument=_eq(),
        side=OrderSide.BUY,
        quantity=Quantity(Decimal(qty)),
        price=Price(Decimal("2500")),
    )


def _store(path: Path) -> SQLiteEventStore:
    return SQLiteEventStore(path / "events.db")


# ---------------------------------------------------------------------------
# Basic append + replay
# ---------------------------------------------------------------------------

def test_append_returns_positive_sequence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    seq = store.append(OrderPlaced(order=_order()))
    assert seq >= 1
    store.close()


def test_replay_returns_appended_events_in_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(OrderPlaced(order=_order("o-1")))
    store.append(OrderPlaced(order=_order("o-2")))
    store.append(OrderPlaced(order=_order("o-3")))

    events = list(store.replay("orders"))
    assert len(events) == 3
    order_ids = [e.order.order_id.value for e in events]  # type: ignore[union-attr]
    assert order_ids == ["o-1", "o-2", "o-3"]
    store.close()


def test_all_order_event_types_are_storable(tmp_path: Path) -> None:
    """Every OMS event type round-trips through pickle without error."""
    store = _store(tmp_path)
    order = _order()
    fill = _fill()

    store.append(OrderPlaced(order=order))
    store.append(OrderFilled(fill=fill))
    store.append(OrderRejected(order=order, reason="price out of range"))
    store.append(OrderCancelled(order=order))
    store.append(OrderModified(order=order))
    store.append(PlaceOrderCommand(request=OrderRequest(
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal("5")),
    )))

    events = list(store.replay("orders"))
    assert len(events) == 6
    types = [type(e).__name__ for e in events]
    assert types == [
        "OrderPlaced", "OrderFilled", "OrderRejected",
        "OrderCancelled", "OrderModified", "PlaceOrderCommand",
    ]
    store.close()


# ---------------------------------------------------------------------------
# Durability: survive "process restart" (close + reopen)
# ---------------------------------------------------------------------------

def test_events_survive_store_close_and_reopen(tmp_path: Path) -> None:
    """WAL mode: events written before close are visible after reopen."""
    db = tmp_path / "events.db"

    # Session 1 — write and close
    s1 = SQLiteEventStore(db)
    s1.append(OrderPlaced(order=_order("o-10")))
    s1.append(OrderFilled(fill=_fill("o-10")))
    s1.close()

    # Session 2 — reopen (simulates process restart)
    s2 = SQLiteEventStore(db)
    events = list(s2.replay("orders"))
    s2.close()

    assert len(events) == 2
    assert isinstance(events[0], OrderPlaced)
    assert isinstance(events[1], OrderFilled)
    assert events[0].order.order_id.value == "o-10"  # type: ignore[union-attr]


def test_sequence_is_monotonically_increasing_across_sessions(tmp_path: Path) -> None:
    db = tmp_path / "events.db"

    s1 = SQLiteEventStore(db)
    seq1 = s1.append(OrderPlaced(order=_order("o-1")))
    s1.close()

    s2 = SQLiteEventStore(db)
    seq2 = s2.append(OrderPlaced(order=_order("o-2")))
    s2.close()

    assert seq2 > seq1


# ---------------------------------------------------------------------------
# Stream isolation
# ---------------------------------------------------------------------------

def test_replay_filters_by_stream(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(OrderPlaced(order=_order("o-1")))                # → "orders"
    store.append(OrderRejected(order=_order("o-2"), reason="x"))  # → "orders"
    # Manually insert a "system" event by specifying stream_id
    store.append(OrderPlaced(order=_order("o-sys")), stream_id="system")

    orders_stream = list(store.replay("orders"))
    system_stream = list(store.replay("system"))
    all_stream = list(store.replay("*"))

    assert len(orders_stream) == 2
    assert len(system_stream) == 1
    assert len(all_stream) == 3
    store.close()


# ---------------------------------------------------------------------------
# count() and last_sequence()
# ---------------------------------------------------------------------------

def test_count_and_last_sequence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.count() == 0
    assert store.last_sequence() == 0

    seq1 = store.append(OrderPlaced(order=_order("o-1")))
    seq2 = store.append(OrderPlaced(order=_order("o-2")))

    assert store.count() == 2
    assert store.last_sequence() == seq2
    store.close()


# ---------------------------------------------------------------------------
# Correlation id is persisted
# ---------------------------------------------------------------------------

def test_correlation_id_round_trips(tmp_path: Path) -> None:
    corr = CorrelationId("test-corr-42")
    order = Order(
        order_id=OrderId("o-corr"),
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("1")),
        price=Price(Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.NEW,
        correlation_id=corr,
    )
    store = _store(tmp_path)
    store.append(OrderPlaced(order=order, correlation_id=corr))

    events = list(store.replay("orders"))
    assert len(events) == 1
    assert events[0].correlation_id is not None
    assert str(events[0].correlation_id.value) == "test-corr-42"
    store.close()


# ---------------------------------------------------------------------------
# In-memory variant (protocol smoke test)
# ---------------------------------------------------------------------------

def test_in_memory_store_satisfies_protocol() -> None:
    """`:memory:` path gives a protocol-compliant ephemeral store."""
    from tradex_trading.execution.recovery import EventStore
    store = SQLiteEventStore(":memory:")
    assert isinstance(store, EventStore)
    seq = store.append(OrderPlaced(order=_order()))
    assert seq >= 1
    events = list(store.replay())
    assert len(events) == 1
    store.close()


# ---------------------------------------------------------------------------
# Concurrent appends (thread safety)
# ---------------------------------------------------------------------------

def test_concurrent_appends_are_thread_safe(tmp_path: Path) -> None:
    import threading

    store = _store(tmp_path)
    errors: list[Exception] = []

    def _append(n: int) -> None:
        try:
            for i in range(n):
                store.append(OrderPlaced(order=_order(f"o-t-{threading.get_ident()}-{i}")))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_append, args=(10,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Thread errors: {errors}"
    assert store.count() == 40
    store.close()
