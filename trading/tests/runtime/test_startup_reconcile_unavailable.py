"""Startup reconciliation must fail closed when broker book+positions are unavailable."""

from __future__ import annotations

from unittest.mock import MagicMock

import tradex_runtime.startup as startup_mod
from tradex_domain import BrokerId
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_reactive.bus import ReactiveBus

from tradex_trading.config.schema import AppConfig, PersistenceConfig
from tradex_trading.sdk.session import SessionState


def test_run_startup_reconciliation_unavailable_trips_kill_switch() -> None:
    broker = MagicMock()
    broker.get_orderbook.side_effect = RuntimeError("venue down")
    broker.get_positions.side_effect = RuntimeError("venue down")
    engine = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=SimulatedFillSource(),
    )

    tripped = startup_mod._run_startup_reconciliation(broker, engine)

    assert tripped is True
    assert engine.kill_switch is True


def test_recon_unavailable_refuses_ready_even_if_kill_switch_already_on() -> None:
    """Pre-tripped kill_switch_default must not mask RECON_UNAVAILABLE."""
    broker = MagicMock()
    broker.get_orderbook.side_effect = RuntimeError("venue down")
    broker.get_positions.side_effect = RuntimeError("venue down")
    engine = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=SimulatedFillSource(),
    )
    engine.trip_kill_switch(reason="configured_default")

    tripped = startup_mod._run_startup_reconciliation(broker, engine)

    assert tripped is True


def test_live_boot_reconcile_unavailable_refuses_ready(monkeypatch, tmp_path) -> None:
    broker = MagicMock()
    backend = MagicMock()
    backend.subscribe_orders = MagicMock()
    broker.stream_backend.return_value = backend
    broker.get_orderbook.side_effect = RuntimeError("no book")
    broker.get_positions.side_effect = RuntimeError("no positions")
    broker.master_loader = None

    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env",
        lambda _pid, **_kw: broker,
    )

    cfg = AppConfig(
        mode="live",
        broker_id=BrokerId.DHAN,
        live_enabled=True,
        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
    )
    session = startup_mod.boot(cfg)
    try:
        assert session.state != SessionState.READY
        assert session.engine.kill_switch is True
    finally:
        session.stop()
