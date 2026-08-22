"""Tests for boot() safety gates — mode validation, live gates, wiring."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from tradex_domain import BrokerId
from tradex_brokers.common.capabilities import dhan_capabilities

from tradex_trading.config.schema import AppConfig
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

    def test_live_boot_wires_stream_backend(self, monkeypatch) -> None:
        from tradex_trading.runtime import live as live_mod

        backend = MagicMock()
        broker = self._fake_broker(backend)
        monkeypatch.setattr(live_mod, "build_broker_from_env", lambda _p, **_kw: broker)

        cfg = AppConfig(mode="live", broker_id=BrokerId.DHAN, live_enabled=True)
        session = boot(cfg)
        try:
            assert session._stream_backend is backend
            assert session.stream._backend is backend
        finally:
            session.stop()

    def test_live_boot_degrades_when_backend_fails(self, monkeypatch) -> None:
        from tradex_trading.runtime import live as live_mod

        broker = self._fake_broker()
        broker.stream_backend.side_effect = RuntimeError("no ws transport")
        monkeypatch.setattr(live_mod, "build_broker_from_env", lambda _p, **_kw: broker)

        cfg = AppConfig(mode="live", broker_id=BrokerId.DHAN, live_enabled=True)
        session = boot(cfg)  # must not raise
        try:
            assert session._stream_backend is None
        finally:
            session.stop()

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

    def test_no_persistence_keeps_no_guard(self) -> None:
        """Default boot stays unchanged: idempotency is opt-in."""
        session = boot(AppConfig(mode="paper"))
        try:
            assert session.engine._guard is None  # noqa: SLF001 – wiring probe
        finally:
            session.stop()
