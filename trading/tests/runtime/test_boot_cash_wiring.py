"""C1 follow-up — wire CashLedger into boot().

The risk gate was added in `d6f65ea`; this test ensures the boot path
actually binds the cash provider for paper and live modes. Without
this, the gate is dormant in real sessions.

The tests use a small custom boot hook: AppConfig accepts an optional
``cash_provider`` callable on the risk config; the boot calls
``risk_manager.bind_cash_provider(cash_provider)`` after construction.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_trading.config.schema import AppConfig, RiskConfig
from tradex_trading.runtime.startup import boot


def test_paper_boot_binds_cash_provider_when_configured() -> None:
    """RED: passing cash_provider= on AppConfig binds it to the risk manager."""
    cash = {"value": Decimal("100000")}

    def provider() -> Decimal:
        return cash["value"]

    cfg = AppConfig(mode="paper", risk=RiskConfig(cash_provider=provider))
    session = boot(cfg)
    try:
        engine = session.engine
        # The risk manager must have a bound cash provider after boot.
        assert engine._risk.cash_provider_bound is True  # type: ignore[attr-defined]
    finally:
        session.stop()


def test_paper_boot_without_configured_provider_uses_broker_funds() -> None:
    """A paper boot with no configured provider falls back to broker funds.

    This used to assert the gate stayed OFF, which meant a paper session could
    admit a BUY with no balance check at all. Paper now sources the balance
    from the broker like live does, and fails closed when it cannot be read.
    """
    cfg = AppConfig(mode="paper")
    session = boot(cfg)
    try:
        risk = session.engine._risk  # type: ignore[attr-defined]
        assert risk.cash_provider_bound is True
        assert risk.fail_closed_cash is True
    finally:
        session.stop()
