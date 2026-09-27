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

from typing import Any
from unittest.mock import MagicMock

import pytest
from tradex_config.schema import FEED_INSTRUMENTS_ENV, AppConfig, PersistenceConfig
from tradex_domain import BrokerId
from tradex_runtime.bar_aggregator import BarFrame
from tradex_runtime.feed_recovery import ParquetHistoryProvider
from tradex_runtime.session_impl import SessionState, TradingSession


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


class _OneReconcilableBar:
    """A ``fetch_bars`` that always reconciles, whatever window it is asked for.

    An *instance* rather than a function: ``fetch_bars`` is patched as a class
    attribute, and only a plain function would bind ``self`` to it and swallow
    the instrument the coordinator passes first.
    """

    def __call__(self, instrument: Any, timeframe: str, start: Any, end: Any) -> list:
        return [
            BarFrame(
                instrument=str(instrument.instrument_id),
                timeframe="1m",
                time=1789946400,  # 2026-09-24T10:00Z
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=10.0,
                closed=True,
            )
        ]


def _name_a_tape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give a live boot the tape an operator would have named, plus a history.

    A live boot refuses to start unless the tape is named — through
    ``$TRADEX_FEED_INSTRUMENTS`` or ``boot(feed_instruments=)`` — and the cold
    start it then runs is ``READY`` only when the history reconciliation returns
    a bar. These are the repo-wide factory tests, so a live session has to come
    up the way an unattended operator's does, via the variable the refusal
    itself points at. Spelled out here rather than shared with
    ``trading/tests/runtime/conftest.py`` so a fixture does not reach every
    directory under ``trading/tests``.
    """
    monkeypatch.setenv(FEED_INSTRUMENTS_ENV, "NSE:RELIANCE")
    monkeypatch.setattr(ParquetHistoryProvider, "fetch_bars", _OneReconcilableBar())


def test_paper_factory_returns_ready() -> None:
    session = TradingSession.paper()
    assert session.state == SessionState.READY
    # Services are immediately usable — no explicit start() needed.
    assert session.broker is not None
    session.stop()


def test_live_factory_returns_ready(monkeypatch, tmp_path) -> None:
    import tradex_runtime.live as runtime_live
    import tradex_runtime.session_impl as session_mod

    _name_a_tape(monkeypatch)
    monkeypatch.setattr(
        runtime_live, "build_broker_from_env", lambda _provider: _fake_live_broker()
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
    from tradex_runtime.startup import boot

    session = boot(AppConfig(broker_id=BrokerId.PAPER, mode="paper"))
    assert session.state == SessionState.READY
    assert session.broker is not None
    session.stop()


def test_boot_backtest_returns_ready() -> None:
    from tradex_runtime.startup import boot

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
    import tradex_runtime.live as live_mod
    import tradex_runtime.startup as startup

    _name_a_tape(monkeypatch)
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


def test_boot_live_fails_when_verify_connection_false(monkeypatch, tmp_path) -> None:
    """Live boot must fail closed when the wire auth probe fails."""
    import tradex_runtime.live as live_mod
    import tradex_runtime.startup as startup

    fake = _fake_live_broker()
    fake.verify_connection.return_value = False

    monkeypatch.setattr(live_mod, "build_broker_from_env", lambda _broker_id, **_kw: fake)
    monkeypatch.setattr(
        startup,
        "AppConfig",
        lambda **kwargs: AppConfig(
            **kwargs, persistence=PersistenceConfig(path=str(tmp_path / "orders.db"))
        ),
    )

    with pytest.raises(RuntimeError, match="wire authentication|verify_connection"):
        startup.boot(
            AppConfig(
                broker_id=BrokerId.DHAN,
                mode="live",
                live_enabled=True,
                persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
            )
        )
    assert fake.connect.called
    assert fake.verify_connection.called


def test_boot_live_has_one_feed_supervisor(monkeypatch, tmp_path) -> None:
    from datetime import UTC, datetime

    import tradex_runtime.live as live_mod
    import tradex_runtime.startup as startup
    from tradex_domain import (
        CorrelationId,
        Equity,
        OrderRequest,
        OrderSide,
        OrderStatus,
        OrderType,
        Price,
        Quantity,
    )

    _name_a_tape(monkeypatch)
    fake = _fake_live_broker()
    fake.get_orderbook.return_value = []
    fake.get_positions.return_value = []
    monkeypatch.setattr(live_mod, "build_broker_from_env", lambda _broker_id, **_kw: fake)
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
    try:
        assert session.feed_supervisor is session.market_feed.supervisor
        assert session.engine.feed_supervisor is session.market_feed.supervisor

        # Shared supervisor drives the engine gate: not READY → feed_not_ready;
        # after recovery → submit is not rejected for feed.
        from decimal import Decimal

        req = OrderRequest(
            instrument=Equity.of("NSE", "RELIANCE"),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("1")),
            price=Price(value=Decimal("100")),
            correlation_id=CorrelationId(value="shared-sup-gate"),
        )
        sup = session.engine.feed_supervisor
        assert sup is not None
        # The boot cold started the feed for the tape it was given, so it is
        # READY already; the gate is exercised in both directions instead.
        # Demote first — what a socket reopen does — and the very next order
        # must be refused for the feed, through this one shared supervisor.
        sup.connected()
        sup.recovery_started()
        blocked = session.engine.submit(req)
        assert blocked.status is OrderStatus.REJECTED
        assert blocked.message == "feed_not_ready"

        sup.recovery_succeeded(
            last_event_at=datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
            recovered_through=datetime(2026, 9, 24, 10, 1, tzinfo=UTC),
        )
        req2 = OrderRequest(
            instrument=Equity.of("NSE", "RELIANCE"),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("1")),
            price=Price(value=Decimal("100")),
            correlation_id=CorrelationId(value="shared-sup-ready"),
        )
        after = session.engine.submit(req2)
        assert after.message != "feed_not_ready"
    finally:
        session.stop()


def test_boot_context_returns_ready_session() -> None:
    from tradex_runtime.startup import boot_context

    ctx = boot_context(AppConfig(broker_id=BrokerId.PAPER, mode="paper"))
    assert ctx.session.state == SessionState.READY
    assert ctx.session.broker is not None
    ctx.close()
    assert ctx.session.state == SessionState.STOPPED
