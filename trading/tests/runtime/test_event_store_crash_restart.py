"""C3 — crash-restart recovery via durable event store + engine replay.

Simulates process death after a filled order: events are on disk, the in-memory
cache is empty on restart, and ``_recover_oms_from_events`` (boot path) must
restore orders and positions before trading resumes.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from tradex_domain import OrderRequest, OrderSide, OrderType, Quantity
from tradex_domain.enums import OrderStatus
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_execution.recovery import recover_trading_cache
from tradex_execution.sqlite_event_store import SQLiteEventStore
from tradex_execution.trading_cache import TradingCache
from tradex_reactive.bus import ReactiveBus
from tradex_runtime.startup import _recover_oms_from_events


def _market_buy() -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
    )


def _run_session(db: Path) -> tuple[str, Decimal]:
    """Submit one immediate fill; return order id and expected position qty."""
    store = SQLiteEventStore(db)
    bus = ReactiveBus()
    cache = TradingCache()
    engine = ExecutionEngine(
        bus,
        SimulatedFillSource(),
        cache=cache,
        event_store=store,
    )
    try:
        receipt = engine.submit(_market_buy())
        assert receipt is not None
        order_id = receipt.order_id.value
        pos = cache.get_position(Equity.of("NSE", "RELIANCE"))
        assert pos is not None
        assert pos.quantity.value == Decimal("10")
        assert len(cache.all_orders()) == 1
        assert cache.all_orders()[0].status == OrderStatus.FILLED
        return order_id, pos.quantity.value
    finally:
        engine.shutdown()
        store.close()


def test_crash_restart_restores_order_and_position_via_boot_recovery(
    tmp_path: Path,
) -> None:
    """Drop engine; empty cache; boot recovery path rebuilds OMS state."""
    db = tmp_path / "oms-events.db"
    order_id, expected_qty = _run_session(db)

    store = SQLiteEventStore(db)
    cache = TradingCache()
    _recover_oms_from_events(store, cache)
    store.close()

    recovered = cache.get_order(order_id)
    assert recovered is not None
    assert recovered.status == OrderStatus.FILLED
    assert recovered.filled_quantity.value == Decimal("10")

    pos = cache.get_position(Equity.of("NSE", "RELIANCE"))
    assert pos is not None
    assert pos.quantity.value == expected_qty


def test_crash_restart_new_engine_after_session_recovery(tmp_path: Path) -> None:
    """SessionRecovery + fill replay on a fresh engine cache (direct API)."""
    db = tmp_path / "oms-events.db"
    order_id, expected_qty = _run_session(db)

    store = SQLiteEventStore(db)
    cache = TradingCache()
    result = recover_trading_cache(store, cache)
    store.close()

    assert result.events_replayed >= 2
    assert result.orders_recovered >= 1

    # Second engine shares the recovered cache — no duplicate submit.
    engine2 = ExecutionEngine(
        ReactiveBus(),
        SimulatedFillSource(),
        cache=cache,
        event_store=SQLiteEventStore(db),
    )
    try:
        assert engine2.get_order(OrderId(order_id)) is not None
        pos = engine2.cache.get_position(Equity.of("NSE", "RELIANCE"))
        assert pos is not None
        assert pos.quantity.value == expected_qty
    finally:
        engine2.shutdown()


def _fake_live_broker() -> MagicMock:
    broker = MagicMock()
    backend = MagicMock()
    backend.subscribe_orders = MagicMock()
    broker.stream_backend.return_value = backend
    broker.get_orderbook.return_value = []
    broker.get_positions.return_value = []
    broker.master_loader = None
    return broker


def test_live_boot_ready_after_recover_when_broker_matches(
    monkeypatch, tmp_path: Path,
) -> None:
    """Recover event store then reach READY when broker book matches local."""
    import tradex_runtime.startup as startup_mod
    from tradex_domain import BrokerId

    from tradex_trading.config.schema import AppConfig, PersistenceConfig
    from tradex_trading.sdk.session import SessionState

    db = tmp_path / "orders.db"
    order_id, expected_qty = _run_session(db)

    probe = TradingCache()
    store = SQLiteEventStore(db)
    _recover_oms_from_events(store, probe)
    store.close()

    broker = _fake_live_broker()
    broker.get_orderbook.return_value = list(probe.all_orders())
    broker.get_positions.return_value = list(probe.all_positions())
    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env",
        lambda _pid, **_kw: broker,
    )

    session = startup_mod.boot(
        AppConfig(
            mode="live",
            broker_id=BrokerId.DHAN,
            live_enabled=True,
            persistence=PersistenceConfig(path=str(db)),
        )
    )
    try:
        assert session.state == SessionState.READY
        recovered = session.engine.cache.get_order(order_id)
        assert recovered is not None
        pos = session.engine.cache.get_position(Equity.of("NSE", "RELIANCE"))
        assert pos is not None
        assert pos.quantity.value == expected_qty
    finally:
        session.stop()


def test_live_boot_refuses_ready_after_recover_on_high_recon_drift(
    monkeypatch, tmp_path: Path,
) -> None:
    """Recovered local orders with empty broker book → HIGH drift → not READY."""
    import tradex_runtime.startup as startup_mod
    from tradex_domain import BrokerId

    from tradex_trading.config.schema import AppConfig, PersistenceConfig
    from tradex_trading.sdk.session import SessionState

    db = tmp_path / "orders.db"
    _run_session(db)

    broker = _fake_live_broker()
    broker.get_orderbook.return_value = []
    broker.get_positions.return_value = []
    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env",
        lambda _pid, **_kw: broker,
    )

    session = startup_mod.boot(
        AppConfig(
            mode="live",
            broker_id=BrokerId.DHAN,
            live_enabled=True,
            persistence=PersistenceConfig(path=str(db)),
        )
    )
    try:
        assert session.state != SessionState.READY
        assert session.engine.kill_switch is True
    finally:
        session.stop()
