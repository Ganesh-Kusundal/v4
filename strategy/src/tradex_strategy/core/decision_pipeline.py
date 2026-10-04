"""Pure decision pipeline: signal + price -> sized, protected OrderRequest."""

from __future__ import annotations

from typing import Any

from tradex_domain.execution import OrderRequest
from tradex_domain.strategy import Signal
from tradex_domain.value_objects import Price

from tradex_strategy.core.brackets import protective_request
from tradex_strategy.core.sizing import PositionSizer


class DecisionPipeline:
    """Encapsulates the signal -> sizing -> protection -> order decision.

    The engine keeps the orchestration (defer, flush, correlation ids,
    submit) and delegates the decision here, so the decision is independently
    testable and the engine is purely an orchestrator. This is a parity
    extraction: the pipeline produces the same OrderRequest the engine's
    inline path produced.
    """

    def __init__(self, sizer: PositionSizer) -> None:
        self._sizer = sizer

    def build_request(
        self,
        signal: Signal,
        entry_price: Price,
        strategy_id: str,
        *,
        correlation_id: Any = None,
        tag: str | None = None,
        reference_timestamp: Any = None,
    ) -> OrderRequest | None:
        decision = self._sizer.size(signal, reference_price=entry_price)
        if decision.quantity is None:
            return None
        return protective_request(
            signal=signal,
            entry=entry_price,
            quantity=decision.quantity,
            correlation_id=correlation_id,
            tag=tag,
            strategy_id=strategy_id,
            reference_timestamp=reference_timestamp,
        )
