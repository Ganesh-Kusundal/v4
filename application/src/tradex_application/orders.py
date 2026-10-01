"""Application layer — order command handlers.

Three use cases own the canonical path from parsed domain request to engine:

    submit_order        → engine.submit (plain order)
    submit_bracket_order → broker capability gate → engine.submit
    modify_order        → engine.modify
    cancel_order        → engine.cancel

plus the wire→domain request builder ``build_bracket_request`` (REF-12: moved
from the interface route) so bracket request shaping is testable and reusable
without HTTP.

No HTTP concerns live here (no FastAPI, no HTTPException). Engine-level
exceptions (IdempotencyInflight, IdempotencyKeyReuseMismatch, OrderRejectedError)
pass through unmodified; the route layer maps them to HTTP status codes.

The only new exception introduced here is BrokerCapabilityError — a business
rule rather than an engine rule, so it belongs at this layer, not in the route.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from tradex_domain.constants import (
    INSTRUMENT_ID_FORMAT_MESSAGE,
    INSTRUMENT_ID_OR_EXCHANGE_SYMBOL_MESSAGE,
)
from tradex_domain.enums import OrderSide, OrderType, ProductType, TimeInForce
from tradex_domain.execution import BracketOrderRequest, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

if TYPE_CHECKING:
    from tradex_execution.engine import ExecutionEngine


class BrokerCapabilityError(Exception):
    """Broker does not support the requested order type."""


def _price(value: Any, name: str) -> Price:
    """Parse a decimal ``Price`` from a wire body value.

    Raises ``ValueError`` (never an HTTP error) — the route maps it to 422.
    """
    try:
        return Price(Decimal(str(value)))
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc


def _parse_product_wire(raw: Any) -> ProductType:
    """Resolve a wire product string to a domain ``ProductType``.

    Uses the canonical ``ProductType.from_wire`` single source of truth.
    Explicit and closed on purpose: the product drives margin treatment, so
    an unrecognised value must never fall back to the ``INTRADAY`` default.
    ``COVER_ORDER`` has no wire form and is intentionally absent.
    """
    try:
        return ProductType.from_wire(raw)
    except ValueError:
        raise ValueError(
            f"unknown product {raw!r}; expected one of MIS, CNC, NRML, MTF"
        ) from None


def build_bracket_request(
    body: dict, correlation_id: CorrelationId | None = None,
) -> BracketOrderRequest:
    """Parse a wire order body into a bracket (super) ``OrderRequest``.

    REF-12: moved from the interface route
    (``tradex_interfaces.routes.orders._build_bracket_request``) so request
    shaping lives at the application layer and the route shrinks to
    parse → build → submit. Mirrors the plain-order instrument/entry fields,
    plus the protective legs ``stop_loss_price``/``target_price`` (required)
    and ``trailing_jump`` (optional, defaults 0). Raises ``KeyError`` on
    missing required fields and ``ValueError`` on malformed ones — the route
    maps both to HTTP 422 with the same message strings as before; the
    domain ``BracketOrderRequest`` validates protective-price ordering.
    """
    instrument_id = body.get("instrument_id")
    if instrument_id:
        parts = instrument_id.split(":", 1)
        if len(parts) != 2:
            raise ValueError(INSTRUMENT_ID_FORMAT_MESSAGE)
        instrument = Equity.of(parts[0], parts[1])
    else:
        exchange = body.get("exchange")
        symbol = body.get("symbol")
        if not exchange or not symbol:
            raise ValueError(INSTRUMENT_ID_OR_EXCHANGE_SYMBOL_MESSAGE)
        instrument = Equity.of(exchange, symbol)

    side = OrderSide(body["side"])
    order_type = OrderType(body.get("order_type", "MARKET"))
    quantity = Quantity(Decimal(str(body["quantity"])))
    price = _price(body["price"], "price")
    time_in_force = (
        TimeInForce(body["time_in_force"])
        if body.get("time_in_force")
        else TimeInForce.DAY
    )
    trailing_jump = body.get("trailing_jump")
    return BracketOrderRequest(
        instrument=instrument,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
        time_in_force=time_in_force,
        stop_loss_price=_price(body["stop_loss_price"], "stop_loss_price"),
        target_price=_price(body["target_price"], "target_price"),
        trailing_jump=(
            _price(trailing_jump, "trailing_jump")
            if trailing_jump is not None
            else _price("0", "trailing_jump")
        ),
        product_type=_parse_product_wire(body.get("product")),
        correlation_id=correlation_id,
    )


def submit_order(engine: ExecutionEngine, request: OrderRequest) -> Any:
    """Submit a plain order through the execution engine.

    Returns ``OrderReceipt`` on first submission.  An idempotency replay
    returns the original ``OrderId`` (engine internal contract, passed
    through for the route to map into an idempotency-replay HTTP response).
    Engine-level exceptions (IdempotencyInflight, risk rejection, etc.)
    are not caught here — callers decide the HTTP shape.
    """
    return engine.submit(request)


def submit_bracket_order(engine: ExecutionEngine, broker: Any, request: BracketOrderRequest) -> Any:
    """Submit a bracket (super) order after validating broker capability.

    Raises:
        BrokerCapabilityError: when the broker does not advertise
            ``capabilities.supports_super_order = True``.  The route maps
            this to HTTP 422 so the client knows the venue limitation.

    Returns ``OrderReceipt`` on first submission or ``OrderId`` on replay —
    same contract as ``submit_order``.
    """
    caps = getattr(broker, "capabilities", None)
    if caps is None or not getattr(caps, "supports_super_order", False):
        raise BrokerCapabilityError("broker does not support super orders")
    return engine.submit(request)


def modify_order(engine: ExecutionEngine, order_id: OrderId, request: OrderRequest) -> Any:
    """Modify an existing open order through the execution engine.

    Returns the domain ``Order`` as modified and projected into the OMS.
    Engine-level exceptions (not-found, terminal-status, risk denial,
    IdempotencyKeyReuseMismatch, etc.) are not caught here.
    """
    return engine.modify(order_id, request)


def cancel_order(
    engine: ExecutionEngine,
    order_id: OrderId,
    correlation_id: CorrelationId,
) -> Any:
    """Cancel an open order through the execution engine.

    Returns the domain ``Order`` in CANCELLED status.
    Engine-level exceptions (not-found, terminal-status, IdempotencyInflight,
    etc.) are not caught here.
    """
    return engine.cancel(order_id, correlation_id=correlation_id)
