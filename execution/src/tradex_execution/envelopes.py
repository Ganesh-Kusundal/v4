"""Wave A3 — Typed execution envelopes.

Every envelope carries a common header (event_id, schema_version, command_id,
correlation_id, strategy_id, strategy_version, policy_version, code_revision,
event_timestamp, receive_timestamp) and a typed payload.

EnvelopeTranslator converts envelopes ↔ existing domain events so the
ExecutionEngine pipeline requires zero changes — envelopes are the outer typed
shell; domain events remain the engine's internal language.

Command envelopes (inbound):
    PlaceOrderEnvelope, ModifyOrderEnvelope, CancelOrderEnvelope, RiskDecisionEnvelope

Event envelopes (outbound):
    OrderAcceptedEnvelope, OrderRejectedEnvelope, OrderAcknowledgedEnvelope,
    OrderPartiallyFilledEnvelope, OrderFilledEnvelope, OrderCancelledEnvelope,
    OrderUnknownEnvelope
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.events import (
    OrderCancelled,
    OrderFilled,
    OrderModified,
    OrderPlaced,
    OrderRejected,
    PlaceOrderCommand,
)
from tradex_domain.execution import Fill, Order, OrderRequest
from tradex_domain.instruments import Instrument
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

if TYPE_CHECKING:
    pass

_SCHEMA_VERSION = 1


def _now() -> datetime:
    return datetime.now(UTC)


def _new_event_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Common envelope header
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnvelopeHeader:
    """Metadata carried by every execution envelope.

    Separating header from payload lets callers build headers once and
    attach them to multiple envelope types without repeating fields.
    """

    event_id: str = field(default_factory=_new_event_id)
    schema_version: int = _SCHEMA_VERSION
    command_id: str | None = None
    correlation_id: str | None = None
    strategy_id: str = ""
    strategy_version: str = ""
    policy_version: str = ""
    code_revision: str = ""
    event_timestamp: datetime = field(default_factory=_now)
    receive_timestamp: datetime = field(default_factory=_now)

    @classmethod
    def new(
        cls,
        *,
        command_id: str | None = None,
        correlation_id: str | None = None,
        strategy_id: str = "",
        strategy_version: str = "",
        policy_version: str = "",
        code_revision: str = "",
        event_timestamp: datetime | None = None,
    ) -> EnvelopeHeader:
        """Construct a header with defaults for optional fields."""
        now = _now()
        return cls(
            command_id=command_id,
            correlation_id=correlation_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            policy_version=policy_version,
            code_revision=code_revision,
            event_timestamp=event_timestamp or now,
            receive_timestamp=now,
        )


# ---------------------------------------------------------------------------
# Base envelope — header fields inlined for frozen-dataclass ergonomics
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionEnvelope:
    """All execution envelopes share this header surface.

    Subclasses add payload fields via standard dataclass inheritance.
    """

    event_id: str = field(default_factory=_new_event_id)
    schema_version: int = _SCHEMA_VERSION
    command_id: str | None = None
    correlation_id: str | None = None
    strategy_id: str = ""
    strategy_version: str = ""
    policy_version: str = ""
    code_revision: str = ""
    event_timestamp: datetime = field(default_factory=_now)
    receive_timestamp: datetime = field(default_factory=_now)


# ---------------------------------------------------------------------------
# Command envelopes (inbound — strategies/adapters → engine)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlaceOrderEnvelope(ExecutionEnvelope):
    """Command: place a new order.

    Carries an :class:`OrderRequest` as the typed payload so the translator
    can forward it directly to ``PlaceOrderCommand`` without re-parsing fields.
    The header provides the strategy / policy tracing context.
    """

    request: OrderRequest | None = None  # required; None only for dataclass default


@dataclass(frozen=True, slots=True)
class ModifyOrderEnvelope(ExecutionEnvelope):
    """Command: modify an existing open order."""

    order_id: str = ""
    request: OrderRequest | None = None  # required; None only for dataclass default


@dataclass(frozen=True, slots=True)
class CancelOrderEnvelope(ExecutionEnvelope):
    """Command: cancel an open order."""

    order_id: str = ""


@dataclass(frozen=True, slots=True)
class RiskDecisionEnvelope(ExecutionEnvelope):
    """Risk gate verdict (approved or denied) for a pending order command.

    ``approved=True`` is a positive gate; the engine may proceed.
    ``approved=False`` must block submission and publish an OrderRejected.
    ``reason`` carries the denial rationale (empty on approval).
    """

    approved: bool = True
    reason: str = ""
    subject_command_id: str | None = None  # the command_id this decision covers


# ---------------------------------------------------------------------------
# Event envelopes (outbound — engine → strategies/adapters/audit)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderAcceptedEnvelope(ExecutionEnvelope):
    """Engine accepted and submitted the order to the venue."""

    order_id: str = ""
    order: Order | None = None


@dataclass(frozen=True, slots=True)
class OrderRejectedEnvelope(ExecutionEnvelope):
    """Order was rejected by risk, idempotency, or the venue."""

    order_id: str = ""
    reason: str = ""
    order: Order | None = None


@dataclass(frozen=True, slots=True)
class OrderAcknowledgedEnvelope(ExecutionEnvelope):
    """Venue acknowledged the order (ACK state)."""

    order_id: str = ""
    order: Order | None = None


@dataclass(frozen=True, slots=True)
class OrderPartiallyFilledEnvelope(ExecutionEnvelope):
    """A partial fill was applied to the order."""

    order_id: str = ""
    filled_quantity: str = "0"   # Decimal serialised to str for frozen purity
    fill_price: str = "0"
    fill: Fill | None = None


@dataclass(frozen=True, slots=True)
class OrderFilledEnvelope(ExecutionEnvelope):
    """The order reached fully-filled state."""

    order_id: str = ""
    filled_quantity: str = "0"
    fill_price: str = "0"
    fill: Fill | None = None


@dataclass(frozen=True, slots=True)
class OrderCancelledEnvelope(ExecutionEnvelope):
    """The order was cancelled."""

    order_id: str = ""
    order: Order | None = None


@dataclass(frozen=True, slots=True)
class OrderUnknownEnvelope(ExecutionEnvelope):
    """Order state is unknown (venue timeout, reconciliation gap, etc.)."""

    order_id: str = ""
    detail: str = ""


# ---------------------------------------------------------------------------
# Translator — the only stateful seam between envelopes and domain events
# ---------------------------------------------------------------------------


class EnvelopeTranslator:
    """Bidirectional mapper: typed envelopes ↔ existing domain events.

    The ExecutionEngine is the first handler — it works exclusively in domain
    types (PlaceOrderCommand, OrderRequest, OrderFilled …). This translator
    is the *adaptation layer*, not a replacement for the engine pipeline.

    Inbound (envelope → domain):
        to_place_command     PlaceOrderEnvelope  → PlaceOrderCommand
        to_modify_args       ModifyOrderEnvelope → (OrderId, OrderRequest)
        to_cancel_args       CancelOrderEnvelope → (OrderId, CorrelationId|None)

    Outbound (domain event → envelope):
        from_order_placed     OrderPlaced     → OrderAcceptedEnvelope
        from_order_rejected   OrderRejected   → OrderRejectedEnvelope
        from_order_filled     OrderFilled     → OrderFilledEnvelope
        from_order_cancelled  OrderCancelled  → OrderCancelledEnvelope
        from_order_modified   OrderModified   → OrderAcknowledgedEnvelope
    """

    # -- inbound -----------------------------------------------------------

    def to_place_command(self, env: PlaceOrderEnvelope) -> PlaceOrderCommand:
        """Translate a PlaceOrderEnvelope → PlaceOrderCommand for the bus.

        Injects correlation_id from the envelope header into the request when
        the request itself does not carry one, preserving the strategy's
        tracing token end-to-end.
        """
        if env.request is None:
            raise ValueError("PlaceOrderEnvelope.request must not be None")
        request = env.request
        # Inject correlation if request has none but envelope does.
        if request.correlation_id is None and env.correlation_id:
            from dataclasses import replace as _replace
            cid = CorrelationId(value=env.correlation_id)
            request = _replace(request, correlation_id=cid)
        return PlaceOrderCommand(
            request=request,
            correlation_id=CorrelationId(value=env.correlation_id)
            if env.correlation_id else None,
        )

    def to_modify_args(
        self, env: ModifyOrderEnvelope
    ) -> tuple[OrderId, OrderRequest]:
        """Extract (order_id, request) for engine.modify()."""
        if not env.order_id:
            raise ValueError("ModifyOrderEnvelope.order_id must not be empty")
        if env.request is None:
            raise ValueError("ModifyOrderEnvelope.request must not be None")
        return OrderId(value=env.order_id), env.request

    def to_cancel_args(
        self, env: CancelOrderEnvelope
    ) -> tuple[OrderId, CorrelationId | None]:
        """Extract (order_id, correlation_id) for engine.cancel()."""
        if not env.order_id:
            raise ValueError("CancelOrderEnvelope.order_id must not be empty")
        cid = CorrelationId(value=env.correlation_id) if env.correlation_id else None
        return OrderId(value=env.order_id), cid

    # -- outbound ----------------------------------------------------------

    def _base_kwargs(
        self,
        *,
        command_id: str | None = None,
        correlation_id: str | None = None,
        strategy_id: str = "",
        strategy_version: str = "",
        policy_version: str = "",
        code_revision: str = "",
        event_timestamp: datetime | None = None,
    ) -> dict:
        now = _now()
        return dict(
            command_id=command_id,
            correlation_id=correlation_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            policy_version=policy_version,
            code_revision=code_revision,
            event_timestamp=event_timestamp or now,
            receive_timestamp=now,
        )

    def from_order_placed(
        self,
        event: OrderPlaced,
        **meta: str,
    ) -> OrderAcceptedEnvelope:
        """Wrap an OrderPlaced domain event as an outbound envelope."""
        cid_val = (
            str(event.order.correlation_id)
            if event.order.correlation_id else None
        )
        return OrderAcceptedEnvelope(
            order_id=event.order.order_id.value,
            order=event.order,
            **self._base_kwargs(
                correlation_id=cid_val,
                event_timestamp=event.timestamp,
                **meta,  # type: ignore[arg-type]
            ),
        )

    def from_order_rejected(
        self,
        event: OrderRejected,
        **meta: str,
    ) -> OrderRejectedEnvelope:
        """Wrap an OrderRejected domain event as an outbound envelope."""
        cid_val = (
            str(event.order.correlation_id)
            if event.order.correlation_id else None
        )
        return OrderRejectedEnvelope(
            order_id=event.order.order_id.value,
            reason=event.reason,
            order=event.order,
            **self._base_kwargs(
                correlation_id=cid_val,
                event_timestamp=event.timestamp,
                **meta,  # type: ignore[arg-type]
            ),
        )

    def from_order_filled(
        self,
        event: OrderFilled,
        **meta: str,
    ) -> OrderFilledEnvelope:
        """Wrap an OrderFilled domain event as an outbound envelope."""
        fill = event.fill
        return OrderFilledEnvelope(
            order_id=fill.order_id.value,
            filled_quantity=str(fill.quantity.value),
            fill_price=str(fill.price.value),
            fill=fill,
            **self._base_kwargs(
                event_timestamp=event.timestamp,
                **meta,  # type: ignore[arg-type]
            ),
        )

    def from_order_cancelled(
        self,
        event: OrderCancelled,
        **meta: str,
    ) -> OrderCancelledEnvelope:
        """Wrap an OrderCancelled domain event as an outbound envelope."""
        cid_val = (
            str(event.order.correlation_id)
            if event.order.correlation_id else None
        )
        return OrderCancelledEnvelope(
            order_id=event.order.order_id.value,
            order=event.order,
            **self._base_kwargs(
                correlation_id=cid_val,
                event_timestamp=event.timestamp,
                **meta,  # type: ignore[arg-type]
            ),
        )

    def from_order_modified(
        self,
        event: OrderModified,
        **meta: str,
    ) -> OrderAcknowledgedEnvelope:
        """Wrap an OrderModified domain event as an outbound envelope.

        Modified → Acknowledged: the venue accepted and applied the modification;
        the envelope signals that the order state is now the post-modify snapshot.
        """
        cid_val = (
            str(event.order.correlation_id)
            if event.order.correlation_id else None
        )
        return OrderAcknowledgedEnvelope(
            order_id=event.order.order_id.value,
            order=event.order,
            **self._base_kwargs(
                correlation_id=cid_val,
                event_timestamp=event.timestamp,
                **meta,  # type: ignore[arg-type]
            ),
        )


