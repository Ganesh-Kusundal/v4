"""Phase 1: Composition Root & Bootstrap Runtime Verification.
Tests fail-closed behavior, configuration consistency, and session initialization.
"""

import os
from decimal import Decimal

import pytest
from tradex_config.schema import AppConfig, PersistenceConfig
from tradex_domain import BrokerId
from tradex_runtime.session_impl import SessionState, TradingSession
from tradex_runtime.startup import boot

from trading.tests.risk_support import permissive_risk


def test_bootstrap_paper_session_success():
    """Verify clean composition root bootstrap in paper mode.

    The real composition root is ``boot()`` which returns a ready
    ``TradingSession`` with all components wired.
    """
    session = boot(AppConfig(risk=permissive_risk(), mode="paper"))
    try:
        assert isinstance(session, TradingSession)
        # Composition root hands back a session that is already started.
        assert session.state == SessionState.READY
        assert session.engine is not None
        assert session.bus is not None
        # The OMS cache the engine owns.
        assert session.engine.cache is not None

        # Verify initial cash is allocated. Paper mode seeds the engine's cash
        # ledger from the broker's reported balance (startup._boot_tail), so
        # the live, engine-owned balance is the cash that a BUY is settled
        # against — the meaningful "money is present" invariant.
        snapshot = session.engine.cash_snapshot()
        assert snapshot.cash > Decimal("0")

        # The same balance is visible on the broker's account the way the old
        # ``session.account.balance`` assertion intended.
        account = session.broker.get_account()
        assert account is not None
        assert account.balance.amount > Decimal("0")
    finally:
        session.stop()


def test_bootstrap_live_fails_without_credentials():
    """Verify boot fails closed immediately when live broker credentials are missing.

    A live boot needs a non-paper broker, ``live_enabled=True`` and a
    persistence path to get past the config gates; it then builds the broker
    via ``build_broker_from_env``, which raises ``AuthenticationError`` naming
    the missing credential before any network call. The test drives that real
    path with the Dhan credentials cleared from the environment.
    """
    dhan_env = [
        k for k in os.environ
        if k.startswith("DHAN_")
    ]
    saved = {k: os.environ.pop(k) for k in dhan_env}
    try:
        cfg = AppConfig(risk=permissive_risk(),
            broker_id=BrokerId.DHAN,
            mode="live",
            live_enabled=True,
            persistence=PersistenceConfig(path="/tmp/tradex-runtime-audit/live.db"),
        )
        with pytest.raises(Exception) as exc_info:
            boot(cfg)
        err_msg = str(exc_info.value).upper()
        assert (
            "DHAN" in err_msg
            or "CREDENTIAL" in err_msg
            or "CLIENT_ID" in err_msg
            or "TOKEN" in err_msg
        )
    finally:
        os.environ.update(saved)
