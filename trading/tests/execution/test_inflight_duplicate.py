"""N5 (P2) — a retry on a reserved-but-incomplete idempotency key gets a
deterministic in-flight receipt (and a 409 at the HTTP edge), never an
opaque 500. The reservation stays owned by the original request.

The principal-quant review (architecture-flow-verification-2026-09-04, N5)
found the guard's ``RuntimeError("already reserved")`` escaped as a 500, so
a client could not distinguish "still processing" from failure.
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.execution import Order, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

from tradex_trading.execution.engine import (
    ExecutionEngine,
    IdempotencyDuplicate,
    IdempotencyInflight,
    MemoryIdempotencyGuard,
)
from tradex_domain.execution import OrderReceipt

from tradex_trading.execution.fill_sources import BrokerFillSource
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus


def _request(correlation_id: str | None = None) -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        correlation_id=(
            CorrelationId(correlation_id) if correlation_id is not None else None
        ),
    )


class _VenueBroker:
    """Recording venue."""

    owns_position_projection = False

    def __init__(self) -> None:
        self.submit_calls: list[OrderRequest] = []

    def submit_order(self, request: OrderRequest) -> object:
        self.submit_calls.append(request)
        return "venue-1"


def _build_engine(
    broker: _VenueBroker,
    *,
    guard: object | None = None,
) -> ExecutionEngine:
    return ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=BrokerFillSource(broker),
        cache=TradingCache(),
        idempotency_guard=guard if guard is not None else MemoryIdempotencyGuard(),
    )


def test_inflight_submit_returns_deterministic_receipt() -> None:
    """Contract 1: in-flight key → receipt, no fill-source call, no exception."""
    broker = _VenueBroker()
    engine = _build_engine(broker)
    cid = CorrelationId("inflight-1")
    engine._guard.check_and_reserve(cid)  # noqa: SLF001 — simulate in-flight first req

    receipt = engine.submit(_request(correlation_id="inflight-1"))

    assert receipt.message == "idempotency_in_flight"
    assert receipt.status is OrderStatus.PENDING
    assert broker.submit_calls == []


def test_inflight_key_stays_owned_then_replays_after_completion() -> None:
    """Contract 2: reservation survives the receipt; completion enables replay."""
    broker = _VenueBroker()
    engine = _build_engine(broker)
    cid = CorrelationId("inflight-2")
    engine._guard.check_and_reserve(cid)  # noqa: SLF001

    receipt = engine.submit(_request(correlation_id="inflight-2"))
    assert receipt.message == "idempotency_in_flight"
    # Still owned by the original request — not released by the dedup path.
    with pytest.raises(IdempotencyInflight):
        engine._guard.check_and_reserve(cid)  # noqa: SLF001

    # Original completes → a retry now replays, never double-submits.
    engine._guard.record_result(cid, "orig-order-id")  # noqa: SLF001
    dup = engine._guard.check_and_reserve(cid)  # noqa: SLF001
    assert isinstance(dup, IdempotencyDuplicate)
    assert broker.submit_calls == []


def test_route_maps_inflight_receipt_to_409() -> None:
    """Contract 3: the HTTP edge answers 409, not 500."""
    from fastapi import HTTPException
    from fastapi.testclient import TestClient

    from tradex_trading.interface.fastapi_app import create_app

    class _FakeSession:
        engine = None
        _broker = None

        def __init__(self, engine: object) -> None:
            self.engine = engine

    class _FakeEngine:
        def submit(self, request: object) -> object:
            return OrderReceiptShim()

    class OrderReceiptShim:
        message = "idempotency_in_flight"
        status = OrderStatus.PENDING
        order_id = OrderId("pending")

    client = TestClient(create_app(session=_FakeSession(_FakeEngine())))
    resp = client.post(
        "/orders",
        json={
            "exchange": "NSE",
            "symbol": "RELIANCE",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": "10",
            "price": "2500",
        },
        headers={"Idempotency-Key": "k-1"},
    )
    assert resp.status_code == 409
    assert "being processed" in resp.json()["error"]["message"]


def test_sqlite_guard_inflight_yields_receipt_not_500() -> None:
    """Contract 4: durable guard path behaves identically."""
    import tempfile
    from pathlib import Path

    from tradex_trading.execution.sqlite_store import SQLiteIdempotencyGuard

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "orders.db"
        broker = _VenueBroker()
        guard = SQLiteIdempotencyGuard(db)
        engine = _build_engine(broker, guard=guard)
        cid = CorrelationId("inflight-3")
        guard.check_and_reserve(cid)

        receipt = engine.submit(_request(correlation_id="inflight-3"))

        assert receipt.message == "idempotency_in_flight"
        assert broker.submit_calls == []


def test_concurrent_submits_make_exactly_one_venue_call() -> None:
    """Contract 5: racing retries cannot double-submit at the venue."""
    broker = _VenueBroker()
    engine = _build_engine(broker)
    results: list[object] = []
    lock = threading.Lock()

    def _submit() -> None:
        receipt = engine.submit(_request(correlation_id="inflight-4"))
        with lock:
            results.append(receipt)

    threads = [threading.Thread(target=_submit) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(broker.submit_calls) == 1
    assert len(results) == 4
    # Every attempt resolved deterministically: an OrderReceipt (submitted
    # or in-flight) or a completed-key replay (OrderId). Nothing raised —
    # no RuntimeError escaping as a 500.
    for r in results:
        assert isinstance(r, (OrderId, OrderReceipt))