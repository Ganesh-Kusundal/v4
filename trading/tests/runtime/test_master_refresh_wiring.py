"""TradingSession.live() starts a daily master-refresh scheduler when the
broker carries a cached MasterLoader, and session.stop() tears it down
(v3 parity - the daemon re-downloads the master so a long-running session
picks up new option series past monthly expiry).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tradex_domain import BrokerId
from tradex_trading.config.schema import AppConfig, PersistenceConfig

from tradex_trading.runtime.master_lifecycle import MasterLoader
from tradex_trading.sdk.session import TradingSession


class _BrokerWithMasterLoader:
    """Minimal broker shape that satisfies TradingSession.live() and
    carries a real cached master loader + refresh hook."""

    def __init__(self) -> None:
        self.master_loader = MasterLoader(lambda: b"", lambda _raw: [], cache=None)
        self.refreshes = 0

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def stream_backend(self) -> MagicMock:
        backend = MagicMock()
        backend.subscribe_orders = MagicMock()
        backend.close = MagicMock()
        return backend

    def ensure_master_fresh(self, *, force_refresh: bool = False) -> None:
        self.master_loader.load(force_refresh=force_refresh)
        self.refreshes += 1


class _PlainBroker:
    """Live-compatible broker with no master loader (no scheduler expected)."""

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def stream_backend(self) -> MagicMock:
        backend = MagicMock()
        backend.subscribe_orders = MagicMock()
        backend.close = MagicMock()
        return backend


def test_live_starts_master_refresh_scheduler(monkeypatch, tmp_path) -> None:
    from tradex_trading import runtime
    import tradex_trading.sdk.session as session_mod

    broker = _BrokerWithMasterLoader()
    monkeypatch.setattr(runtime.live, "build_broker_from_env", lambda _provider: broker)
    monkeypatch.setattr(
        session_mod,
        "AppConfig",
        lambda **kwargs: AppConfig(
            **kwargs, persistence=PersistenceConfig(path=str(tmp_path / "orders.db"))
        ),
    )

    session = TradingSession.live(BrokerId.DHAN, confirm=True)
    try:
        assert session._master_scheduler is not None
        assert session._master_scheduler.is_running
        # A manual tick drives the broker hook through the cached loader.
        assert session._master_scheduler.refresh_now() is True
        assert broker.refreshes == 1
    finally:
        session.stop()
    # Teardown: the daemon is stopped and released on session.stop().
    assert session._master_scheduler is None


def test_live_without_loader_starts_no_scheduler(monkeypatch, tmp_path) -> None:
    from tradex_trading import runtime
    import tradex_trading.sdk.session as session_mod

    broker = _PlainBroker()
    monkeypatch.setattr(runtime.live, "build_broker_from_env", lambda _provider: broker)
    monkeypatch.setattr(
        session_mod,
        "AppConfig",
        lambda **kwargs: AppConfig(
            **kwargs, persistence=PersistenceConfig(path=str(tmp_path / "orders.db"))
        ),
    )

    session = TradingSession.live(BrokerId.DHAN, confirm=True)
    try:
        assert session._master_scheduler is None
    finally:
        session.stop()
