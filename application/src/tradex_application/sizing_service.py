"""Application layer — position sizing use case.

Encapsulates the sizing flow: resolve the order size for a signal using
a ``PositionSizer``, returning a ``SizingDecision`` that carries the
quantity, source, and reason.
"""

from __future__ import annotations

import logging
from typing import Any

from tradex_domain.strategy import Signal
from tradex_strategy.core.sizing import PositionSizer, SizingDecision

log = logging.getLogger(__name__)


def size_position(
    sizer: PositionSizer,
    signal: Signal,
    *,
    reference_price: Any,
) -> SizingDecision:
    """Size *signal* at *reference_price* using *sizer*.

    Returns a ``SizingDecision`` with the resolved quantity (or ``None``
    if refused), the source of the size, and a human-readable reason.
    """
    decision = sizer.size(signal, reference_price=reference_price)
    if decision.ok:
        log.info(
            "sized %s: qty=%s source=%s reason=%s",
            signal.instrument,
            decision.quantity.value if decision.quantity else None,
            decision.source,
            decision.reason,
        )
    else:
        log.warning(
            "sizing refused for %s: %s",
            signal.instrument,
            decision.reason,
        )
    return decision
