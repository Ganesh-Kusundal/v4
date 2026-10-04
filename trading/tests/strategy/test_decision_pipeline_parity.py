"""DecisionPipeline parity: same inputs -> same OrderRequest as the inline path."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import Equity, OrderSide, Signal
from tradex_domain.value_objects import Price
from tradex_strategy.core.brackets import protective_request
from tradex_strategy.core.decision_pipeline import DecisionPipeline
from tradex_strategy.core.sizing import unconfigured_sizer


def _signal() -> Signal:
    return Signal(
        instrument=Equity.of("NSE", "RELIANCE"),
        direction=OrderSide.BUY,
        strength=1.0,
        reason="test",
        timestamp=datetime(2026, 8, 5, 15, 0, tzinfo=UTC),
    )


def test_pipeline_matches_inline_protective_request() -> None:
    sizer = unconfigured_sizer()
    pipeline = DecisionPipeline(sizer)
    signal = _signal()
    entry = Price(value=Decimal("100"))
    got = pipeline.build_request(
        signal, entry, "sma_cross",
        correlation_id=None, tag="sma_cross@1.0.0",
        reference_timestamp=signal.timestamp,
    )
    # The inline path the engine used to run:
    decision = sizer.size(signal, reference_price=entry)
    expected = protective_request(
        signal=signal, entry=entry, quantity=decision.quantity,
        correlation_id=None, tag="sma_cross@1.0.0",
        strategy_id="sma_cross", reference_timestamp=signal.timestamp,
    )
    assert got is not None
    assert got.quantity == expected.quantity
    assert got.stop_loss_price == expected.stop_loss_price
    assert got.target_price == expected.target_price
