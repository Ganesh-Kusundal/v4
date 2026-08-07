"""Tests for MetricsRegistry integration with ExecutionEngine."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine, RiskManager
from tradex_trading.execution.fill_sources import PaperFillSource
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.runtime.metrics import MetricsRegistry


def _make_request(
    instrument: Equity | None = None,
    side: OrderSide = OrderSide.BUY,
    quantity: Decimal = Decimal("10"),
    price: Decimal = Decimal("2500"),
) -> OrderRequest:
    return OrderRequest(
        instrument=instrument or Equity.of("NSE", "RELIANCE"),
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=quantity),
        price=Price(value=price),
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
    )


def _make_engine(
    metrics: MetricsRegistry | None = None,
    risk_manager: RiskManager | None = None,
) -> tuple[ExecutionEngine, ReactiveBus, MetricsRegistry | None]:
    bus = ReactiveBus(metrics=metrics)
    engine = ExecutionEngine(
        bus=bus,
        fill_source=PaperFillSource(),
        risk_manager=risk_manager,
        metrics=metrics,
    )
    return engine, bus, metrics


class TestEngineMetricsNone:
    """When metrics is None (default), no errors occur."""

    def test_submit_without_metrics(self) -> None:
        engine, _, metrics = _make_engine(metrics=None)
        assert metrics is None
        receipt = engine.submit(_make_request())
        assert receipt.status in (OrderStatus.FILLED, OrderStatus.NEW)

    def test_kill_switch_without_metrics(self) -> None:
        engine, _, metrics = _make_engine(metrics=None)
        assert metrics is None
        engine.trip_kill_switch(reason="test")
        assert engine.kill_switch is True


class TestEngineMetricsCounters:
    """Test that metrics counters are incremented correctly."""

    def test_submit_increments_orders_submitted(self) -> None:
        reg = MetricsRegistry()
        engine, _, _ = _make_engine(metrics=reg)
        engine.submit(_make_request())
        assert reg.get("orders.submitted") >= 1

    def test_fill_increments_orders_filled(self) -> None:
        reg = MetricsRegistry()
        engine, _, _ = _make_engine(metrics=reg)
        engine.submit(_make_request())
        # PaperFillSource fills immediately
        assert reg.get("orders.filled") >= 1

    def test_risk_rejection_increments_counters(self) -> None:
        reg = MetricsRegistry()
        risk = RiskManager(max_order_value=Decimal("100"))  # Very low limit
        engine, _, _ = _make_engine(metrics=reg, risk_manager=risk)
        # price=2500 * qty=10 = 25000 > 100 => rejected
        engine.submit(_make_request())
        assert reg.get("orders.rejected") >= 1
        assert reg.get("risk.rejected") >= 1

    def test_kill_switch_trip_increments_counter(self) -> None:
        reg = MetricsRegistry()
        engine, _, _ = _make_engine(metrics=reg)
        engine.trip_kill_switch(reason="test")
        assert reg.get("kill_switch.tripped") == 1

    def test_fill_error_increments_orders_rejected(self) -> None:
        reg = MetricsRegistry()
        bus = ReactiveBus(metrics=reg)

        class FailingFill:
            def submit(self, request: OrderRequest) -> tuple:
                raise RuntimeError("fill error")

        engine = ExecutionEngine(
            bus=bus,
            fill_source=FailingFill(),  # type: ignore[arg-type]
            metrics=reg,
        )
        engine.submit(_make_request())
        assert reg.get("orders.rejected") >= 1


class TestBusMetrics:
    """Test that the bus increments its counter on publish."""

    def test_publish_increments_bus_counter(self) -> None:
        reg = MetricsRegistry()
        bus = ReactiveBus(metrics=reg)
        bus.publish("test_message")
        assert reg.get("bus.messages.published") == 1

    def test_publish_without_metrics(self) -> None:
        bus = ReactiveBus()
        bus.publish("test_message")  # Should not raise
