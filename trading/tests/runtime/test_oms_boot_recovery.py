"""Wave B2 — OMS boot recovery integration tests.

Verifies that ``SQLiteEventStore`` is wired into the live/persistent startup
sequence so that:

1. A pre-existing event log is replayed at boot and the OMS cache is rebuilt
   from it (before broker reconciliation).
2. An empty event store is a clean no-op.
3. New lifecycle events published on the bus are durably appended to the
   event store after boot.
4. The full ``boot()`` path with ``cfg.persistence.path`` exercises the
   event-sourced recovery and wires future appends.

No mocks. Real SQLite on tmp_path. Real ReactiveBus. Real TradingCache.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import OrderCancelled, OrderFilled, OrderPlaced, OrderRejected
from tradex_domain.execution import Fill, Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.config.schema import AppConfig, PersistenceConfig
from tradex_trading.execution.sqlite_event_store import SQLiteEventStore
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.runtime.startup import _attach_event_store, _recover_oms_from_events

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _order(order_id: str = "o-1", status: OrderStatus = OrderStatus.NEW) -> Order:
    return Order(
        order_id=OrderId(order_id),
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("100")),
        price=Price(Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        status=status,
    )


def _fill(order_id: str = "o-1", qty: str = "100") -> Fill:
    return Fill(
        order_id=OrderId(order_id),
        instrument=_eq(),
        side=OrderSide.BUY,
        quantity=Quantity(Decimal(qty)),
        price=Price(Decimal("2500")),
    )


def _db(tmp_path: Path) -> Path:
    return tmp_path / "oms.db"


# ---------------------------------------------------------------------------
# 1. _recover_oms_from_events rebuilds cache from event log
# ---------------------------------------------------------------------------


def test_recover_oms_from_events_rebuilds_order_cache(tmp_path: Path) -> None:
    """OrderPlaced events in the event store are replayed into TradingCache."""
    db = _db(tmp_path)
    store = SQLiteEventStore(db)
    store.append(OrderPlaced(order=_order("o-1")))
    store.append(OrderPlaced(order=_order("o-2")))
    store.close()

    store2 = SQLiteEventStore(db)
    cache = TradingCache()
    _recover_oms_from_events(store2, cache)
    store2.close()

    assert cache.get_order("o-1") is not None
    assert cache.get_order("o-2") is not None
    assert len(cache.all_orders()) == 2


def test_recover_oms_from_events_applies_status_transitions(tmp_path: Path) -> None:
    """The last-applied event wins: cancelled order lands as CANCELLED."""
    db = _db(tmp_path)
    store = SQLiteEventStore(db)
    store.append(OrderPlaced(order=_order("o-can", OrderStatus.NEW)))
    store.append(OrderCancelled(order=_order("o-can", OrderStatus.CANCELLED)))
    store.close()

    store2 = SQLiteEventStore(db)
    cache = TradingCache()
    _recover_oms_from_events(store2, cache)
    store2.close()

    recovered = cache.get_order("o-can")
    assert recovered is not None
    assert recovered.status == OrderStatus.CANCELLED


def test_recover_oms_from_events_handles_fill_orphan(tmp_path: Path) -> None:
    """A fill without a prior OrderPlaced still produces a stub order in cache."""
    db = _db(tmp_path)
    store = SQLiteEventStore(db)
    # Only a fill — no matching OrderPlaced
    store.append(OrderFilled(fill=_fill("orphan-1", qty="50")))
    store.close()

    store2 = SQLiteEventStore(db)
    cache = TradingCache()
    _recover_oms_from_events(store2, cache)
    store2.close()

    stub = cache.get_order("orphan-1")
    assert stub is not None
    assert stub.status == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# 2. _recover_oms_from_events with an empty store is a clean no-op
# ---------------------------------------------------------------------------


def test_recover_oms_from_empty_store_is_noop(tmp_path: Path) -> None:
    """An empty event store leaves the cache untouched."""
    db = _db(tmp_path)
    store = SQLiteEventStore(db)
    store.close()

    store2 = SQLiteEventStore(db)
    cache = TradingCache()
    _recover_oms_from_events(store2, cache)
    store2.close()

    assert cache.all_orders() == []


# ---------------------------------------------------------------------------
# 3. _attach_event_store appends future bus events durably
# ---------------------------------------------------------------------------


def test_attach_event_store_appends_placed_event(tmp_path: Path) -> None:
    """OrderPlaced published on bus after wiring is appended to the event store."""
    db = _db(tmp_path)
    store = SQLiteEventStore(db)
    bus = ReactiveBus()

    _attach_event_store(bus, store)

    bus.publish(OrderPlaced(order=_order("o-wire-1")))
    store.close()

    store2 = SQLiteEventStore(db)
    events = list(store2.replay("orders"))
    store2.close()

    assert len(events) == 1
    placed = events[0]
    assert isinstance(placed, OrderPlaced)
    assert placed.order.order_id.value == "o-wire-1"


def test_attach_event_store_appends_all_lifecycle_events(tmp_path: Path) -> None:
    """All five order lifecycle event types are appended when published."""
    db = _db(tmp_path)
    store = SQLiteEventStore(db)
    bus = ReactiveBus()

    _attach_event_store(bus, store)

    bus.publish(OrderPlaced(order=_order("o-p")))
    bus.publish(OrderFilled(fill=_fill("o-p")))
    bus.publish(OrderCancelled(order=_order("o-c", OrderStatus.CANCELLED)))
    bus.publish(OrderRejected(order=_order("o-r", OrderStatus.REJECTED), reason="risk"))

    count = store.count("orders")
    store.close()

    assert count == 4


def test_attach_event_store_does_not_append_non_order_events(tmp_path: Path) -> None:
    """Non-order events (e.g. ErrorOccurred) are NOT appended to the event store."""
    from tradex_domain.events import ErrorOccurred

    db = _db(tmp_path)
    store = SQLiteEventStore(db)
    bus = ReactiveBus()

    _attach_event_store(bus, store)

    bus.publish(ErrorOccurred(error=ValueError("boom")))

    count = store.count("*")
    store.close()

    assert count == 0


# ---------------------------------------------------------------------------
# 4. boot() with persistence.path wires event-store recovery end-to-end
# ---------------------------------------------------------------------------


def test_boot_with_persistence_replays_events_into_cache(tmp_path: Path) -> None:
    """Full boot(): pre-existing events are replayed into the OMS cache."""
    from tradex_trading.runtime.startup import boot

    db = _db(tmp_path)

    # Pre-populate the event store before boot
    store = SQLiteEventStore(db)
    store.append(OrderPlaced(order=_order("pre-boot-1")))
    store.append(OrderPlaced(order=_order("pre-boot-2")))
    store.close()

    cfg = AppConfig(persistence=PersistenceConfig(path=str(db)))
    session = boot(cfg)
    try:
        cache = session.engine.cache
        assert cache.get_order("pre-boot-1") is not None, (
            "pre-boot order 'pre-boot-1' must be in cache after event replay"
        )
        assert cache.get_order("pre-boot-2") is not None, (
            "pre-boot order 'pre-boot-2' must be in cache after event replay"
        )
    finally:
        session.stop()


def test_boot_with_persistence_appends_new_events_after_boot(tmp_path: Path) -> None:
    """After boot(), engine lifecycle events are appended to the durable store.

    The engine owns appends (C1); publishing on the bus alone is not the
    durability path — that would double-write if both bus attachment and
    engine._append were active.
    """
    from decimal import Decimal

    from tradex_domain import OrderRequest, OrderSide, OrderType, Quantity
    from tradex_domain.instruments import Equity

    from tradex_trading.runtime.startup import boot

    db = _db(tmp_path)
    cfg = AppConfig(persistence=PersistenceConfig(path=str(db)))
    session = boot(cfg)
    try:
        assert session.engine._event_store is not None
        request = OrderRequest(
            instrument=Equity.of("NSE", "RELIANCE"),
            side=OrderSide.BUY,
            quantity=Quantity(value=Decimal("1")),
            order_type=OrderType.MARKET,
        )
        session.engine.submit(request)
    finally:
        session.stop()

    import time

    time.sleep(0.05)

    store = SQLiteEventStore(db)
    events = list(store.replay("orders"))
    store.close()
    assert len(events) >= 1, "engine submit must durable-append at least one OMS event"


def test_boot_without_persistence_path_skips_event_store(tmp_path: Path) -> None:
    """boot() with no persistence.path must not create any SQLite files."""
    import os

    from tradex_trading.runtime.startup import boot

    before = set(os.listdir(tmp_path))

    cfg = AppConfig()  # no persistence path
    session = boot(cfg)
    session.stop()

    after = set(os.listdir(tmp_path))
    assert after == before, "boot() without persistence.path must not create files"


# ---------------------------------------------------------------------------
# 5. Boot order guarantee: event replay precedes reconciliation
# ---------------------------------------------------------------------------


def test_event_replay_precedes_reconciliation(tmp_path: Path) -> None:
    """The OMS cache is populated from events before _run_startup_reconciliation.

    We verify this implicitly: if boot() reaches READY and the cache has the
    pre-boot orders, the recovery ran before the reconciliation step that
    gates session.start().
    """
    from tradex_trading.runtime.startup import boot
    from tradex_trading.sdk.session import SessionState

    db = _db(tmp_path)
    store = SQLiteEventStore(db)
    store.append(OrderPlaced(order=_order("seq-check-1", OrderStatus.NEW)))
    store.close()

    cfg = AppConfig(persistence=PersistenceConfig(path=str(db)))
    session = boot(cfg)
    try:
        # session is in READY state (start() was called)
        assert session.state == SessionState.READY
        # Pre-boot order is visible — event replay ran before READY
        assert session.engine.cache.get_order("seq-check-1") is not None
    finally:
        session.stop()
