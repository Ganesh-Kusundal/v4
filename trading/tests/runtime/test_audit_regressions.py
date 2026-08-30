"""Audit regression tests — boot safety, kill switch, risk gates.

Ported from v3 ``test_audit_regressions.py``.

v4 API differences:
- ``AppConfig`` has ``mode``, ``broker_id``, ``risk``, ``kill_switch_default``, ``live_enabled``
- No separate ``BrokerConfig`` or ``environment`` fields
- ``boot()`` validates mode, live+paper broker conflict, live_enabled
- Kill switch blocks order submission with REJECTED status
- Risk limits enforced by ``RiskManager``
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain import BrokerId
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.config.schema import AppConfig, RiskConfig
from tradex_trading.runtime.startup import boot


def _equity() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request(
    *,
    quantity: int = 10,
    price: Decimal = Decimal("100"),
) -> OrderRequest:
    return OrderRequest(
        instrument=_equity(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=price),
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
    )


# ---------------------------------------------------------------------------
# Boot safety — invalid configs rejected
# ---------------------------------------------------------------------------


class TestBootSafety:
    """boot() rejects invalid configurations."""

    def test_invalid_mode_rejected(self) -> None:
        config = AppConfig(mode="invalid")
        with pytest.raises(ValueError, match="unknown mode"):
            boot(config)

    def test_live_mode_with_paper_broker_rejected(self) -> None:
        config = AppConfig(
            mode="live",
            broker_id=BrokerId.PAPER,
            live_enabled=True,
        )
        with pytest.raises(ValueError, match="non-paper broker"):
            boot(config)

    def test_live_mode_without_live_enabled_rejected(self) -> None:
        config = AppConfig(
            mode="live",
            broker_id=BrokerId.DHAN,
            live_enabled=False,
        )
        with pytest.raises(ValueError, match="live_enabled"):
            boot(config)


# ---------------------------------------------------------------------------
# Kill switch — blocks order submission
# ---------------------------------------------------------------------------


class TestKillSwitchRegression:
    """Kill switch must block order submission when enabled."""

    def test_kill_switch_default_from_config(self) -> None:
        config = AppConfig(kill_switch_default=True)
        session = boot(config)
        receipt = session.engine.submit(_request())
        assert receipt.status == OrderStatus.REJECTED
        session.stop()

    def test_kill_switch_rejects_orders(self) -> None:
        session = boot()
        session._engine.kill_switch = True
        receipt = session.engine.submit(_request())
        assert receipt.status == OrderStatus.REJECTED
        session.stop()


# ---------------------------------------------------------------------------
# Risk gates — max order value
# ---------------------------------------------------------------------------


class TestRiskGateRegression:
    """Risk limits must be enforced by the execution engine."""

    def test_max_order_value_enforced(self) -> None:
        config = AppConfig(
            risk=RiskConfig(max_order_value=Decimal("500")),
        )
        session = boot(config)
        # price=100 * quantity=10 = 1000 > max_order_value=500
        receipt = session.engine.submit(_request(quantity=10, price=Decimal("100")))
        assert receipt.status == OrderStatus.REJECTED
        session.stop()

    def test_risk_allows_small_orders(self) -> None:
        config = AppConfig(
            risk=RiskConfig(max_order_value=Decimal("5000")),
        )
        session = boot(config)
        # price=100 * quantity=2 = 200 < max_order_value=5000
        receipt = session.engine.submit(_request(quantity=2, price=Decimal("100")))
        assert receipt.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
        session.stop()


# ---------------------------------------------------------------------------
# Paper mode — default boot works
# ---------------------------------------------------------------------------


class TestPaperModeRegression:
    """Paper mode boot must succeed with default config."""

    def test_default_boot_succeeds(self) -> None:
        session = boot()
        assert session.state == "READY"
        assert session.broker_id == BrokerId.PAPER
        session.stop()

    def test_paper_mode_submit_works(self) -> None:
        session = boot()
        receipt = session.engine.submit(_request())
        assert receipt.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
        session.stop()
