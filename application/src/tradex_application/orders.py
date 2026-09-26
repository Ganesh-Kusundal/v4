"""Application layer — order command handlers.

Three use cases own the canonical path from parsed domain request to engine:

    submit_order        → engine.submit (plain order)
    submit_bracket_order → broker capability gate → engine.submit
    modify_order        → engine.modify
    cancel_order        → engine.cancel

No HTTP concerns live here (no FastAPI, no HTTPException). Engine-level
exceptions (IdempotencyInflight, IdempotencyKeyReuseMismatch, OrderRejectedError)
pass through unmodified; the route layer maps them to HTTP status codes.

The only new exception introduced here is BrokerCapabilityError — a business
rule rather than an engine rule, so it belongs at this layer, not in the route.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tradex_domain.execution import BracketOrderRequest, OrderRequest
from tradex_domain.value_objects import CorrelationId, OrderId

if TYPE_CHECKING:
    from tradex_execution.engine import ExecutionEngine


class BrokerCapabilityError(Exception):
    """Broker does not support the requested order type."""


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
