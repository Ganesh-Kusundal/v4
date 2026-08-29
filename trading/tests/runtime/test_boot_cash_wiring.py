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


def test_paper_boot_no_cash_provider_means_no_gate() -> None:
    """Backward compat: a paper boot without cash_provider has the gate off."""
    cfg = AppConfig(mode="paper")
    session = boot(cfg)
    try:
        assert session.engine._risk.cash_provider_bound is False  # type: ignore[attr-defined]
    finally:
        session.stop()
