"""Tests for boot() safety gates — mode validation, live gates, wiring."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from tradex_brokers.common.capabilities import dhan_capabilities
from tradex_domain import BrokerId

from tradex_trading.config.schema import AppConfig
from tradex_trading.config.schema import PersistenceConfig
from tradex_trading.runtime.startup import boot


class TestBootModeValidation:
    """boot() validates the mode against an allowlist."""

    def test_paper_mode_boots(self) -> None:
        cfg = AppConfig(mode="paper")
        session = boot(cfg)
        assert session.state.value == "READY"
        session.stop()

    def test_backtest_mode_boots(self) -> None:
        cfg = AppConfig(mode="backtest")
        session = boot(cfg)
        assert session.state.value == "READY"
        session.stop()

    def test_unknown_mode_raises(self) -> None:
        cfg = AppConfig(mode="unknown_mode")
        with pytest.raises(ValueError, match="unknown mode"):
            boot(cfg)

    def test_empty_mode_raises(self) -> None:
        cfg = AppConfig(mode="")
        with pytest.raises(ValueError, match="unknown mode"):
            boot(cfg)


class TestAppConfigBrokerIdValidation:
    """PE-14: AppConfig.from_dict rejects invalid broker_id instead of
    silently defaulting to PAPER."""

    def test_invalid_broker_id_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid broker_id.*DHA"):
            AppConfig.from_dict({"broker_id": "DHA"})

    def test_valid_broker_id_accepted(self) -> None:
        cfg = AppConfig.from_dict({"broker_id": "DHAN"})
        assert cfg.broker_id == BrokerId.DHAN

    def test_default_broker_id_is_paper(self) -> None:
        cfg = AppConfig.from_dict({})
        assert cfg.broker_id == BrokerId.PAPER


class TestBootLiveGates:
    """boot() enforces live-mode safety gates."""

    def test_live_with_paper_broker_raises(self) -> None:
        cfg = AppConfig(mode="live", broker_id=BrokerId.PAPER, live_enabled=True)
        with pytest.raises(ValueError, match="live mode requires a non-paper broker"):
            boot(cfg)

    def test_live_without_live_enabled_raises(self) -> None:
        cfg = AppConfig(mode="live", broker_id=BrokerId.DHAN, live_enabled=False)
        with pytest.raises(ValueError, match="live mode requires live_enabled"):
            boot(cfg)


class TestBootSessionMode:
    """boot() stores mode on the session."""

    def test_session_has_mode(self) -> None:
        cfg = AppConfig(mode="paper")
        session = boot(cfg)
        assert session.mode == "paper"
        session.stop()

    def test_backtest_session_mode(self) -> None:
        cfg = AppConfig(mode="backtest")
        session = boot(cfg)
        assert session.mode == "backtest"
        session.stop()


class TestBootStreamBackendWiring:
    """boot() wires the broker order/portfolio stream backend into the session."""

    def _fake_broker(self, backend: MagicMock | None = None) -> MagicMock:
        broker = MagicMock()
        broker.capabilities = dhan_capabilities()
        broker.stream_backend.return_value = backend or MagicMock()
        return broker

    def test_live_boot_wires_stream_backend(self, monkeypatch, tmp_path) -> None:
        from tradex_trading.runtime import live as live_mod

        backend = MagicMock()
        broker = self._fake_broker(backend)
        monkeypatch.setattr(live_mod, "build_broker_from_env", lambda _p, **_kw: broker)

        cfg = AppConfig(mode="live", broker_id=BrokerId.DHAN, live_enabled=True,
                        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")))
        session = boot(cfg)
        try:
            assert session._stream_backend is backend
            assert session._stream_backend is backend
        finally:
            session.stop()

    def test_live_boot_refuses_when_order_stream_backend_fails(self, monkeypatch, tmp_path) -> None:
        from tradex_trading.runtime import live as live_mod

        broker = self._fake_broker()
        broker.stream_backend.side_effect = RuntimeError("no ws transport")
        monkeypatch.setattr(live_mod, "build_broker_from_env", lambda _p, **_kw: broker)

        cfg = AppConfig(mode="live", broker_id=BrokerId.DHAN, live_enabled=True,
                        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")))
        with pytest.raises(RuntimeError, match="order-stream backend"):
            boot(cfg)

    def test_live_boot_refuses_without_order_stream_backend(self, monkeypatch, tmp_path) -> None:
        from tradex_trading.runtime import live as live_mod

        broker = self._fake_broker()
        broker.stream_backend.return_value = None
        monkeypatch.setattr(live_mod, "build_broker_from_env", lambda _p, **_kw: broker)

        cfg = AppConfig(mode="live", broker_id=BrokerId.DHAN, live_enabled=True,
                        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")))
        with pytest.raises(ValueError, match="order-stream backend"):
            boot(cfg)

    def test_paper_boot_has_no_stream_backend(self) -> None:
        cfg = AppConfig(mode="paper")
        session = boot(cfg)
        try:
            assert session._stream_backend is None
        finally:
            session.stop()


class TestBootPersistenceWiring:
    """boot() wires the durable idempotency guard when persistence is set."""

    def test_persistence_path_wires_sqlite_guard(self, tmp_path) -> None:
        from tradex_trading.config.schema import PersistenceConfig
        from tradex_trading.execution.sqlite_store import SQLiteIdempotencyGuard

        cfg = AppConfig(
            mode="paper",
            persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
        )
        session = boot(cfg)
        try:
            guard = session.engine._guard  # noqa: SLF001 – wiring probe
            assert isinstance(guard, SQLiteIdempotencyGuard)
        finally:
            session.stop()

    def test_default_boot_wires_in_memory_idempotency_guard(self) -> None:
        """Every order endpoint has a guard, even without SQLite durability."""
        from tradex_trading.execution.idempotency import MemoryIdempotencyGuard

        session = boot(AppConfig(mode="paper"))
        try:
            assert isinstance(session.engine._guard, MemoryIdempotencyGuard)  # noqa: SLF001
        finally:
            session.stop()

    def test_live_boot_requires_persistence_path(self, monkeypatch, tmp_path) -> None:
        from tradex_trading.runtime import live as live_mod

        broker = MagicMock()
        broker.capabilities = dhan_capabilities()
        backend = MagicMock()
        broker.stream_backend.return_value = backend
        monkeypatch.setattr(live_mod, "build_broker_from_env", lambda _p, **_kw: broker)

        cfg = AppConfig(mode="live", broker_id=BrokerId.DHAN, live_enabled=True)
        with pytest.raises(ValueError, match="persistence"):
            boot(cfg)
