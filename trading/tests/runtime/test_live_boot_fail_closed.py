"""Live boot must wire fail-closed engine gates and refuse READY without durability."""

from __future__ import annotations

from unittest.mock import MagicMock

import tradex_runtime.startup as startup_mod
from tradex_domain import BrokerId

from tradex_trading.config.schema import AppConfig, PersistenceConfig
from tradex_trading.sdk.session import SessionState


def _fake_live_broker() -> MagicMock:
    broker = MagicMock()
    backend = MagicMock()
    backend.subscribe_orders = MagicMock()
    broker.stream_backend.return_value = backend
    broker.get_orderbook.return_value = []
    broker.get_positions.return_value = []
    broker.master_loader = None
    return broker


def _live_cfg(tmp_path) -> AppConfig:
    return AppConfig(
        mode="live",
        broker_id=BrokerId.DHAN,
        live_enabled=True,
        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
    )


def test_live_boot_wires_fail_closed_engine_flags(monkeypatch, tmp_path) -> None:
    """Live ExecutionEngine must require risk, feed, and durable events."""
    broker = _fake_live_broker()
    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env",
        lambda _pid, **_kw: broker,
    )

    session = startup_mod.boot(_live_cfg(tmp_path))
    try:
        engine = session.engine
        assert engine._require_risk is True  # noqa: SLF001
        assert engine._require_feed is True  # noqa: SLF001
        assert engine._require_durable_events is True  # noqa: SLF001
        assert engine._event_store is not None  # noqa: SLF001
    finally:
        session.stop()


def test_live_boot_without_event_store_refuses_ready(monkeypatch, tmp_path) -> None:
    """Missing durable event store must skip session.start() (fail-closed)."""
    broker = _fake_live_broker()
    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env",
        lambda _pid, **_kw: broker,
    )
    import tradex_runtime.startup as runtime_startup

    real_tail = runtime_startup._boot_tail

    def _tail_without_event_store(*args, **kwargs):
        kwargs["event_store"] = None
        return real_tail(*args, **kwargs)

    monkeypatch.setattr(runtime_startup, "_boot_tail", _tail_without_event_store)

    session = startup_mod.boot(_live_cfg(tmp_path))
    try:
        assert session.state != SessionState.READY
        assert session.engine.kill_switch is True
    finally:
        session.stop()


def test_paper_boot_does_not_require_fail_closed_flags() -> None:
    """Paper mode must not enable live-only engine gates."""
    session = startup_mod.boot(AppConfig(mode="paper"))
    try:
        engine = session.engine
        assert engine._require_risk is False  # noqa: SLF001
        assert engine._require_feed is False  # noqa: SLF001
        assert engine._require_durable_events is False  # noqa: SLF001
        assert session.state == SessionState.READY
    finally:
        session.stop()
