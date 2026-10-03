"""End-to-end live order: request → engine → broker → event store → replay.

The seam a paper/live session runs: an ``OrderRequest`` reaches the venue
through ``ExecutionEngine.submit``, and the durable ``SQLiteEventStore``
records what happened so a later replay reconstructs it.

The composition is the *production* one, not a convenient stub:

* ``PaperBroker`` stands in for a real adapter. A live test would need
  credentials; the property under test is the engine↔adapter contract, which
  ``PaperBroker`` satisfies as a real implementation.
* ``BrokerFillSource(PaperBroker)`` is exactly what ``_compose_fill_source``
  builds for ``mode="live"`` (``runtime/src/tradex_runtime/startup.py:234``).
* ``SQLiteEventStore`` is a real file-backed store with a real hash chain.

Three behaviours this file pins, each of which a first draft got wrong by
assuming the convenient case:

**Live submit is ACK-only.** ``BrokerFillSource.submit`` returns
``(order, None)`` — an ACK and *no fill* (``fill_sources.py:334-336``) — because
a real venue acknowledges before it fills. The fill arrives later on the
inbound bridge. A test asserting ``submit`` produces a fill would pass against
``SimulatedFillSource`` and prove nothing about live mode.

**Who owns the position projection decides who books the fill.**
``PaperBroker.owns_position_projection`` is ``True`` by default, and
``_apply_fill`` then *deliberately skips* applying it (``engine.py:1161``,
gated on ``not owns_projection``) — the broker already did. Both branches are
covered below, because which one runs is a property of the adapter wiring, not
an incidental detail.

**A risk rejection is a receipt, not an exception.** The gate publishes
``OrderRejected``, appends it durably, and returns
``OrderReceipt(status=REJECTED)`` (``engine.py:634-645``). A bare raise would
strand the idempotency reservation, so the refusal is deliberately a value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from tradex_brokers.paper.adapter import PaperBroker
from tradex_domain import Equity, OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.enums import ProductType
from tradex_domain.events import OrderFilled, OrderPlaced, OrderRejected
from tradex_domain.execution import Fill, OrderRequest
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import BrokerFillSource
from tradex_execution.risk import RiskManager
from tradex_execution.sqlite_event_store import SQLiteEventStore
from tradex_execution.trading_cache import TradingCache
from tradex_reactive.bus import ReactiveBus

#: Working capital for the engine's cash ledger.
CASH = Decimal("500000")
#: The risk manager's per-order ceiling. Deliberately below CASH so the gate
#: is the binding constraint and an oversized order cannot be masked by funds.
ORDER_CEILING = Decimal("100000")
#: A resting LIMIT, well below any price the paper tape quotes, so the order
#: is accepted and left unfilled — the state a live submit actually reaches.
RESTING_PRICE = Decimal("50.00")


@pytest.fixture
def instrument() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _compose(broker: PaperBroker, store: SQLiteEventStore):
    """The live composition, returning ``(engine, cache)``."""
    cache = TradingCache()
    engine = ExecutionEngine(
        ReactiveBus(),
        BrokerFillSource(broker),
        risk_manager=RiskManager(max_order_value=ORDER_CEILING),
        cache=cache,
        cash=CASH,
        event_store=store,
    )
    return engine, cache


@pytest.fixture
def broker() -> PaperBroker:
    """A paper broker that owns its own position projection (the default)."""
    b = PaperBroker()
    b.connect()
    yield b
    b.close()


@pytest.fixture
def store(tmp_path: Path) -> SQLiteEventStore:
    s = SQLiteEventStore(tmp_path / "events.db")
    yield s
    s.close()


def _request(
    instrument: Equity,
    *,
    side: OrderSide = OrderSide.BUY,
    qty: int = 10,
    price: Decimal = RESTING_PRICE,
    correlation: str = "corr-1",
) -> OrderRequest:
    return OrderRequest(
        instrument=instrument,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal(qty)),
        price=Price(price),
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
        correlation_id=CorrelationId(correlation),
        strategy_id="e2e-live",
        reference_timestamp=datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
    )


def _venue_fill(
    order_id: OrderId, request: OrderRequest, *, fill_id: str, qty: int = 10
) -> Fill:
    """A venue-reported fill for *order_id* — what the inbound bridge receives."""
    return Fill(
        order_id=order_id,
        instrument=request.instrument,
        side=request.side,
        quantity=Quantity(Decimal(qty)),
        price=Price(request.price.value),
        timestamp=datetime(2026, 7, 1, 9, 16, tzinfo=UTC),
        product_type=request.product_type,
        strategy_id=request.strategy_id,
        fill_id=fill_id,
    )


class TestSubmitReachesTheBroker:
    """The submit leg: request in, order at the venue, receipt back."""

    def test_submit_places_an_order_the_broker_can_find(
        self, broker: PaperBroker, store: SQLiteEventStore, instrument: Equity
    ) -> None:
        """The order the receipt names is retrievable from the broker."""
        engine, _cache = _compose(broker, store)
        try:
            receipt = engine.submit(_request(instrument))
            assert receipt.order_id.value, "the receipt carried no order id"

            at_broker = broker.get_order(receipt.order_id)
            assert at_broker is not None, (
                "the receipt names an order the broker never received"
            )
            assert at_broker.instrument == instrument
            assert at_broker.side is OrderSide.BUY
            assert at_broker.quantity.value == Decimal(10)
        finally:
            engine.shutdown()

    def test_submit_is_an_ack_not_a_fill(
        self, broker: PaperBroker, store: SQLiteEventStore, instrument: Equity
    ) -> None:
        """Live submit acknowledges; it does not report a trade.

        This is the assertion that keeps the test honest about which fill source
        it exercises. A synchronous fill here would mean ``BrokerFillSource``
        had silently become a simulated one.
        """
        engine, _cache = _compose(broker, store)
        try:
            receipt = engine.submit(_request(instrument))
            assert receipt.status is OrderStatus.ACK

            at_broker = broker.get_order(receipt.order_id)
            assert not at_broker.is_filled, (
                "the paper tape crossed a resting LIMIT that should not trade"
            )
        finally:
            engine.shutdown()

    def test_one_submit_yields_one_order_at_the_venue(
        self, broker: PaperBroker, store: SQLiteEventStore, instrument: Equity
    ) -> None:
        """A single submit dispatches a single order, not a retry storm."""
        engine, _cache = _compose(broker, store)
        try:
            receipt = engine.submit(_request(instrument))
            held = [o for o in broker.get_order_list() if o.instrument == instrument]
            assert len(held) == 1, f"{len(held)} orders reached the venue for one submit"
            assert held[0].order_id.value == receipt.order_id.value
        finally:
            engine.shutdown()


class TestProjectionOwnership:
    """Who books the fill is decided by the adapter, and both ways are wired."""

    def test_a_projection_owning_broker_books_no_local_position(
        self, broker: PaperBroker, store: SQLiteEventStore, instrument: Equity
    ) -> None:
        """With the default broker, the engine defers booking to the adapter.

        ``PaperBroker.owns_position_projection`` is True, so ``_apply_fill``
        skips its own booking — double-booking would be the bug. The fill is
        still durable, which is the property that survives the deferral.
        """
        assert broker.owns_position_projection, "the default wiring changed"
        engine, cache = _compose(broker, store)
        try:
            request = _request(instrument)
            receipt = engine.submit(request)
            engine.ingest_fill(_venue_fill(receipt.order_id, request, fill_id="T1"))

            assert cache.all_positions() == [], (
                "the engine booked a position its own adapter already owns"
            )
            assert any(
                isinstance(e, OrderFilled) for e in store.replay("orders")
            ), "the deferred fill was not recorded"
        finally:
            engine.shutdown()

    def test_an_engine_owning_broker_books_the_position_and_moves_cash(
        self, broker: PaperBroker, store: SQLiteEventStore, instrument: Equity
    ) -> None:
        """With ``project_positions=False`` the engine books and debits itself.

        The mirror of the case above, and the reason the flag exists: an
        adapter that does not maintain its own book must leave the engine to
        do it, or the position simply never appears.
        """
        broker._project_positions = False  # noqa: SLF001
        assert not broker.owns_position_projection

        engine, cache = _compose(broker, store)
        try:
            request = _request(instrument, qty=10)
            receipt = engine.submit(request)
            engine.ingest_fill(_venue_fill(receipt.order_id, request, fill_id="T1"))

            positions = cache.all_positions()
            assert len(positions) == 1, "a fill booked no position"
            assert positions[0].quantity.value == Decimal(10)
            assert positions[0].avg_price.value == RESTING_PRICE

            # 10 shares at 50.00 leaves the ledger 500 short of its opening cash.
            assert engine.cash_snapshot().cash == CASH - (Decimal(10) * RESTING_PRICE)
        finally:
            engine.shutdown()


class TestInboundFillIsAppliedOnce:
    """The dedup contract on the bridge."""

    @pytest.fixture
    def engine_book(self, broker: PaperBroker, store: SQLiteEventStore):
        """An engine that owns its book, so fills are observable in it."""
        broker._project_positions = False  # noqa: SLF001
        engine, cache = _compose(broker, store)
        yield engine, cache
        engine.shutdown()

    def test_a_re_delivered_fill_books_once(
        self, engine_book, instrument: Equity
    ) -> None:
        """A broker retrying its stream cannot inflate the book.

        Without the dedup this is 20 shares from a 10-share order.
        """
        engine, cache = engine_book
        request = _request(instrument, qty=10)
        receipt = engine.submit(request)

        fill = _venue_fill(receipt.order_id, request, fill_id="T1", qty=10)
        engine.ingest_fill(fill)
        engine.ingest_fill(fill)

        assert cache.all_positions()[0].quantity.value == Decimal(10), (
            "a re-delivered fill was applied twice"
        )

    def test_two_distinct_fills_both_apply(self, engine_book, instrument: Equity) -> None:
        """Distinct fill ids are distinct trades and both land.

        The mirror of the dedup test: keying only on
        ``(order, side, qty, price)`` would swallow a genuine second partial
        fill that happened to match the first.
        """
        engine, cache = engine_book
        request = _request(instrument, qty=20)
        receipt = engine.submit(request)

        engine.ingest_fill(_venue_fill(receipt.order_id, request, fill_id="T1", qty=10))
        engine.ingest_fill(_venue_fill(receipt.order_id, request, fill_id="T2", qty=10))

        assert cache.all_positions()[0].quantity.value == Decimal(20), (
            "a genuine second partial fill was swallowed by the dedup"
        )


class TestDurableEventLog:
    """The record leg: what a later replay reads back."""

    def test_the_submitted_order_is_replayable(
        self, broker: PaperBroker, store: SQLiteEventStore, instrument: Equity
    ) -> None:
        """A submitted order is recoverable from the event store alone."""
        engine, _cache = _compose(broker, store)
        try:
            receipt = engine.submit(_request(instrument))
            replayed = list(store.replay("orders"))
            placed = [e for e in replayed if isinstance(e, OrderPlaced)]

            assert placed, f"no OrderPlaced among {[type(e).__name__ for e in replayed]}"
            assert any(
                p.order.order_id.value == receipt.order_id.value for p in placed
            ), "the replayed log does not contain the order that was submitted"
        finally:
            engine.shutdown()

    def test_the_fill_is_replayable_with_its_price_and_quantity(
        self, broker: PaperBroker, store: SQLiteEventStore, instrument: Equity
    ) -> None:
        """The inbound fill lands in the log, not only in the book."""
        engine, _cache = _compose(broker, store)
        try:
            request = _request(instrument, qty=10)
            receipt = engine.submit(request)
            engine.ingest_fill(_venue_fill(receipt.order_id, request, fill_id="T1", qty=10))

            filled = [e for e in store.replay("orders") if isinstance(e, OrderFilled)]
            match = [f for f in filled if f.fill.order_id.value == receipt.order_id.value]

            assert match, "a fill was ingested but never recorded"
            assert match[0].fill.quantity.value == Decimal(10)
            assert match[0].fill.price.value == RESTING_PRICE
        finally:
            engine.shutdown()

    def test_the_chain_verifies_after_a_full_round_trip(
        self, broker: PaperBroker, store: SQLiteEventStore, instrument: Equity
    ) -> None:
        """Submit + fill leaves a store whose hash chain still verifies.

        ``replay`` runs the chain check, so reading the log back at all proves
        it — a broken chain raises instead of returning rows.
        """
        engine, _cache = _compose(broker, store)
        try:
            request = _request(instrument)
            receipt = engine.submit(request)
            engine.ingest_fill(_venue_fill(receipt.order_id, request, fill_id="T1"))

            assert list(store.replay("orders")), "a verified chain returned nothing"
        finally:
            engine.shutdown()


class TestRiskGateIsInFront:
    """The gate leg: an oversized order is refused before dispatch."""

    def test_an_oversized_order_returns_a_rejected_receipt(
        self, broker: PaperBroker, store: SQLiteEventStore, instrument: Equity
    ) -> None:
        """10,000 x 100.00 = 1,000,000, ten times the ceiling.

        The refusal is a *receipt*, not an exception: the idempotency
        reservation is already held at this point, and raising would strand the
        correlation id so the caller's retry would be skipped as a duplicate.
        """
        engine, _cache = _compose(broker, store)
        try:
            receipt = engine.submit(
                _request(instrument, qty=10_000, price=Decimal("100.00"))
            )
            assert receipt.status is OrderStatus.REJECTED
            assert receipt.message == "risk_check_failed"
        finally:
            engine.shutdown()

    def test_an_oversized_order_never_reaches_the_venue(
        self, broker: PaperBroker, store: SQLiteEventStore, instrument: Equity
    ) -> None:
        """The gate is in front of dispatch, not behind it.

        This is the claim that distinguishes a gate before the venue call from
        a check after it: the broker holds no order at all.
        """
        engine, _cache = _compose(broker, store)
        try:
            engine.submit(_request(instrument, qty=10_000, price=Decimal("100.00")))
            held = [o for o in broker.get_order_list() if o.instrument == instrument]
            assert not held, f"a rejected order still reached the venue: {held}"
        finally:
            engine.shutdown()

    def test_a_rejection_is_recorded_for_the_audit_trail(
        self, broker: PaperBroker, store: SQLiteEventStore, instrument: Equity
    ) -> None:
        """A gated order leaves a rejection in the durable log.

        An order that vanishes silently is indistinguishable from one never
        attempted, which is the failure the audit trail exists to prevent.
        """
        engine, _cache = _compose(broker, store)
        try:
            engine.submit(_request(instrument, qty=10_000, price=Decimal("100.00")))
            rejected = [
                e for e in store.replay("orders") if isinstance(e, OrderRejected)
            ]
            assert rejected, "a rejected order left no durable record"
            assert rejected[0].reason == "risk_check_failed"
        finally:
            engine.shutdown()

    def test_the_released_correlation_id_is_reusable(
        self, broker: PaperBroker, store: SQLiteEventStore, instrument: Equity
    ) -> None:
        """A risk-rejected order hands its correlation id back.

        The reservation is released on risk-reject (``engine.py:621-623``), so
        the same key is immediately usable. Leaking it would make the id
        permanently unusable and the rejection unrecoverable.
        """
        engine, _cache = _compose(broker, store)
        try:
            rejected = engine.submit(
                _request(instrument, qty=10_000, price=Decimal("100.00"),
                         correlation="reuse-me")
            )
            assert rejected.status is OrderStatus.REJECTED

            # Same key, now an order that passes the gate.
            accepted = engine.submit(
                _request(instrument, qty=10, price=RESTING_PRICE, correlation="reuse-me")
            )
            assert accepted.status is OrderStatus.ACK, (
                "the correlation id was stranded by the risk rejection"
            )
        finally:
            engine.shutdown()
