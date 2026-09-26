"""N2 (P1) — modify and cancel must reserve, replay, and conflict on their
idempotency keys like submits do.

The principal-quant review (architecture-flow-verification-2026-09-04, N2)
found PUT/DELETE idempotency keys were validated but never reached the
guard: a double-fired PUT dispatched the venue modify twice, and keys were
never bound to a request hash.

These tests pin the fixed contract:

  1. duplicate modify (same cid + same payload): venue modify called once,
     the retry replays the original modified Order;
  2. same cid + materially different payload: ``IdempotencyKeyReuseMismatch``,
     venue untouched by the retry;
  3. duplicate cancel (same cid): venue cancel called once, the retry
     replays the cancelled Order;
  4. a risk-denied modify releases the cid (same cid + payload succeeds
     once the policy permits);
  5. the SQLite guard replays a completed modify across guard instances
     on the same database file.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine, RiskManager
from tradex_trading.execution.fill_sources import BrokerFillSource
from tradex_trading.execution.idempotency import (
    IdempotencyKeyReuseMismatch,
    MemoryIdempotencyGuard,
)
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus


def _instrument() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request(
    price: str = "2500",
    qty: str = "10",
    correlation_id: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        instrument=_instrument(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
        correlation_id=(
            CorrelationId(correlation_id) if correlation_id is not None else None
        ),
    )


class _VenueBroker:
    """Recording venue with optional modify/cancel failures."""

    owns_position_projection = False

    def __init__(self) -> None:
        self.modify_calls: list[tuple[str, OrderRequest]] = []
        self.cancel_calls: list[str] = []
        self.fail_modify: Exception | None = None
        self.fail_cancel: Exception | None = None

    def submit_order(self, request: OrderRequest) -> object:
        return "venue-1"

    def modify_order(self, order_id: object, request: OrderRequest) -> None:
        oid = getattr(order_id, "value", str(order_id))
        if self.fail_modify is not None:
            raise self.fail_modify
        self.modify_calls.append((oid, request))

    def cancel_order(self, order_id: object) -> None:
        oid = getattr(order_id, "value", str(order_id))
        if self.fail_cancel is not None:
            raise self.fail_cancel
        self.cancel_calls.append(oid)


def _build_engine(
    broker: _VenueBroker,
    *,
    risk: RiskManager | None = None,
    guard: object | None = None,
) -> ExecutionEngine:
    return ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=BrokerFillSource(broker),
        cache=TradingCache(),
        risk_manager=risk,
        idempotency_guard=guard if guard is not None else MemoryIdempotencyGuard(),
    )


def _place_ack(engine: ExecutionEngine) -> str:
    """Place an ACK'd order and return its engine order id."""
    receipt = engine.submit(_request(correlation_id="submit-cid"))
    assert receipt.status is OrderStatus.ACK
    return receipt.order_id.value


def test_duplicate_modify_replays_without_second_venue_call() -> None:
    """Contract 1: one venue modify; the retry replays the original Order."""
    broker = _VenueBroker()
    engine = _build_engine(broker)
    oid = _place_ack(engine)

    req = _request(price="2510", correlation_id="mod-cid")
    first = engine.modify(OrderId(oid), req)
    second = engine.modify(OrderId(oid), req)

    assert len(broker.modify_calls) == 1
    assert second is first
    assert second.price.value == Decimal("2510")


def test_modify_cid_reuse_with_different_payload_conflicts() -> None:
    """Contract 2: same cid + different payload → mismatch, venue untouched."""
    broker = _VenueBroker()
    engine = _build_engine(broker)
    oid = _place_ack(engine)

    req_a = _request(price="2510", correlation_id="mod-cid")
    engine.modify(OrderId(oid), req_a)

    req_b = _request(price="2600", correlation_id="mod-cid")
    with pytest.raises(IdempotencyKeyReuseMismatch):
        engine.modify(OrderId(oid), req_b)
    assert len(broker.modify_calls) == 1  # retry never reached the venue


def test_duplicate_cancel_replays_without_second_venue_call() -> None:
    """Contract 3: one venue cancel; the retry replays the cancelled Order."""
    broker = _VenueBroker()
    engine = _build_engine(broker)
    oid = _place_ack(engine)
    cid = CorrelationId("cancel-cid")

    first = engine.cancel(OrderId(oid), correlation_id=cid)
    second = engine.cancel(OrderId(oid), correlation_id=cid)

    assert broker.cancel_calls == [oid]
    assert second is first
    assert first.status is OrderStatus.CANCELLED


def test_risk_denied_modify_releases_cid() -> None:
    """Contract 4: a denied modify frees the key for a later approved retry."""
    broker = _VenueBroker()
    engine = _build_engine(broker)
    oid = _place_ack(engine)

    # Bind risk AFTER the order is placed so the submit isn't caught by it.
    risk = RiskManager(max_position_value=Decimal("1000"))
    engine._risk = risk  # noqa: SLF001

    big = _request(price="100", qty="100", correlation_id="mod-cid")  # 10k notional
    from tradex_domain.errors import OrderRejectedError

    with pytest.raises(OrderRejectedError):
        engine.modify(OrderId(oid), big)

    # Policy loosened — the same cid + payload must now succeed (not "reserved").
    risk._max_position_value = Decimal("100000")  # noqa: SLF001
    retry = engine.modify(OrderId(oid), big)
    assert retry.quantity.value == Decimal("100")
    assert len(broker.modify_calls) == 1


def test_sqlite_guard_replays_modify_across_instances() -> None:
    """Contract 5: completed modify key survives a guard rebuild on the DB."""
    import tempfile
    from pathlib import Path

    from tradex_trading.execution.sqlite_store import SQLiteIdempotencyGuard

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "orders.db"
        broker1 = _VenueBroker()
        engine1 = _build_engine(broker1, guard=SQLiteIdempotencyGuard(db))
        oid = _place_ack(engine1)
        req = _request(price="2510", correlation_id="mod-cid")
        engine1.modify(OrderId(oid), req)

        broker2 = _VenueBroker()
        engine2 = _build_engine(broker2, guard=SQLiteIdempotencyGuard(db))
        # Rebuilt engine + guard on the same DB: the retry must replay.
        replayed = engine2.modify(OrderId(oid), req)
        assert replayed.price.value == Decimal("2510")
        assert broker2.modify_calls == []
