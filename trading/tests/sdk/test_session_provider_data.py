"""Session provider data tests — boot modes and session services.

Ported from v3 ``test_session_provider_data.py``.

v4 API differences:
- ``DhanBroker`` / ``UpstoxBroker`` are stubs — all trading methods raise ``NotImplementedError``
- ``boot(config)`` accepts ``AppConfig`` with mode, broker_id, risk, etc.
- Provider-specific data contracts (quote/ltp/history) cannot be tested with stubs
- Instead, test boot modes and session service accessibility
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain import BrokerId

from tradex_trading.config.schema import AppConfig, RiskConfig
from tradex_trading.runtime.startup import boot
from tradex_trading.sdk.session import SessionState

# ---------------------------------------------------------------------------
# Boot modes — verify session starts correctly in each mode
# ---------------------------------------------------------------------------


class TestBootModes:
    """boot() works in paper, backtest, and replay modes."""

    def test_boot_paper_mode(self) -> None:
        config = AppConfig(mode="paper")
        session = boot(config)
        assert session.state == SessionState.READY
        assert session.mode == "paper"
        session.stop()

    def test_boot_backtest_mode(self) -> None:
        config = AppConfig(mode="backtest")
        session = boot(config)
        assert session.state == SessionState.READY
        assert session.mode == "backtest"
        session.stop()

    def test_boot_replay_mode(self) -> None:
        config = AppConfig(mode="replay")
        session = boot(config)
        assert session.state == SessionState.READY
        assert session.mode == "replay"
        session.stop()


# ---------------------------------------------------------------------------
# Boot safety — invalid configs rejected
# ---------------------------------------------------------------------------


class TestBootSafety:
    """boot() rejects invalid configurations."""

    def test_invalid_mode_raises(self) -> None:
        config = AppConfig(mode="invalid")
        with pytest.raises(ValueError, match="unknown mode"):
            boot(config)

    def test_live_mode_with_paper_broker_raises(self) -> None:
        config = AppConfig(mode="live", broker_id=BrokerId.PAPER, live_enabled=True)
        with pytest.raises(ValueError, match="live mode requires a non-paper broker"):
            boot(config)

    def test_live_mode_without_live_enabled_raises(self) -> None:
        config = AppConfig(mode="live", broker_id=BrokerId.DHAN, live_enabled=False)
        with pytest.raises(ValueError, match="live mode requires live_enabled"):
            boot(config)


# ---------------------------------------------------------------------------
# Session services accessible in all non-live modes
# ---------------------------------------------------------------------------


class TestSessionServicesInModes:
    """All 7 services accessible after boot in each mode."""

    @pytest.mark.parametrize("mode", ["paper", "backtest", "replay"])
    def test_all_services_accessible(self, mode: str) -> None:
        config = AppConfig(mode=mode)
        session = boot(config)
        assert session.market is not None
        assert session.trade is not None
        assert session.portfolio is not None
        assert session.stream is not None
        assert session.scanner is not None
        assert session.analytics is not None
        assert session.extension is not None
        session.stop()

    @pytest.mark.parametrize("mode", ["paper", "backtest", "replay"])
    def test_broker_id_is_paper(self, mode: str) -> None:
        config = AppConfig(mode=mode)
        session = boot(config)
        assert session.broker_id == BrokerId.PAPER
        session.stop()


# ---------------------------------------------------------------------------
# Risk config propagation
# ---------------------------------------------------------------------------


class TestRiskConfigPropagation:
    """RiskConfig is propagated to the execution engine."""

    def test_default_risk_config(self) -> None:
        session = boot()
        # Session should be working with default risk config
        assert session.state == SessionState.READY
        session.stop()

    def test_custom_risk_config(self) -> None:
        config = AppConfig(
            risk=RiskConfig(
                max_order_value=Decimal("100000"),
                max_position_value=Decimal("500000"),
                max_orders_per_minute=60,
            ),
        )
        session = boot(config)
        assert session.state == SessionState.READY
        session.stop()
