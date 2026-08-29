"""H3 — applied_fills LRU eviction counter + configurable cap.

The principal-architect review (H3) found that
``engine.py:495-498`` documents a 50k LRU cap on the
applied-fills dedup set with a "TODO add latency histograms
when dashboard needs them" comment. The cap itself works
but is invisible to operators, and there is no metric when
eviction actually happens under load. Two fixes:

  1. ``applied_fills_max: int = 50_000`` becomes an
     ``__init__`` parameter so tests (and tight-memory
     deployments) can dial it down.
  2. A ``bus.applied_fills.evicted`` counter increments by 1
     every time the LRU evicts a key.

The docstring on ``_record_applied_fill`` is also updated to
spell out the cap and LRU semantics so the next maintainer
sees them at the call site, not just in a side comment.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain.enums import OrderSide, OrderStatus, OrderType
from tradex_domain.execution import Order, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.market import OHLC, Candle, Timeframe
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.runtime.metrics import MetricsRegistry


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _bus() -> MagicMock:
    bus = MagicMock()
    bus.of_type = MagicMock(return_value=MagicMock())
    bus.of_type.return_value.pipe = MagicMock(return_value=MagicMock())
    bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()
    return bus


def _request(**overrides) -> OrderRequest:
    defaults = dict(
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
    )
    defaults.update(overrides)
    return OrderRequest(**defaults)


def _candle(ts: datetime) -> Candle:
    return Candle(
        instrument=_eq(),
        timeframe=Timeframe("1d"),
        ohlc=OHLC(
            open=Price(value=Decimal("100")),
            high=Price(value=Decimal("105")),
            low=Price(value=Decimal("95")),
            close=Price(value=Decimal("102")),
        ),
        volume=Decimal("1000"),
        timestamp=ts,
    )


def _make_order(order_id: str, status: OrderStatus = OrderStatus.NEW) -> Order:
    return Order(
        order_id=OrderId(value=order_id),
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force="DAY",
        status=status,
    )


def _make_fill(order_id: str, fill_id: str, qty: int = 1) -> object:
    """Build a Fill-like object with the fingerprint the engine reads."""
    from tradex_domain.execution import Fill

    return Fill(
        order_id=OrderId(value=order_id),
        instrument=_eq(),
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal("100")),
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        fill_id=fill_id,
    )


def test_applied_fills_evicted_counter_increments_on_lru_eviction() -> None:
    """Appending more fills than the cap evicts and increments the counter."""
    metrics = MetricsRegistry()
    engine = ExecutionEngine(
        bus=_bus(),
        fill_source=SimulatedFillSource(),
        metrics=metrics,
        applied_fills_max=3,
    )

    for i in range(5):
        engine._record_applied_fill(_make_fill(f"o-{i}", f"f-{i}"))

    # 5 inserts into a cap-3 LRU → at least 2 evictions.
    assert metrics.get("bus.applied_fills.evicted") >= 2


def test_applied_fills_max_is_configurable() -> None:
    """The cap is bound at construction and is the size that the LRU uses."""
    engine = ExecutionEngine(
        bus=_bus(),
        fill_source=SimulatedFillSource(),
        metrics=MetricsRegistry(),
        applied_fills_max=10,
    )

    assert engine._applied_fills_max == 10

    # The dict never grows past the cap.
    for i in range(25):
        engine._record_applied_fill(_make_fill(f"o-{i}", f"f-{i}"))
    assert len(engine._applied_fills) <= 10
