"""A fill that cannot be persisted must not move the in-memory book.

The event store is authoritative and the cache is a projection. If the book
moves first and the durable write then fails, the two disagree permanently and
a restart rebuilds a book that is missing a fill the venue already executed.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.events import OrderFilled
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.cash_ledger import CashSnapshot
from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fees import FeeCalculator
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.execution.recovery import InMemoryEventStore
from tradex_trading.execution.trading_cache import TradingCache


def _bus() -> MagicMock:
    bus = MagicMock()
    bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()
    return bus


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )


def test_failed_persist_leaves_positions_and_cash_untouched() -> None:
    """The live-fill bridge must not move the book when the write fails.

    Exercised through ``_apply_fill`` because that is the live path: the bus
    subscriber that applies inbound venue fills. The sync ``submit`` path
    reaches the same code after publishing, so ordering is pinned once here.
    """
    cache = TradingCache()
    store = InMemoryEventStore()
    engine = ExecutionEngine(
        bus=_bus(),
        fill_source=SimulatedFillSource(),
        cache=cache,
        event_store=store,
        cash=Decimal("100000"),
        fee_calculator=FeeCalculator(),
        require_durable_events=True,
    )

    fill = SimulatedFillSource().submit(_request())[1]
    assert fill is not None

    original_append = store.append
    state = {"failed": False}

    def flaky(event):
        if type(event).__name__ == "OrderFilled" and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("disk full")
        return original_append(event)

    store.append = flaky  # type: ignore[method-assign]

    engine._apply_fill(OrderFilled(fill=fill))  # noqa: SLF001 - the live path

    assert state["failed"] is True, "the durable write never failed"
    # The book must not have moved, because the event never landed.
    assert cache.all_positions() == []
    assert engine.cash_snapshot() == CashSnapshot(
        cash=Decimal("100000"), total_fees=Decimal("0"),
    )
