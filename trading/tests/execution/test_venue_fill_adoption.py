"""MP-4: a venue-confirmed fill the local ledger deems unaffordable is booked
and flagged, never dropped.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain.enums import OrderSide
from tradex_domain.events import ErrorOccurred, OrderFilled
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fees import FeeCalculator
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_execution.trading_cache import TradingCache
from tradex_runtime.metrics import MetricsRegistry as RuntimeMetrics

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _bus() -> MagicMock:
    bus = MagicMock()
    bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()
    published = []
    bus.publish.side_effect = lambda e: published.append(e)
    bus._published = published
    return bus


def _fill() -> Fill:
    return Fill(
        order_id=OrderId("o-unaff"),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        fill_id="venue-unaff",
    )


def test_venue_fill_unaffordable_is_booked_and_flagged() -> None:
    metrics = RuntimeMetrics()
    bus = _bus()
    engine = ExecutionEngine(
        bus=bus,
        fill_source=SimulatedFillSource(),
        cache=TradingCache(),
        cash=Decimal("0"),
        fee_calculator=FeeCalculator(),
        metrics=metrics,
    )

    # Must not raise into the bus loop.
    engine._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001

    # Position reflects the fill (venue is truth).
    positions = engine.cache.all_positions()
    assert len(positions) == 1
    assert positions[0].quantity.value == Decimal("10")

    # Cash reflects the fill (overdraft), not silently zero.
    assert engine.cash_snapshot().cash < Decimal("0")

    # Flagged via ErrorOccurred.
    errors = [e for e in bus._published if isinstance(e, ErrorOccurred)]
    assert errors, "no ErrorOccurred published"
    assert "unaffordable_venue_fill=o-unaff" in str(errors[0].error)

    # Metric incremented.
    assert metrics.get("bus.fill.unadoptable") >= 1
