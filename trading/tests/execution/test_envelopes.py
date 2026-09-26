"""Wave A3 — Typed execution envelopes: integration tests.

Tests verify:
  1. All envelope types carry the required header fields.
  2. EnvelopeTranslator correctly maps inbound command envelopes → domain events.
  3. EnvelopeTranslator correctly maps outbound domain events → event envelopes.
  4. ExecutionEngine remains the first handler — the translator only adapts types,
     never duplicates engine logic.
  5. Edge-cases: missing required fields raise ValueError, not silent failures.

No mocks — uses SimulatedFillSource and ReactiveBus (real components).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from tradex_domain import Equity, OrderRequest, OrderSide, OrderStatus, OrderType, Price, Quantity
from tradex_domain.events import (
    OrderCancelled,
    OrderFilled,
    OrderModified,
    OrderPlaced,
    OrderRejected,
    PlaceOrderCommand,
)
from tradex_domain.execution import Fill, Order
from tradex_domain.value_objects import CorrelationId, OrderId

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.envelopes import (
    CancelOrderEnvelope,
    EnvelopeHeader,
    EnvelopeTranslator,
    ExecutionEnvelope,
    ModifyOrderEnvelope,
    OrderAcceptedEnvelope,
    OrderAcknowledgedEnvelope,
    OrderCancelledEnvelope,
    OrderFilledEnvelope,
    OrderPartiallyFilledEnvelope,
    OrderRejectedEnvelope,
    OrderUnknownEnvelope,
    PlaceOrderEnvelope,
    RiskDecisionEnvelope,
)
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.reactive.bus import ReactiveBus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _equity() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request(cid: CorrelationId | None = None) -> OrderRequest:
    return OrderRequest(
        instrument=_equity(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        correlation_id=cid,
    )


def _engine() -> ExecutionEngine:
    return ExecutionEngine(bus=ReactiveBus(), fill_source=SimulatedFillSource())


def _order(
    status: OrderStatus = OrderStatus.NEW,
    cid: CorrelationId | None = None,
) -> Order:
    from tradex_domain.enums import TimeInForce
    return Order(
        order_id=OrderId(value=str(uuid.uuid4())),
        instrument=_equity(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=status,
        correlation_id=cid,
    )


def _fill(order: Order) -> Fill:
    return Fill(
        order_id=order.order_id,
        instrument=order.instrument,
        side=order.side,
        quantity=order.quantity,
        price=order.price,  # type: ignore[arg-type]
        fill_id=str(uuid.uuid4()),
    )


# ---------------------------------------------------------------------------
# 1. Header / base — every envelope carries required metadata
# ---------------------------------------------------------------------------

class TestEnvelopeHeader:
    def test_auto_event_id(self) -> None:
        h = EnvelopeHeader()
        assert uuid.UUID(h.event_id)  # valid UUID

    def test_schema_version_default(self) -> None:
        h = EnvelopeHeader()
        assert h.schema_version == 1

    def test_all_header_fields_present_on_base_envelope(self) -> None:
        env = ExecutionEnvelope(
            strategy_id="strat-1",
            strategy_version="v2",
            policy_version="p3",
            code_revision="abc123",
        )
        assert env.strategy_id == "strat-1"
        assert env.strategy_version == "v2"
        assert env.policy_version == "p3"
        assert env.code_revision == "abc123"
        assert isinstance(env.event_timestamp, datetime)
        assert isinstance(env.receive_timestamp, datetime)
        assert uuid.UUID(env.event_id)

    def test_header_factory_new(self) -> None:
        h = EnvelopeHeader.new(
            command_id="cmd-42",
            correlation_id="corr-99",
            strategy_id="s",
            strategy_version="1.0",
            policy_version="pol",
            code_revision="sha",
        )
        assert h.command_id == "cmd-42"
        assert h.correlation_id == "corr-99"
        assert h.strategy_id == "s"

    def test_envelope_is_frozen(self) -> None:
        env = ExecutionEnvelope()
        with pytest.raises((AttributeError, TypeError)):
            env.schema_version = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. Command envelope shapes
# ---------------------------------------------------------------------------

class TestCommandEnvelopes:
    def test_place_order_envelope_carries_request(self) -> None:
        req = _request()
        env = PlaceOrderEnvelope(request=req, strategy_id="s1")
        assert env.request is req
        assert env.strategy_id == "s1"

    def test_modify_order_envelope_fields(self) -> None:
        req = _request()
        oid = str(uuid.uuid4())
        env = ModifyOrderEnvelope(order_id=oid, request=req)
        assert env.order_id == oid
        assert env.request is req

    def test_cancel_order_envelope_fields(self) -> None:
        oid = str(uuid.uuid4())
        env = CancelOrderEnvelope(order_id=oid, correlation_id="c1")
        assert env.order_id == oid
        assert env.correlation_id == "c1"

    def test_risk_decision_approved(self) -> None:
        env = RiskDecisionEnvelope(approved=True, subject_command_id="cmd-1")
        assert env.approved is True
        assert env.subject_command_id == "cmd-1"
        assert env.reason == ""

    def test_risk_decision_denied(self) -> None:
        env = RiskDecisionEnvelope(approved=False, reason="position_limit")
        assert env.approved is False
        assert env.reason == "position_limit"


# ---------------------------------------------------------------------------
# 3. Event envelope shapes
# ---------------------------------------------------------------------------

class TestEventEnvelopes:
    def test_order_accepted_envelope(self) -> None:
        o = _order()
        env = OrderAcceptedEnvelope(order_id=o.order_id.value, order=o)
        assert env.order_id == o.order_id.value
        assert env.order is o

    def test_order_rejected_envelope(self) -> None:
        o = _order()
        env = OrderRejectedEnvelope(order_id=o.order_id.value, reason="risk_check_failed")
        assert env.reason == "risk_check_failed"

    def test_order_acknowledged_envelope(self) -> None:
        o = _order()
        env = OrderAcknowledgedEnvelope(order_id=o.order_id.value)
        assert env.order_id == o.order_id.value

    def test_order_partially_filled_envelope(self) -> None:
        o = _order()
        f = _fill(o)
        env = OrderPartiallyFilledEnvelope(
            order_id=o.order_id.value,
            filled_quantity=str(f.quantity.value),
            fill_price=str(f.price.value),
            fill=f,
        )
        assert env.filled_quantity == "10"
        assert env.fill_price == "100"
        assert env.fill is f

    def test_order_filled_envelope(self) -> None:
        o = _order()
        f = _fill(o)
        env = OrderFilledEnvelope(
            order_id=o.order_id.value,
            filled_quantity=str(f.quantity.value),
            fill_price=str(f.price.value),
            fill=f,
        )
        assert env.fill is f

    def test_order_cancelled_envelope(self) -> None:
        o = _order()
        env = OrderCancelledEnvelope(order_id=o.order_id.value, order=o)
        assert env.order_id == o.order_id.value

    def test_order_unknown_envelope(self) -> None:
        oid = str(uuid.uuid4())
        env = OrderUnknownEnvelope(order_id=oid, detail="timeout")
        assert env.detail == "timeout"


# ---------------------------------------------------------------------------
# 4. EnvelopeTranslator — inbound (command → domain)
# ---------------------------------------------------------------------------

class TestTranslatorInbound:
    def setup_method(self) -> None:
        self.t = EnvelopeTranslator()

    def test_to_place_command_produces_PlaceOrderCommand(self) -> None:
        req = _request()
        env = PlaceOrderEnvelope(request=req, correlation_id="corr-1")
        cmd = self.t.to_place_command(env)
        assert isinstance(cmd, PlaceOrderCommand)
        assert cmd.request.instrument == req.instrument
        assert cmd.request.quantity == req.quantity

    def test_to_place_command_injects_correlation_when_missing(self) -> None:
        # request has no cid; envelope does — translator must inject it
        req = _request(cid=None)
        env = PlaceOrderEnvelope(request=req, correlation_id="injected-cid")
        cmd = self.t.to_place_command(env)
        assert cmd.request.correlation_id is not None
        assert str(cmd.request.correlation_id) == "injected-cid"

    def test_to_place_command_preserves_existing_cid(self) -> None:
        cid = CorrelationId(value="original")
        req = _request(cid=cid)
        env = PlaceOrderEnvelope(request=req, correlation_id="envelope-cid")
        cmd = self.t.to_place_command(env)
        # request already had a cid; translator must not override it
        assert str(cmd.request.correlation_id) == "original"

    def test_to_place_command_raises_when_request_none(self) -> None:
        env = PlaceOrderEnvelope()
        with pytest.raises(ValueError, match="request must not be None"):
            self.t.to_place_command(env)

    def test_to_modify_args_extracts_order_id_and_request(self) -> None:
        oid = str(uuid.uuid4())
        req = _request()
        env = ModifyOrderEnvelope(order_id=oid, request=req)
        order_id_obj, req_out = self.t.to_modify_args(env)
        assert order_id_obj.value == oid
        assert req_out is req

    def test_to_modify_args_raises_on_empty_order_id(self) -> None:
        env = ModifyOrderEnvelope(request=_request())
        with pytest.raises(ValueError, match="order_id must not be empty"):
            self.t.to_modify_args(env)

    def test_to_modify_args_raises_on_none_request(self) -> None:
        env = ModifyOrderEnvelope(order_id="oid-1")
        with pytest.raises(ValueError, match="request must not be None"):
            self.t.to_modify_args(env)

    def test_to_cancel_args_extracts_order_id_and_cid(self) -> None:
        oid = str(uuid.uuid4())
        env = CancelOrderEnvelope(order_id=oid, correlation_id="c-99")
        order_id_obj, cid = self.t.to_cancel_args(env)
        assert order_id_obj.value == oid
        assert cid is not None
        assert str(cid) == "c-99"

    def test_to_cancel_args_no_correlation(self) -> None:
        oid = str(uuid.uuid4())
        env = CancelOrderEnvelope(order_id=oid)
        order_id_obj, cid = self.t.to_cancel_args(env)
        assert cid is None

    def test_to_cancel_args_raises_on_empty_order_id(self) -> None:
        env = CancelOrderEnvelope()
        with pytest.raises(ValueError, match="order_id must not be empty"):
            self.t.to_cancel_args(env)


# ---------------------------------------------------------------------------
# 5. EnvelopeTranslator — outbound (domain event → envelope)
# ---------------------------------------------------------------------------

class TestTranslatorOutbound:
    def setup_method(self) -> None:
        self.t = EnvelopeTranslator()

    def test_from_order_placed(self) -> None:
        o = _order()
        event = OrderPlaced(order=o)
        env = self.t.from_order_placed(event, strategy_id="s1")
        assert isinstance(env, OrderAcceptedEnvelope)
        assert env.order_id == o.order_id.value
        assert env.order is o
        assert env.strategy_id == "s1"
        assert isinstance(env.event_id, str)

    def test_from_order_placed_propagates_correlation(self) -> None:
        cid = CorrelationId(value="cid-42")
        o = _order(cid=cid)
        event = OrderPlaced(order=o)
        env = self.t.from_order_placed(event)
        assert env.correlation_id == "cid-42"

    def test_from_order_rejected(self) -> None:
        o = _order(status=OrderStatus.REJECTED)
        event = OrderRejected(order=o, reason="risk_check_failed")
        env = self.t.from_order_rejected(event, policy_version="p-1")
        assert isinstance(env, OrderRejectedEnvelope)
        assert env.reason == "risk_check_failed"
        assert env.order_id == o.order_id.value
        assert env.policy_version == "p-1"

    def test_from_order_filled(self) -> None:
        o = _order()
        f = _fill(o)
        event = OrderFilled(fill=f)
        env = self.t.from_order_filled(event, strategy_id="strat-x")
        assert isinstance(env, OrderFilledEnvelope)
        assert env.order_id == o.order_id.value
        assert env.filled_quantity == str(f.quantity.value)
        assert env.fill_price == str(f.price.value)
        assert env.fill is f
        assert env.strategy_id == "strat-x"

    def test_from_order_cancelled(self) -> None:
        o = _order()
        event = OrderCancelled(order=o)
        env = self.t.from_order_cancelled(event)
        assert isinstance(env, OrderCancelledEnvelope)
        assert env.order_id == o.order_id.value
        assert env.order is o

    def test_from_order_modified_produces_acknowledged_envelope(self) -> None:
        o = _order()
        event = OrderModified(order=o)
        env = self.t.from_order_modified(event, code_revision="sha-1")
        assert isinstance(env, OrderAcknowledgedEnvelope)
        assert env.order_id == o.order_id.value
        assert env.code_revision == "sha-1"

    def test_outbound_envelopes_have_unique_event_ids(self) -> None:
        o = _order()
        e1 = OrderPlaced(order=o)
        e2 = OrderPlaced(order=o)
        env1 = self.t.from_order_placed(e1)
        env2 = self.t.from_order_placed(e2)
        assert env1.event_id != env2.event_id


# ---------------------------------------------------------------------------
# 6. End-to-end: envelope → engine → domain event → envelope (round-trip)
# ---------------------------------------------------------------------------

class TestRoundTrip:
    """Verify that the translator integrates with the real ExecutionEngine."""

    def test_place_order_via_envelope_reaches_engine(self) -> None:
        bus = ReactiveBus()
        engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())
        translator = EnvelopeTranslator()

        received_placed: list[OrderPlaced] = []
        received_filled: list[OrderFilled] = []

        bus.of_type(OrderPlaced).subscribe(on_next=received_placed.append)
        bus.of_type(OrderFilled).subscribe(on_next=received_filled.append)

        # Build envelope and translate to PlaceOrderCommand
        env = PlaceOrderEnvelope(
            request=_request(),
            strategy_id="test-strategy",
            strategy_version="1.0",
            policy_version="none",
            code_revision="abc",
        )
        cmd = translator.to_place_command(env)

        # Engine is the first handler — publish the domain command to the bus
        bus.publish(cmd)

        assert len(received_placed) == 1, "engine must publish OrderPlaced"
        assert len(received_filled) == 1, "SimulatedFillSource fills synchronously"

        # Now wrap the outbound events back into envelopes
        accepted_env = translator.from_order_placed(
            received_placed[0], strategy_id="test-strategy"
        )
        filled_env = translator.from_order_filled(
            received_filled[0], strategy_id="test-strategy"
        )

        assert isinstance(accepted_env, OrderAcceptedEnvelope)
        assert isinstance(filled_env, OrderFilledEnvelope)
        assert accepted_env.strategy_id == "test-strategy"
        assert filled_env.strategy_id == "test-strategy"
        # order_id must be consistent across both envelopes
        assert accepted_env.order_id == filled_env.order_id

    def test_engine_not_touched_by_translator(self) -> None:
        """Translator is pure adapter — engine pipeline is unchanged."""
        # Both paths (direct submit and envelope→command) must yield same events
        bus1 = ReactiveBus()
        engine1 = ExecutionEngine(bus=bus1, fill_source=SimulatedFillSource())
        filled1: list[OrderFilled] = []
        bus1.of_type(OrderFilled).subscribe(on_next=filled1.append)
        engine1.submit(_request())

        bus2 = ReactiveBus()
        engine2 = ExecutionEngine(bus=bus2, fill_source=SimulatedFillSource())
        filled2: list[OrderFilled] = []
        bus2.of_type(OrderFilled).subscribe(on_next=filled2.append)
        env = PlaceOrderEnvelope(request=_request())
        cmd = EnvelopeTranslator().to_place_command(env)
        bus2.publish(cmd)

        # Both paths produce a fill with equal quantity and price
        assert len(filled1) == 1
        assert len(filled2) == 1
        assert filled1[0].fill.quantity == filled2[0].fill.quantity
        assert filled1[0].fill.price == filled2[0].fill.price
