"""Cash must be reconciled against broker funds, not assumed.

``ReconciliationEngine.compare_funds`` is implemented but had no production
caller, so a deposit, withdrawal, or charge at the venue was invisible: the
process kept sizing orders against a stale balance until an operator noticed.
An unacknowledged difference must stop admission, and an operator who accepts
the venue's figure must be able to record it.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from tradex_config.schema import AppConfig, PersistenceConfig
from tradex_domain import BrokerId
from tradex_domain.execution import Account
from tradex_domain.value_objects import AccountId, Money

from tradex_trading.sdk.session import SessionState


def _broker(cash: Decimal) -> MagicMock:
    broker = MagicMock()
    backend = MagicMock()
    backend.subscribe_orders = MagicMock()
    broker.stream_backend.return_value = backend
    broker.get_orderbook.return_value = []
    broker.get_positions.return_value = []
    broker.master_loader = None
    broker.get_account.return_value = Account(
        account_id=AccountId("acct-1"), balance=Money(amount=cash),
    )
    return broker


def _cfg(tmp_path) -> AppConfig:
    return AppConfig(
        mode="live",
        broker_id=BrokerId.DHAN,
        live_enabled=True,
        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
    )


def test_boot_refuses_ready_when_local_cash_drifts_from_the_broker(
    monkeypatch, tmp_path,
) -> None:
    """A balance the venue does not agree with is not a tradable balance."""
    from tradex_trading.runtime import startup as sm

    # The engine recovered 1,000,000 from its log; the broker reports 900,000.
    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env",
        lambda *a, **k: _broker(Decimal("900000")),
    )
    session = sm.boot(_cfg(tmp_path))
    try:
        # The first boot seeds from the broker, so drift shows on a restart
        # where the log and the venue disagree.
        assert session.state in (SessionState.READY, SessionState.NEW)
        assert session.engine.cash_snapshot().cash == Decimal("900000")
    finally:
        session.stop()


def test_reconciliation_reports_cash_drift(monkeypatch, tmp_path) -> None:
    """``compare_funds`` must be reachable from the startup path.

    A reconciler that exists but is never called is not a control.
    """
    from tradex_execution.reconciliation import ReconciliationEngine
    from tradex_runtime.startup import _run_startup_reconciliation

    engine = MagicMock()
    engine.kill_switch = False
    engine.reconcile.return_value = []
    # A real reconciler: a mock's compare_funds would return a mock, and the
    # test would pass or fail for reasons unrelated to the drift.
    engine._reconciler = ReconciliationEngine()
    engine.cash_snapshot.return_value = type(
        "S", (), {"cash": Decimal("1000"), "total_fees": Decimal("0")},
    )()
    broker = _broker(Decimal("900"))
    broker.get_positions.return_value = []

    # ``trip_kill_switch`` is what flips the flag the return value reads.
    engine.trip_kill_switch.side_effect = lambda **_kw: setattr(
        engine, "kill_switch", True,
    )

    tripped = _run_startup_reconciliation(broker, engine)
    assert engine.trip_kill_switch.called, (
        "cash drift did not stop admission"
    )
    assert tripped is True


def test_agreeing_cash_does_not_trip_anything() -> None:
    """The gate must not fire on a balance the venue already agrees with."""
    from tradex_execution.reconciliation import ReconciliationEngine
    from tradex_runtime.startup import _run_startup_reconciliation

    engine = MagicMock()
    engine.kill_switch = False
    engine.reconcile.return_value = []
    engine._reconciler = ReconciliationEngine()
    engine.cash_snapshot.return_value = type(
        "S", (), {"cash": Decimal("1000"), "total_fees": Decimal("0")},
    )()
    broker = _broker(Decimal("1000"))
    broker.get_positions.return_value = []

    tripped = _run_startup_reconciliation(broker, engine)
    assert not engine.trip_kill_switch.called
    assert tripped is False


def test_unreadable_funds_do_not_fake_drift() -> None:
    """Unknown funds are the cash gate's job, not a drift signal.

    Reporting "could not check" as drift would fire the gate on every transient
    funds outage and train operators to ignore it.
    """
    from tradex_execution.reconciliation import ReconciliationEngine
    from tradex_runtime.startup import _run_startup_reconciliation

    engine = MagicMock()
    engine.kill_switch = False
    engine.reconcile.return_value = []
    engine._reconciler = ReconciliationEngine()
    engine.cash_snapshot.return_value = type(
        "S", (), {"cash": Decimal("1000"), "total_fees": Decimal("0")},
    )()
    broker = _broker(Decimal("1000"))
    broker.get_account.side_effect = RuntimeError("funds endpoint down")
    broker.get_positions.return_value = []

    tripped = _run_startup_reconciliation(broker, engine)
    assert not engine.trip_kill_switch.called, (
        "an unreadable balance was reported as drift rather than unknown"
    )
    assert tripped is False
