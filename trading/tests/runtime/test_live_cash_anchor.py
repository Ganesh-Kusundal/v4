"""Live boot binds a real cash provider and anchors the account balance.

The risk cash gate is the last check between a stale balance and real money,
so live must read broker funds rather than leaving the gate unbound.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from tradex_config.schema import AppConfig, PersistenceConfig
from tradex_domain import BrokerId
from tradex_domain.execution import Account
from tradex_domain.value_objects import AccountId, Money


def _broker_with_cash(cash: Decimal | None) -> MagicMock:
    broker = MagicMock()
    backend = MagicMock()
    backend.subscribe_orders = MagicMock()
    broker.stream_backend.return_value = backend
    broker.get_orderbook.return_value = []
    broker.get_positions.return_value = []
    broker.master_loader = None
    balance = None if cash is None else Money(amount=cash)
    broker.get_account.return_value = Account(
        account_id=AccountId("acct-1"), balance=balance,
    )
    return broker


def _live_cfg(tmp_path) -> AppConfig:
    return AppConfig(
        mode="live",
        broker_id=BrokerId.DHAN,
        live_enabled=True,
        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
    )


def test_live_boot_binds_cash_provider_from_broker(monkeypatch, tmp_path) -> None:
    """The gate must read broker funds, not sit unbound."""
    from tradex_trading.runtime import startup as sm

    broker = _broker_with_cash(Decimal("500000"))
    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env", lambda *a, **k: broker,
    )
    session = sm.boot(_live_cfg(tmp_path))
    try:
        risk = session.engine._risk
        assert risk.cash_provider_bound is True
        assert risk.fail_closed_cash is True
        assert risk._cash_provider() == Decimal("500000")
    finally:
        session.stop()


def test_live_boot_anchors_cash_so_a_restart_can_fold_it(
    monkeypatch, tmp_path,
) -> None:
    """The engine must hold and record the balance, not merely read it.

    Wiring the provider is not enough: without a ledger and a durable anchor the
    process reports cash as 0 and a restart has nothing to fold.
    """
    from tradex_config.schema import AppConfig, PersistenceConfig
    from tradex_domain.events import CashAccountInitialized
    from tradex_execution.sqlite_event_store import SQLiteEventStore

    from tradex_trading.runtime import startup as sm

    db = str(tmp_path / "orders.db")
    cfg = AppConfig(
        mode="live",
        broker_id=BrokerId.DHAN,
        live_enabled=True,
        persistence=PersistenceConfig(path=db),
    )
    broker = _broker_with_cash(Decimal("500000"))
    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env", lambda *a, **k: broker,
    )
    session = sm.boot(cfg)
    try:
        assert session.engine.cash_snapshot().cash == Decimal("500000")
        assert session.engine.cash_anchor_missing is False
    finally:
        session.stop()

    store = SQLiteEventStore(db)
    try:
        anchors = [
            e for e in store.replay("orders")
            if isinstance(e, CashAccountInitialized)
        ]
        assert [a.amount for a in anchors] == [Decimal("500000")]
    finally:
        store.close()


def test_live_boot_reports_unknown_when_funds_unreadable(monkeypatch, tmp_path) -> None:
    """An unreadable balance is unknown, and unknown denies rather than admits."""
    from tradex_trading.runtime import startup as sm

    broker = _broker_with_cash(None)
    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env", lambda *a, **k: broker,
    )
    session = sm.boot(_live_cfg(tmp_path))
    try:
        risk = session.engine._risk
        assert risk.cash_provider_bound is True
        assert risk._cash_provider() is None
    finally:
        session.stop()


def test_live_boot_reports_unknown_when_funds_call_raises(monkeypatch, tmp_path) -> None:
    """A funds call that blows up must not be read as a zero balance."""
    from tradex_trading.runtime import startup as sm

    broker = _broker_with_cash(Decimal("100"))
    broker.get_account.side_effect = RuntimeError("funds endpoint down")
    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env", lambda *a, **k: broker,
    )
    session = sm.boot(_live_cfg(tmp_path))
    try:
        assert session.engine._risk._cash_provider() is None
    finally:
        session.stop()


def test_paper_boot_binds_cash_provider() -> None:
    from tradex_trading.runtime import startup as sm

    session = sm.boot(AppConfig(mode="paper"))
    try:
        risk = session.engine._risk
        assert risk.cash_provider_bound is True
        assert risk.fail_closed_cash is True
    finally:
        session.stop()
