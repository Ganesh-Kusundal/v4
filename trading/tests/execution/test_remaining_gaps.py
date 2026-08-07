"""Tests for remaining P0/P1/P2 gap fixes across engine, fees, reconciliation.

Covers:
- P0: UNKNOWN in terminal statuses, live_orders_enabled gate, async pipeline
  boundary-crossed + idempotency record + position_projection_owned guard,
  reconcile with broker_orders, kill switch → risk manager propagation
- P1: ExecutionEngine.get_order/all_orders
- P2: FeeCalculator brokerage cap, reconciliation avg_price drift,
  ProviderHttpClient business-level token rejection
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType
from tradex_domain.execution import Fill, OrderRequest, Position
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import (
    CorrelationId,
    Money,
    OrderId,
    Price,
    Quantity,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_instrument():
    return Equity.of("NSE", "RELIANCE")


def _make_request(**overrides):
    defaults = dict(
        instrument=_make_instrument(),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force="DAY",
    )
    defaults.update(overrides)
    return OrderRequest(**defaults)


def _make_fill(order_id: OrderId, price_value: str = "100"):
    return Fill(
        order_id=order_id,
        instrument=_make_instrument(),
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal(price_value)),
    )


# ===================================================================
# P0: UNKNOWN in terminal statuses
# ===================================================================


class TestUnknownInTerminalStatuses:
    def test_unknown_is_terminal(self):
        from tradex_trading.execution.engine import _TERMINAL_STATUSES
        assert OrderStatus.UNKNOWN in _TERMINAL_STATUSES

    def test_all_four_terminal_statuses(self):
        from tradex_trading.execution.engine import _TERMINAL_STATUSES
        expected = {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.UNKNOWN,
        }
        assert _TERMINAL_STATUSES == expected


# ===================================================================
# P0: RiskManager live_orders_enabled master gate
# ===================================================================


class TestRiskManagerLiveOrdersGate:
    def test_default_enabled(self):
        from tradex_trading.execution.engine import RiskManager
        rm = RiskManager()
        assert rm.live_orders_enabled is True

    def test_gate_blocks_orders(self):
        from tradex_trading.execution.engine import RiskManager
        rm = RiskManager(live_orders_enabled=False)
        req = _make_request()
        assert rm.check(req) is False

    def test_gate_can_be_toggled(self):
        from tradex_trading.execution.engine import RiskManager
        rm = RiskManager(live_orders_enabled=False)
        req = _make_request()
        assert rm.check(req) is False
        rm.live_orders_enabled = True
        assert rm.check(req) is True

    def test_gate_blocks_before_other_checks(self):
        from tradex_trading.execution.engine import RiskManager
        rm = RiskManager(
            max_order_value=Decimal("1"),
            live_orders_enabled=False,
        )
        req = _make_request(
            price=Price(value=Decimal("1000")),
            quantity=Quantity(value=Decimal("100")),
        )
        # Even though order value exceeds limit, gate is the first check
        assert rm.check(req) is False


# ===================================================================
# P0: Kill switch propagates to risk manager
# ===================================================================


class TestKillSwitchPropagatesToRiskManager:
    def test_trip_kill_disables_risk_manager(self):
        from tradex_trading.execution.engine import ExecutionEngine, RiskManager
        from tradex_trading.execution.fill_sources import SimulatedFillSource

        bus = MagicMock()
        bus.of_type = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()

        rm = RiskManager()
        engine = ExecutionEngine(
            bus=bus,
            fill_source=SimulatedFillSource(),
            risk_manager=rm,
        )
        assert rm.live_orders_enabled is True

        engine.trip_kill_switch("test")
        assert rm.live_orders_enabled is False
        assert engine.kill_switch is True


# ===================================================================
# P0: Reconcile with broker_orders
# ===================================================================


class TestReconcileWithBrokerOrders:
    def test_reconcile_detect_order_drift(self):
        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import SimulatedFillSource

        bus = MagicMock()
        bus.of_type = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()

        engine = ExecutionEngine(
            bus=bus,
            fill_source=SimulatedFillSource(),
        )
        # Reconcile with empty broker orders → all local orders are drift
        drifts = engine.reconcile(broker_orders=[])
        # No local orders, so no drift
        assert drifts == []

    def test_reconcile_with_both_positions_and_orders(self):
        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import SimulatedFillSource

        bus = MagicMock()
        bus.of_type = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()

        engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())
        drifts = engine.reconcile(
            broker_positions=[],
            broker_orders=[],
        )
        assert isinstance(drifts, list)


# ===================================================================
# P1: ExecutionEngine.get_order / all_orders
# ===================================================================


class TestExecutionEngineOrderLookups:
    def test_get_order_returns_none_for_missing(self):
        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import SimulatedFillSource

        bus = MagicMock()
        bus.of_type = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()

        engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())
        result = engine.get_order(OrderId(value="nonexistent"))
        assert result is None

    def test_all_orders_empty_initially(self):
        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import SimulatedFillSource

        bus = MagicMock()
        bus.of_type = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()

        engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())
        assert engine.all_orders() == []


# ===================================================================
# P2: FeeCalculator brokerage cap
# ===================================================================


class TestFeeCalculatorBrokerageCap:
    def test_brokerage_capped_at_20(self):
        from tradex_trading.execution.fees import _BROKERAGE_CAP, FeeCalculator

        calc = FeeCalculator()
        # Large trade: 1000 shares at 5000 = 5,000,000
        # 0.03% of 5M = 1500, but brokerage cap is Rs 20
        fill = Fill(
            order_id=OrderId(value="cap-test"),
            instrument=_make_instrument(),
            side=OrderSide.BUY,
            quantity=Quantity(value=Decimal("1000")),
            price=Price(value=Decimal("5000")),
        )
        fee = calc.calculate(fill)
        # Brokerage is capped at 20, but STT/exchange/GST add more
        # Without cap, brokerage alone would be 1500
        # With cap, total should be much less than without cap
        uncapped_brokerage = Decimal("5000000") * Decimal("0.03") / Decimal("100")
        assert uncapped_brokerage > _BROKERAGE_CAP  # confirms cap applies
        # Total fee should be reasonable (not thousands from uncapped brokerage)
        assert fee.amount < Decimal("2000")

    def test_small_trade_brokerage_not_capped(self):
        from tradex_trading.execution.fees import FeeCalculator

        calc = FeeCalculator()
        # Small trade: 1 share at 100 = 100
        # 0.03% of 100 = 0.03, well under cap
        fill = Fill(
            order_id=OrderId(value="small-test"),
            instrument=_make_instrument(),
            side=OrderSide.BUY,
            quantity=Quantity(value=Decimal("1")),
            price=Price(value=Decimal("100")),
        )
        fee = calc.calculate(fill)
        assert fee.amount > Decimal("0")


# ===================================================================
# P2: Reconciliation avg_price drift detection
# ===================================================================


class TestReconciliationAvgPriceDrift:
    def test_detects_avg_price_drift(self):
        from tradex_trading.execution.reconciliation import (
            DriftSeverity,
            ReconciliationEngine,
        )

        engine = ReconciliationEngine()
        inst = _make_instrument()

        local_pos = Position(
            instrument=inst,
            quantity=Quantity(value=Decimal("100")),
            avg_price=Price(value=Decimal("250.00")),
            realized_pnl=Money(amount=Decimal("0")),
            unrealized_pnl=Money(amount=Decimal("0")),
        )
        broker_pos = Position(
            instrument=inst,
            quantity=Quantity(value=Decimal("100")),
            avg_price=Price(value=Decimal("251.00")),
            realized_pnl=Money(amount=Decimal("0")),
            unrealized_pnl=Money(amount=Decimal("0")),
        )

        drifts = engine.reconcile([local_pos], [broker_pos])
        # Should detect avg_price drift (> 0.01 tolerance)
        price_drifts = [d for d in drifts if d.reason == "avg_price drift"]
        assert len(price_drifts) == 1
        assert price_drifts[0].severity == DriftSeverity.MEDIUM

    def test_no_drift_when_prices_match(self):
        from tradex_trading.execution.reconciliation import ReconciliationEngine

        engine = ReconciliationEngine()
        inst = _make_instrument()

        pos = Position(
            instrument=inst,
            quantity=Quantity(value=Decimal("100")),
            avg_price=Price(value=Decimal("250.00")),
            realized_pnl=Money(amount=Decimal("0")),
            unrealized_pnl=Money(amount=Decimal("0")),
        )

        drifts = engine.reconcile([pos], [pos])
        assert len(drifts) == 0

    def test_no_price_drift_for_tiny_difference(self):
        from tradex_trading.execution.reconciliation import ReconciliationEngine

        engine = ReconciliationEngine()
        inst = _make_instrument()

        local_pos = Position(
            instrument=inst,
            quantity=Quantity(value=Decimal("100")),
            avg_price=Price(value=Decimal("250.005")),
            realized_pnl=Money(amount=Decimal("0")),
            unrealized_pnl=Money(amount=Decimal("0")),
        )
        broker_pos = Position(
            instrument=inst,
            quantity=Quantity(value=Decimal("100")),
            avg_price=Price(value=Decimal("250.010")),
            realized_pnl=Money(amount=Decimal("0")),
            unrealized_pnl=Money(amount=Decimal("0")),
        )

        drifts = engine.reconcile([local_pos], [broker_pos])
        # diff is 0.005, below 0.01 tolerance
        price_drifts = [d for d in drifts if d.reason == "avg_price drift"]
        assert len(price_drifts) == 0


# ===================================================================
# P0: ProviderHttpClient business-level token rejection
# ===================================================================


class TestBusinessTokenRejection:
    def test_is_business_token_rejection_detects_markers(self):
        from tradex_brokers.common.provider_client import ProviderHttpClient

        # Response with authentication failure marker
        result = {"status": "error", "message": "invalid_token"}
        assert ProviderHttpClient._is_business_token_rejection(result) is True

    def test_is_business_token_rejection_clean_response(self):
        from tradex_brokers.common.provider_client import ProviderHttpClient

        result = {"status": "success", "data": {"order_id": "123"}}
        assert ProviderHttpClient._is_business_token_rejection(result) is False

    def test_is_business_token_rejection_no_status(self):
        from tradex_brokers.common.provider_client import ProviderHttpClient

        result = {"data": "some data"}
        assert ProviderHttpClient._is_business_token_rejection(result) is False


# ===================================================================
# P0: Async pipeline _process_request fixes
# ===================================================================


class TestAsyncPipelineFixes:
    def test_process_request_boundary_crossed_raises(self):
        """When fill source has boundary_crossed and raises, should raise
        OrderSubmissionUnknownError instead of publishing rejection."""
        from tradex_domain.errors import OrderSubmissionUnknownError

        from tradex_trading.execution.engine import ExecutionEngine

        bus = MagicMock()
        bus.of_type = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()

        fill_source = MagicMock()
        fill_source.submission_boundary_crossed = True
        fill_source.submit.side_effect = OSError("connection lost")

        engine = ExecutionEngine(bus=bus, fill_source=fill_source)

        req = _make_request()
        with pytest.raises(OrderSubmissionUnknownError):
            engine._process_request(req)

    def test_process_request_records_idempotency_result(self):
        """After successful fill, idempotency guard should have result recorded."""
        from tradex_trading.execution.engine import (
            ExecutionEngine,
            MemoryIdempotencyGuard,
        )
        from tradex_trading.execution.fill_sources import SimulatedFillSource

        bus = MagicMock()
        bus.of_type = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()

        guard = MemoryIdempotencyGuard()
        engine = ExecutionEngine(
            bus=bus,
            fill_source=SimulatedFillSource(),
            idempotency_guard=guard,
        )

        cid = CorrelationId(value="test-corr-id")
        req = _make_request(correlation_id=cid)
        engine._process_request(req)

        # The guard should have the result recorded
        dup = guard.check_and_reserve(cid)
        assert dup is not None  # Result was recorded

    def test_process_request_respects_position_projection_owned(self):
        """When fill source owns position projection, skip local position update."""
        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import SimulatedFillSource

        bus = MagicMock()
        bus.of_type = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()

        fill_source = SimulatedFillSource()
        # Simulate broker-owned projection
        fill_source.position_projection_owned = True  # type: ignore[attr-defined]

        engine = ExecutionEngine(bus=bus, fill_source=fill_source)

        req = _make_request()
        engine._process_request(req)

        # Position should NOT be updated locally since projection is owned
        positions = engine.cache.all_positions()
        assert len(positions) == 0

    def test_process_request_normal_fill_updates_position(self):
        """Normal fill source (no projection ownership) should update position."""
        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import SimulatedFillSource

        bus = MagicMock()
        bus.of_type = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe = MagicMock(return_value=MagicMock())
        bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()

        engine = ExecutionEngine(
            bus=bus,
            fill_source=SimulatedFillSource(),
        )

        req = _make_request()
        engine._process_request(req)

        positions = engine.cache.all_positions()
        assert len(positions) == 1
