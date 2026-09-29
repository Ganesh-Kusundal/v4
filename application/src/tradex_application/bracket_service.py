"""Application layer — bracket order placement use case.

Encapsulates the full bracket order placement flow: read protective levels
from a signal, validate them against the entry price, build a
``BracketOrderRequest``, and submit it through the execution engine.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from tradex_domain.execution import BracketOrderRequest, OrderRequest
from tradex_domain.value_objects import CorrelationId, Price, Quantity
from tradex_strategy.core.brackets import protective_request

if TYPE_CHECKING:
    from tradex_execution.engine import ExecutionEngine

log = logging.getLogger(__name__)


def place_bracket_order(
    engine: ExecutionEngine,
    signal: Any,
    entry: Price,
    quantity: Quantity,
    *,
    correlation_id: CorrelationId | None = None,
    tag: str | None = None,
    strategy_id: str | None = None,
    reference_timestamp: Any = None,
) -> Any:
    """Place a bracket order for *signal* through the execution engine.

    Reads the signal's declared protective levels, validates them against
    *entry*, and builds a ``BracketOrderRequest`` (or plain ``OrderRequest``
    when no valid pair is declared). Submits through the engine and returns
    the ``OrderReceipt`` (or ``OrderId`` on idempotency replay).

    Engine-level exceptions (risk rejection, idempotency, etc.) pass
    through unmodified.
    """
    request: OrderRequest = protective_request(
        signal=signal,
        entry=entry,
        quantity=quantity,
        correlation_id=correlation_id,
        tag=tag,
        strategy_id=strategy_id,
        reference_timestamp=reference_timestamp,
    )
    if isinstance(request, BracketOrderRequest):
        log.info(
            "placing bracket order for %s: entry=%s stop=%s target=%s qty=%s",
            request.instrument,
            entry.value,
            request.stop_loss_price.value if request.stop_loss_price else None,
            request.target_price.value if request.target_price else None,
            quantity.value,
        )
    return engine.submit(request)
