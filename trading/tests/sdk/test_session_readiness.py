"""Repo-wide readiness sweep: every TradingSession factory returns READY.

Guards the class of bug where a factory returns a NEW session whose services
all raise ``SessionStateError`` (the ``TradingSession.live()`` bug, and the
same latent issue in ``paper()`` before it started calling ``start()``).

Covered paths:
- ``TradingSession.paper()``
- ``TradingSession.live(confirm=True)``
- ``runtime.startup.boot()`` (paper, backtest, and live modes)
- ``runtime.startup.boot_context()``

The raw constructor is intentionally excluded: a hand-built session stays NEW
until the caller calls ``start()`` (tested in ``test_session_lifecycle.py``).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from tradex_domain import BrokerId

from tradex_trading.config.schema import AppConfig, PersistenceConfig
from tradex_trading.sdk.session import SessionState, TradingSession


def _fake_live_broker() -> MagicMock:
    """A broker that satisfies TradingSession.live() without any network.

    A plain MagicMock suffices: ``MarketFeed.__init__`` probes
    ``getattr(broker, "capabilities", None)`` which auto-creates a MagicMock,
    and that fails the ``isinstance(..., BrokerCapabilities)`` check so the
    feed treats the broker as capability-less. NOTE: never stub capabilities
    via ``broker.__class__.capabilities = ...`` — that mutates the global
    MagicMock class and contaminates every other test in the process.
    """
    broker = MagicMock()
    backend = MagicMock()
    backend.subscribe_orders = MagicMock()
    broker.stream_backend.return_value = backend
    return broker


def test_paper_factory_returns_ready() -> None:
    session = TradingSession.paper()
    assert session.state == SessionState.READY
    # Services are immediately usable — no explicit start() needed.
    assert session.broker is not None
    session.stop()


def test_live_factory_returns_ready(monkeypatch, tmp_path) -> None:
    from tradex_trading import runtime
    import tradex_trading.sdk.session as session_mod

    monkeypatch.setattr(
        runtime.live, "build_broker_from_env", lambda _provider: _fake_live_broker()
    )
    monkeypatch.setattr(
        session_mod,
        "AppConfig",
        lambda **kwargs: AppConfig(
            **kwargs, persistence=PersistenceConfig(path=str(tmp_path / "orders.db"))
        ),
    )
    session = TradingSession.live(BrokerId.DHAN, confirm=True)
    assert session.state == SessionState.READY
    assert session.broker is not None
    session.stop()


def test_live_factory_requires_confirm() -> None:
    with pytest.raises(ValueError, match="Live trading requires explicit confirmation"):
        TradingSession.live(BrokerId.DHAN)


def test_boot_paper_returns_ready() -> None:
    from tradex_trading.runtime.startup import boot

    session = boot(AppConfig(broker_id=BrokerId.PAPER, mode="paper"))
    assert session.state == SessionState.READY
    assert session.broker is not None
    session.stop()


def test_boot_backtest_returns_ready() -> None:
    from tradex_trading.runtime.startup import boot

    session = boot(AppConfig(broker_id=BrokerId.PAPER, mode="backtest"))
    assert session.state == SessionState.READY
    assert session.broker is not None
    session.stop()


def test_boot_live_returns_ready(monkeypatch, tmp_path) -> None:
    """boot(live) returns READY without touching the network.

    ``startup.boot`` builds the live broker through
    ``build_broker_from_env`` — patch the module-level name with a fake
    factory (restored cleanly after the test), so the
    connect/stream-backend/fill-source wiring runs offline.
    """
    from tradex_trading.runtime import live as live_mod
    from tradex_trading.runtime import startup

    fake = _fake_live_broker()

    class _FakeFactory:
        @staticmethod
        def build_broker_from_env(_broker_id: str, **_kw: object) -> MagicMock:
            return fake

    monkeypatch.setattr(live_mod, "build_broker_from_env", _FakeFactory.build_broker_from_env)
    monkeypatch.setattr(
        startup,
        "AppConfig",
        lambda **kwargs: AppConfig(
            **kwargs, persistence=PersistenceConfig(path=str(tmp_path / "orders.db"))
        ),
    )

    session = startup.boot(
        AppConfig(
            broker_id=BrokerId.DHAN,
            mode="live",
            live_enabled=True,
            persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
        )
    )
    assert session.state == SessionState.READY
    assert session.broker is not None
    assert session.bus is not None
    assert fake.connect.called
    session.stop()


def test_boot_context_returns_ready_session() -> None:
    from tradex_trading.runtime.startup import boot_context

    ctx = boot_context(AppConfig(broker_id=BrokerId.PAPER, mode="paper"))
    assert ctx.session.state == SessionState.READY
    assert ctx.session.broker is not None
    ctx.close()
    assert ctx.session.state == SessionState.STOPPED
