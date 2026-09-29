"""Application layer — fill computation use case.

Encapsulates the fill computation flow: resolve the fill price from an
order request and optional market price, apply slippage, and build a
``Fill`` domain object.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from tradex_domain.execution import Fill, Order, OrderRequest
from tradex_domain.value_objects import OrderId, Price

log = logging.getLogger(__name__)


def compute_fill(
    order: Order,
    request: OrderRequest,
    market_price: Price | None = None,
    *,
    slippage_model: Any | None = None,
    clock: Any | None = None,
    order_id_override: OrderId | None = None,
) -> Fill:
    """Compute the fill for *order* given *request* and optional *market_price*.

    Resolves the fill price (market price → limit price → trigger price),
    applies slippage if configured, and builds a ``Fill`` with the resolved
    price and timestamp.

    Raises ``ValueError`` when no positive price can be resolved.
    """
    from tradex_execution.fill_model import FillModel

    model = FillModel(slippage_model, clock=clock)
    price = model.resolve_fill_price(request, market_price)
    timestamp = model.fill_timestamp(request)
    return model.make_fill(order, price, timestamp, order_id_override=order_id_override)
