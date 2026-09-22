"""Bracket orders through the ExecutionEngine pipeline.

Regression guard for architecture review B2: a bracket (super) order must not
bypass the engine — idempotency (same correlation id ⇒ one broker mutation,
original id replayed) and risk (a bracket over the notional cap is rejected
without reaching the broker) apply exactly as they do for plain orders. The
live fill source dispatches the composite to the broker's super-order endpoint.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.errors import OrderRejectedError
from tradex_domain.execution import BracketOrderRequest, OrderReceipt, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

from tradex_trading.execution.engine import (
    ExecutionEngine,
    MemoryIdempotencyGuard,
    RiskManager,
)
from tradex_trading.execution.fill_sources import BrokerFillSource
from tradex_trading.reactive.bus import ReactiveBus


class _SuperBroker:
    """Minimal venue adapter exposing only the super-order endpoint."""

    def __init__(self) -> None:
        self.submitted: list[BracketOrderRequest] = []
        self.cancelled: list[OrderId] = []
        self.modified: list[tuple[OrderId, BracketOrderRequest]] = []

    def submit_super_order(self, request: BracketOrderRequest) -> OrderId:
        self.submitted.append(request)
        return OrderId(value="super-1")

    def cancel_super_order(self, order_id: OrderId) -> None:
        self.cancelled.append(order_id)

    def modify_super_order(
        self, order_id: OrderId, request: BracketOrderRequest
    ) -> None:
        self.modified.append((order_id, request))


def _bracket_request(cid: str) -> BracketOrderRequest:
    return BracketOrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500.00")),
        stop_loss_price=Price(value=Decimal("2450.00")),
        target_price=Price(value=Decimal("2600.00")),
        time_in_force=TimeInForce.DAY,
        correlation_id=CorrelationId(value=cid),
    )


def _engine(broker: _SuperBroker, *, risk: RiskManager | None = None) -> ExecutionEngine:
    return ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=BrokerFillSource(broker),
        risk_manager=risk,
        idempotency_guard=MemoryIdempotencyGuard(),
    )


def test_bracket_dedup_single_broker_mutation_and_replay() -> None:
    """One correlation id ⇒ exactly one super-order submission; the retry
    replays the original OrderId and creates no second OMS record."""
    broker = _SuperBroker()
    engine = _engine(broker)

    receipt = engine.submit(_bracket_request("bracket-cid-1"))
    assert isinstance(receipt, OrderReceipt)
    assert receipt.order_id.value == "super-1"
    assert receipt.status == OrderStatus.ACK
    assert len(broker.submitted) == 1
    assert len(engine.all_orders()) == 1
    assert broker.submitted[0].correlation_id == CorrelationId(value="bracket-cid-1")

    replay = engine.submit(_bracket_request("bracket-cid-1"))
    assert isinstance(replay, OrderId)
    assert replay.value == "super-1"
    assert len(broker.submitted) == 1  # no second mutation
    assert len(engine.all_orders()) == 1  # no second OMS record


def test_bracket_survives_restart_and_still_super_cancels(tmp_path) -> None:
    """A bracket mirrored into SQLite keeps its protective legs across a
    process restart, so engine.cancel on the recovered order still reaches
    the venue's cancel_super_order instead of the plain cancel."""
    from tradex_trading.execution.sqlite_store import attach_order_persistence
    from tradex_trading.execution.sqlite_store import SQLiteOrderStore

    db = str(tmp_path / "bracket-restart.db")

    # Engine 1 — place the bracket; the persistence mirror writes it (legs
    # included) to SQLite when OrderPlaced is published.
    broker1 = _SuperBroker()
    bus1 = ReactiveBus()
    engine1 = ExecutionEngine(
        bus=bus1,
        fill_source=BrokerFillSource(broker1),
        idempotency_guard=MemoryIdempotencyGuard(),
    )
    store = SQLiteOrderStore(db)
    attach_order_persistence(bus1, engine1.cache, store)
    try:
        receipt = engine1.submit(_bracket_request("bracket-cid-restart"))
        assert isinstance(receipt, OrderReceipt)
    finally:
        engine1.shutdown()
        store.close()

    # Engine 2 — "process restart": recover the cached order from SQLite.
    broker2 = _SuperBroker()
    engine2 = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=BrokerFillSource(broker2),
    )
    recovered_store = SQLiteOrderStore(db)
    try:
        recovered_store.load_into(engine2.cache)
        recovered = engine2.cache.get_order("super-1")
        assert recovered is not None
        # Legs survived the round trip -> the order is still a bracket.
        assert recovered.stop_loss_price is not None
        assert recovered.target_price is not None

        engine2.cancel(OrderId(value="super-1"))
        assert broker2.cancelled == [OrderId(value="super-1")]
        cached = engine2.cache.get_order("super-1")
        assert cached is not None
        assert cached.status is OrderStatus.CANCELLED
    finally:
        engine2.shutdown()
        recovered_store.close()


def test_bracket_modify_after_restart_reaches_super_endpoint(tmp_path) -> None:
    """A bracket recovered from SQLite keeps its protective legs, so
    engine.modify on the reloaded order still dispatches the composite to
    the venue's modify_super_order and projects the new legs onto the OMS.
    """
    from tradex_trading.execution.sqlite_store import attach_order_persistence
    from tradex_trading.execution.sqlite_store import SQLiteOrderStore

    db = str(tmp_path / "bracket-modify-restart.db")

    # Engine 1 — place the bracket; the persistence mirror writes it (legs
    # included) to SQLite when OrderPlaced is published.
    broker1 = _SuperBroker()
    bus1 = ReactiveBus()
    engine1 = ExecutionEngine(
        bus=bus1,
        fill_source=BrokerFillSource(broker1),
        idempotency_guard=MemoryIdempotencyGuard(),
    )
    store = SQLiteOrderStore(db)
    attach_order_persistence(bus1, engine1.cache, store)
    try:
        receipt = engine1.submit(_bracket_request("bracket-cid-modify-restart"))
        assert isinstance(receipt, OrderReceipt)
    finally:
        engine1.shutdown()
        store.close()

    # Engine 2 — "process restart": recover the cached order from SQLite.
    broker2 = _SuperBroker()
    engine2 = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=BrokerFillSource(broker2),
    )
    recovered_store = SQLiteOrderStore(db)
    try:
        recovered_store.load_into(engine2.cache)
        recovered = engine2.cache.get_order("super-1")
        assert recovered is not None
        # Legs survived the round trip -> the order is still a bracket.
        assert recovered.stop_loss_price == Price(value=Decimal("2450.00"))
        assert recovered.target_price == Price(value=Decimal("2600.00"))

        updated = BracketOrderRequest(
            instrument=Equity.of("NSE", "RELIANCE"),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("2510.00")),
            stop_loss_price=Price(value=Decimal("2460.00")),
            target_price=Price(value=Decimal("2650.00")),
            time_in_force=TimeInForce.DAY,
            trailing_jump=Price(value=Decimal("10")),
        )
        engine2.modify(OrderId(value="super-1"), updated)

        assert broker2.modified == [(OrderId(value="super-1"), updated)]
        cached = engine2.cache.get_order("super-1")
        assert cached is not None
        assert cached.price == Price(value=Decimal("2510.00"))
        assert cached.stop_loss_price == Price(value=Decimal("2460.00"))
        assert cached.target_price == Price(value=Decimal("2650.00"))
    finally:
        engine2.shutdown()
        recovered_store.close()


def test_bracket_different_keys_are_distinct() -> None:
    """Different correlation ids are independent submissions."""
    broker = _SuperBroker()
    engine = _engine(broker)

    first = engine.submit(_bracket_request("bracket-cid-a"))
    second = engine.submit(_bracket_request("bracket-cid-b"))
    assert isinstance(first, OrderReceipt)
    assert isinstance(second, OrderReceipt)
    assert len(broker.submitted) == 2


def test_bracket_cancel_reaches_venue_super_endpoint_and_oms() -> None:
    """engine.cancel on a bracket cancels the composite at the venue via
    cancel_super_order and flips the OMS record to CANCELLED."""
    broker = _SuperBroker()
    engine = _engine(broker)

    receipt = engine.submit(_bracket_request("bracket-cid-cancel"))
    assert isinstance(receipt, OrderReceipt)
    engine.cancel(receipt.order_id)

    assert broker.cancelled == [OrderId(value="super-1")]
    cached = engine.cache.get_order("super-1")
    assert cached is not None
    assert cached.status is OrderStatus.CANCELLED


def test_bracket_modify_reaches_venue_super_endpoint_and_projects_legs() -> None:
    """engine.modify on a bracket sends the full composite to
    modify_super_order and projects the new protective legs onto the OMS."""
    broker = _SuperBroker()
    engine = _engine(broker)

    receipt = engine.submit(_bracket_request("bracket-cid-modify"))
    assert isinstance(receipt, OrderReceipt)

    updated = BracketOrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2510.00")),
        stop_loss_price=Price(value=Decimal("2460.00")),
        target_price=Price(value=Decimal("2650.00")),
        time_in_force=TimeInForce.DAY,
        trailing_jump=Price(value=Decimal("10")),
    )
    modified = engine.modify(receipt.order_id, updated)

    assert broker.modified == [(OrderId(value="super-1"), updated)]
    cached = engine.cache.get_order("super-1")
    assert cached is not None
    assert cached.price == Price(value=Decimal("2510.00"))
    assert cached.stop_loss_price == Price(value=Decimal("2460.00"))
    assert cached.target_price == Price(value=Decimal("2650.00"))
    assert cached.trailing_jump == Price(value=Decimal("10"))
    assert modified.target_price == Price(value=Decimal("2650.00"))


def test_bracket_modify_refuses_plain_request() -> None:
    """A plain modification request on a bracket order is refused before the
    venue is touched — the composite must never reach modify_order."""
    broker = _SuperBroker()
    engine = _engine(broker)

    receipt = engine.submit(_bracket_request("bracket-cid-plain-modify"))
    assert isinstance(receipt, OrderReceipt)

    plain = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("12")),
        price=Price(value=Decimal("2550.00")),
        time_in_force=TimeInForce.DAY,
    )
    with pytest.raises(OrderRejectedError, match="is a bracket"):
        engine.modify(receipt.order_id, plain)
    assert broker.modified == []


def test_bracket_risk_rejection_never_reaches_broker_and_releases_key() -> None:
    """A bracket over the notional cap is refused by the risk gate before the
    broker is touched, and the correlation id is released (a retry re-checks
    rather than tripping the reserved-key guard)."""
    broker = _SuperBroker()
    risk = RiskManager(max_order_value=Decimal("100"))  # 10 x 2500 ≫ 100
    engine = _engine(broker, risk=risk)

    request = _bracket_request("bracket-cid-risky")
    receipt = engine.submit(request)
    assert isinstance(receipt, OrderReceipt)
    assert receipt.status == OrderStatus.REJECTED
    assert receipt.message == "risk_check_failed"
    assert broker.submitted == []

    # The reservation was released on the risk rejection — a retry must not
    # raise MemoryIdempotencyGuard's already-reserved RuntimeError.
    retry = engine.submit(request)
    assert isinstance(retry, OrderReceipt)
    assert retry.status == OrderStatus.REJECTED
    assert broker.submitted == []
