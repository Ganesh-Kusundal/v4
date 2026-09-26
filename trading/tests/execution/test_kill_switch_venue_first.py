"""N4 (P1) — kill-switch cancels venue-first and preserves OMS state on failure.

The principal-quant review (architecture-flow-verification-2026-09-04, N4)
found the kill switch flipped the OMS to CANCELLED before the venue cancel
and only logged failures afterwards. Task 1 made ``engine.cancel`` venue-first
for plain orders (raise before OMS update) and removed the redundant second
venue dispatch from ``trip_kill_switch``; these tests pin the resulting
contract so the ordering can never regress:

  1. a venue-cancel failure for one order leaves THAT order's OMS status
     unchanged (ACK), reports it in ``failures``, and still cancels every
     other open order;
  2. plain orders are venue-cancelled exactly once each;
  3. brackets are super-cancelled exactly once each.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.errors import OrderRejectedError
from tradex_domain.execution import BracketOrderRequest, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import BrokerFillSource
from tradex_trading.execution.idempotency import MemoryIdempotencyGuard
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus


def _instrument() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=_instrument(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
    )


def _bracket_request() -> BracketOrderRequest:
    return BracketOrderRequest(
        instrument=_instrument(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        stop_loss_price=Price(value=Decimal("2400")),
        target_price=Price(value=Decimal("2600")),
    )


class _VenueBroker:
    """Recording venue with per-order cancel failures."""

    owns_position_projection = False

    def __init__(self) -> None:
        self.submitted: list[OrderRequest] = []
        self.cancel_order_calls: list[str] = []
        self.cancel_super_calls: list[str] = []
        self.fail_cancel: set[str] = set()

    def submit_order(self, request: OrderRequest) -> object:
        self.submitted.append(request)
        return f"venue-{len(self.submitted)}"

    def submit_super_order(self, request: BracketOrderRequest) -> object:
        self.submitted.append(request)
        return f"venue-super-{len(self.submitted)}"

    def cancel_order(self, order_id: object) -> None:
        oid = getattr(order_id, "value", str(order_id))
        if oid in self.fail_cancel:
            raise OrderRejectedError(f"venue refused cancel {oid}")
        self.cancel_order_calls.append(oid)

    def cancel_super_order(self, order_id: object) -> None:
        oid = getattr(order_id, "value", str(order_id))
        if oid in self.fail_cancel:
            raise OrderRejectedError(f"venue refused super-cancel {oid}")
        self.cancel_super_calls.append(oid)


def _build_engine(broker: _VenueBroker) -> ExecutionEngine:
    return ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=BrokerFillSource(broker),
        cache=TradingCache(),
        idempotency_guard=MemoryIdempotencyGuard(),
    )


def test_failing_venue_cancel_preserves_that_orders_oms() -> None:
    """Contract 1: the failed order stays ACK, others cancel, failure reported."""
    broker = _VenueBroker()
    engine = _build_engine(broker)

    r1 = engine.submit(_request())
    r2 = engine.submit(_request())
    assert r1.status is OrderStatus.ACK and r2.status is OrderStatus.ACK
    broker.fail_cancel.add(r1.order_id.value)

    failures = engine.trip_kill_switch()

    assert failures == [r1.order_id.value]
    failed = engine.cache.get_order(r1.order_id.value)
    assert failed is not None and failed.status is OrderStatus.ACK, (
        "OMS must not claim CANCELLED when the venue refused the cancel"
    )
    ok = engine.cache.get_order(r2.order_id.value)
    assert ok is not None and ok.status is OrderStatus.CANCELLED
    assert engine.kill_switch is True
    # The venue was attempted for both orders; the failing one raised before
    # recording, so only the successful cancel is logged.
    assert broker.cancel_order_calls == [r2.order_id.value]


def test_plain_orders_venue_cancelled_exactly_once() -> None:
    """Contract 2: no double venue dispatch during a kill switch."""
    broker = _VenueBroker()
    engine = _build_engine(broker)

    r1 = engine.submit(_request())
    r2 = engine.submit(_request())

    failures = engine.trip_kill_switch()

    assert failures == []
    assert sorted(broker.cancel_order_calls) == sorted(
        [r1.order_id.value, r2.order_id.value]
    )
    assert broker.cancel_super_calls == []


def test_bracket_and_plain_cancel_via_their_own_endpoints_once() -> None:
    """Contract 3: brackets super-cancel once, plains plain-cancel once."""
    broker = _VenueBroker()
    engine = _build_engine(broker)

    bracket = engine.submit(_bracket_request())
    plain = engine.submit(_request())

    failures = engine.trip_kill_switch()

    assert failures == []
    assert broker.cancel_super_calls == [bracket.order_id.value]
    assert broker.cancel_order_calls == [plain.order_id.value]
    assert engine.cache.get_order(bracket.order_id.value).status is OrderStatus.CANCELLED
    assert engine.cache.get_order(plain.order_id.value).status is OrderStatus.CANCELLED
