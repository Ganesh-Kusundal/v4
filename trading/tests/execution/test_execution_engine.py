"""WS-E contract tests: ExecutionEngine spine — ported from v3.

Adapted for v4 API: ReactiveBus required, RiskManager.check() → bool,
submit() returns OrderReceipt (no raise on risk denial),
FillSource.submit() → tuple[Order, Fill | None].
"""

from __future__ import annotations

from decimal import Decimal

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
from tradex_domain.events import ErrorOccurred

from tradex_trading.execution.engine import ExecutionEngine, RiskManager
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _bus() -> ReactiveBus:
    return ReactiveBus()


def _request(correlation_id: CorrelationId | None = None) -> OrderRequest:
    return OrderRequest(
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        correlation_id=correlation_id,
    )


def _make_engine(
    fill_source: object | None = None,
    risk_manager: object | None = None,
    cache: TradingCache | None = None,
    **kwargs: object,
) -> ExecutionEngine:
    bus = _bus()
    return ExecutionEngine(
        bus=bus,
        fill_source=fill_source or SimulatedFillSource(),
        risk_manager=risk_manager,
        cache=cache,
        **kwargs,
    )


def test_live_feed_gate_rejects_before_idempotency_when_not_ready() -> None:
    from tradex_trading.runtime.feed_supervisor import FeedSupervisor

    supervisor = FeedSupervisor()
    engine = _make_engine(feed_supervisor=supervisor)

    receipt = engine.submit(_request(CorrelationId(value="feed-gate")))

    assert receipt.status is OrderStatus.REJECTED
    assert receipt.message == "feed_not_ready"


def test_live_feed_gate_allows_after_recovery() -> None:
    from datetime import UTC, datetime

    from tradex_trading.runtime.feed_supervisor import FeedSupervisor

    supervisor = FeedSupervisor()
    supervisor.connected()
    supervisor.recovery_started()
    supervisor.recovery_succeeded(
        last_event_at=datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
        recovered_through=datetime(2026, 9, 24, 10, 1, tzinfo=UTC),
    )
    engine = _make_engine(feed_supervisor=supervisor)

    receipt = engine.submit(_request(CorrelationId(value="feed-ready")))

    assert receipt.status is OrderStatus.FILLED


def test_feed_gate_does_not_change_default_engine_behavior() -> None:
    receipt = _make_engine().submit(_request())
    assert receipt.status is OrderStatus.FILLED


def test_submit_fills_and_returns_receipt() -> None:
    engine = _make_engine()
    receipt = engine.submit(_request())
    assert receipt.status is OrderStatus.FILLED
    order = engine.cache.get_order(receipt.order_id.value)
    assert order is not None


def test_risk_deny_returns_rejected_receipt() -> None:
    # v4 RiskManager with very low order value limit
    risk = RiskManager(max_order_value=Decimal("1"))
    engine = _make_engine(risk_manager=risk)
    receipt = engine.submit(_request())
    # Order value = 100 * 10 = 1000, exceeds limit of 1
    assert receipt.status is OrderStatus.REJECTED


def test_approving_risk_allows_submit() -> None:
    risk = RiskManager(max_order_value=Decimal("100000"))
    engine = _make_engine(risk_manager=risk)
    receipt = engine.submit(_request())
    assert receipt.status is OrderStatus.FILLED


def test_filled_order_updates_position() -> None:
    cache = TradingCache()
    engine = _make_engine(cache=cache)
    receipt = engine.submit(_request())
    assert receipt.status is OrderStatus.FILLED
    pos = cache.get_position(_eq())
    assert pos is not None
    assert pos.quantity.value == 10


def test_order_store_persists_across_submits() -> None:
    engine = _make_engine()
    engine.submit(_request())
    engine.submit(_request())
    assert len(engine.cache.all_orders()) == 2


def test_kill_switch_rejects_after_trip() -> None:
    engine = _make_engine()
    engine.trip_kill_switch(reason="manual halt")
    receipt = engine.submit(_request())
    assert receipt.status is OrderStatus.REJECTED
    assert engine.kill_switch is True


def test_cache_get_order_returns_none_for_missing() -> None:
    engine = _make_engine()
    assert engine.cache.get_order("nonexistent") is None


def test_pipeline_error_publishes_error_occurred(monkeypatch) -> None:
    """Unexpected pipeline failures surface as ErrorOccurred events.

    Regression guard for the v3 bug where the on_error handler fabricated
    an Order with None fields; the engine must never invent order data.
    """
    bus = _bus()
    engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())

    seen: list[ErrorOccurred] = []
    bus.of_type(ErrorOccurred).subscribe(
        on_next=lambda e: seen.append(e), on_error=lambda e: None,
    )

    def _boom() -> bool:
        raise RuntimeError("pipeline blew up")

    # The pipeline filter reads KillSwitch.active, which delegates to
    # is_set(). Patching the method (not the property) means the filter's
    # lambda raises, proving on_error publishes instead of fabricating an
    # Order — the v3 bug this regression guard exists for.
    monkeypatch.setattr(engine._kill_switch, "is_set", _boom)

    bus.publish(_request())

    assert len(seen) == 1
    assert isinstance(seen[0].error, RuntimeError)
    assert str(seen[0].error) == "pipeline blew up"


def test_submit_latency_histogram_observed() -> None:
    """submit() observes elapsed time to orders.submit_latency_seconds."""
    from tradex_trading.runtime.metrics import MetricsRegistry

    bus = _bus()
    metrics = MetricsRegistry()
    engine = ExecutionEngine(
        bus=bus, fill_source=SimulatedFillSource(), metrics=metrics,
    )
    engine.submit(_request())
    hist = metrics.histogram("orders.submit_latency_seconds")
    assert hist.count == 1
    assert hist.value() > 0.0
