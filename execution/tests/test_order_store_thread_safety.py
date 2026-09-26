"""The order store must survive being written from the thread that owns the feed.

Defect (High, M-7): ``SQLiteOrderStore`` was the only one of the three SQLite
connections opened without ``check_same_thread=False`` -- ``SQLiteEventStore``
and ``SQLiteIdempotencyGuard`` both set it. The order store is written from the
broker feed thread (and by ``LiveFillBridge``), not the thread that built it, so
every such write raised ``sqlite3.ProgrammingError``.

That exception was swallowed by the ``except Exception: log`` wrapper around the
persistence writes, so the ``orders`` table silently stopped being written while
``attach_order_persistence`` still reported success. The event log was then the
only durable order record, with nothing to say the write had failed.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderType, OrderStatus, TimeInForce
from tradex_domain.execution import Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.sqlite_store import SQLiteOrderStore

_INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _order() -> Order:
    return Order(
        order_id=OrderId("order-x"),
        instrument=_INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal("10")),
        filled_quantity=Quantity(Decimal("0")),
        price=Price(Decimal("2500")),
        status=OrderStatus.NEW,
        time_in_force=TimeInForce.DAY,
    )


def test_write_from_another_thread_is_persisted(tmp_path) -> None:
    """A fill published from the feed thread must reach the orders table."""
    store = SQLiteOrderStore(tmp_path / "orders.db")

    error: list[BaseException] = []

    def publish() -> None:
        try:
            store.upsert(_order())
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted
            error.append(exc)

    thread = threading.Thread(target=publish, name="feed")
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "writer thread hung"

    assert not error, (
        "writing an order from the feed thread raised, so the orders table "
        f"silently stops being written: {error[0]!r}"
    )

    rows = store.all_orders()
    assert len(rows) == 1, f"order was not persisted from the feed thread: {rows!r}"


def test_connection_allows_cross_thread_use(tmp_path) -> None:
    """The connection must be opened for cross-thread use, like its peers."""
    store = SQLiteOrderStore(tmp_path / "orders.db")
    conn = store._conn  # noqa: SLF001 - the flag lives on the connection
    # sqlite3 exposes no public flag; a cross-thread statement is the observable.
    result: list[str] = []

    def touch() -> None:
        result.append(str(conn.execute("SELECT 1").fetchone()[0]))

    thread = threading.Thread(target=touch)
    thread.start()
    thread.join(timeout=10)
    assert result == ["1"], "connection rejected a statement from another thread"
    assert not isinstance(
        getattr(conn, "_check_same_thread", None), bool
    ) or not getattr(conn, "_check_same_thread", True)


def test_persistence_failure_is_counted_not_only_logged(tmp_path) -> None:
    """A store that stops accepting writes must be visible without log scraping.

    attach_order_persistence swallows persistence failures on purpose so they
    cannot break trading, and still returns a subscription count -- so a dead
    store used to look exactly like a healthy one.
    """
    from tradex_execution.sqlite_store import attach_order_persistence
    from tradex_observability.metrics import MetricsRegistry
    from tradex_reactive.bus import ReactiveBus
    from tradex_execution.trading_cache import TradingCache
    from tradex_domain.events import OrderPlaced

    class _BrokenStore:
        def upsert(self, order: Order) -> None:
            raise RuntimeError("disk gone")

    cache = TradingCache()
    cache.update_order(_order())
    metrics = MetricsRegistry()
    bus = ReactiveBus()
    attach_order_persistence(bus, cache, _BrokenStore(), metrics=metrics)

    bus.publish(OrderPlaced(order=_order()))
    # The bus swallows subscriber exceptions, so assert on the counter.
    value = metrics.get("orders.persistence_failures.mirror")
    assert value is not None and value >= 1, (
        "a failed order write must increment a counter; a dead store that "
        "reports success is indistinguishable from a healthy one"
    )
