"""Inbound fills: a retry must survive a failed write, and fees are recorded.

Two ordering defects this pins:

1. The dedup fingerprint was committed *before* the durable write, so a failed
   write made the venue's identical retry look like a duplicate forever — the
   fill was never applied and never durable.
2. ``_apply_fill`` recomputed the fee, so a replayed event was charged at
   today's rate rather than the amount actually recorded on it.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain.enums import OrderSide
from tradex_domain.events import OrderFilled
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fees import FeeCalculator
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.execution.recovery import InMemoryEventStore
from tradex_trading.execution.trading_cache import TradingCache

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _bus() -> MagicMock:
    bus = MagicMock()
    bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()
    return bus


def _fill(fill_id: str = "venue-1", qty: str = "10") -> Fill:
    return Fill(
        order_id=OrderId("o1"),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal("100")),
        fill_id=fill_id,
    )


def _engine(store: InMemoryEventStore, **kw) -> ExecutionEngine:
    return ExecutionEngine(
        bus=_bus(),
        fill_source=SimulatedFillSource(),
        cache=TradingCache(),
        event_store=store,
        cash=Decimal("100000"),
        fee_calculator=FeeCalculator(),
        require_durable_events=True,
        **kw,
    )


def test_a_retry_after_a_failed_write_is_still_applied() -> None:
    """A failed write must not poison the venue's retry."""
    store = InMemoryEventStore()
    engine = _engine(store)
    original = store.append
    state = {"failed": False}

    def flaky(event):
        if type(event).__name__ == "OrderFilled" and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("disk full")
        return original(event)

    store.append = flaky  # type: ignore[method-assign]

    engine._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001
    assert engine.cache.all_positions() == [], "the first write failed"

    engine._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001 - venue retry

    assert engine.cache.all_positions(), (
        "the retry was swallowed as a duplicate; the fingerprint was recorded "
        "before the durable write succeeded"
    )
    position = engine.cache.all_positions()[0]
    assert position.quantity.value == Decimal("10")
    assert engine.cash_snapshot().cash < Decimal("100000"), (
        "the retried fill must actually move cash"
    )


def test_a_persisted_fill_still_dedups_on_republish() -> None:
    """The dedup contract itself must survive the reordering."""
    store = InMemoryEventStore()
    engine = _engine(store)

    engine._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001
    engine._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001 - republish

    assert engine.cache.all_positions()[0].quantity.value == Decimal("10"), (
        "a re-published fill must not double-apply"
    )


def test_the_recorded_fee_is_applied_not_recomputed() -> None:
    """A replayed event is charged what it says it was charged."""
    store = InMemoryEventStore()
    engine = _engine(store)

    engine._apply_fill(  # noqa: SLF001
        OrderFilled(fill=_fill(), fee_amount=Decimal("20")),
    )

    assert engine.cash_snapshot().total_fees == Decimal("20"), (
        "the recorded fee was replaced by a recomputed one"
    )
    # And the same figure must be what the log now holds.
    persisted = [e for e in store.replay("orders") if isinstance(e, OrderFilled)]
    assert persisted[-1].fee_amount == Decimal("20")


def test_a_fill_without_a_recorded_fee_still_computes_one() -> None:
    """Live fills with no recorded fee fall back to the calculator."""
    store = InMemoryEventStore()
    engine = _engine(store)

    engine._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001

    assert engine.cash_snapshot().total_fees > Decimal("0"), (
        "a live fill with no recorded fee must still be charged"
    )
