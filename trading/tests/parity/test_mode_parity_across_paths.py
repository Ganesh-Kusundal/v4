"""Every execution mode must produce the same economic projection.

The projection is the contract: fills, positions, realized/unrealized P&L, and
cash. Order ids and timestamps are excluded because they are mode-specific;
everything economic must match exactly, or backtest/live disagree about
what actually happened with real money.

This covers the two paths that can drift:
  * the live engine's own book,
  * a rebuild from the durable event log (what a restart would see).
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.events import OrderFilled
from tradex_domain.execution import Fill, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fees import FeeCalculator
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_execution.projection import execution_projection
from tradex_execution.recovery import InMemoryEventStore, recover_trading_cache
from tradex_execution.trading_cache import TradingCache
from tradex_reactive.bus import ReactiveBus

INSTRUMENT = Equity.of("NSE", "RELIANCE")
OPENING_CASH = Decimal("1000000")


def _bus() -> ReactiveBus:
    return ReactiveBus()


def _request(side: OrderSide, quantity: str, price: str, cid: str) -> OrderRequest:
    return OrderRequest(
        instrument=INSTRUMENT,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
        correlation_id=CorrelationId(value=cid),
    )


#: A ladder that buys, adds, then sells through a full round trip and back to
#: flat, so the projection covers entries, realized P&L, and a closed book.
LADDER = [
    (OrderSide.BUY, "10", "100"),
    (OrderSide.BUY, "5", "120"),
    (OrderSide.SELL, "8", "130"),
    (OrderSide.SELL, "7", "110"),
]


def _run_engine(
    fill_source_factory,
    *,
    event_store=None,
    cache=None,
) -> tuple[ExecutionEngine, list[Fill]]:
    """Drive the ladder through an engine and return it with its fills."""
    bus = _bus()
    engine = ExecutionEngine(
        bus,
        fill_source_factory(),
        cache=cache,
        event_store=event_store,
        cash=OPENING_CASH,
        fee_calculator=FeeCalculator(),
    )
    fills: list[Fill] = []
    bus.of_type(OrderFilled).subscribe(lambda event: fills.append(event.fill))
    for index, (side, quantity, price) in enumerate(LADDER):
        engine.submit(_request(side, quantity, price, f"lad-{index}"))
    return engine, fills


def _recorded_fees(store: InMemoryEventStore) -> list[Decimal | None]:
    """The fees the live run actually charged, in fill order."""
    return [
        event.fee_amount
        for event in store.replay("orders")
        if isinstance(event, OrderFilled)
    ]


def test_the_live_run_actually_charged_fees() -> None:
    """The parity comparison is only meaningful if fees are present.

    The ladder moves cash on its own, so a run that silently charged nothing
    would still satisfy every cross-path equality below. Pin that fees exist,
    or "all three paths agree" can be satisfied by all three ignoring costs.
    """
    store = InMemoryEventStore()
    engine, _fills = _run_engine(SimulatedFillSource, event_store=store)
    try:
        charged = engine.cash_snapshot().total_fees
    finally:
        engine.shutdown()

    assert charged > Decimal("0"), (
        "no fees were charged, so the parity assertions prove nothing about costs"
    )
    assert all(fee is not None and fee > 0 for fee in _recorded_fees(store)), (
        "every recorded fill must carry the fee actually charged"
    )


def test_live_book_and_event_store_rebuild_have_the_same_economics() -> None:
    """A restart must rebuild exactly the book the live engine held.

    This is the projection's whole point: the durable log is authoritative, so
    the rebuilt state and the live state are the same economic fact.
    """
    store = InMemoryEventStore()
    engine, fills = _run_engine(SimulatedFillSource, event_store=store)
    try:
        live = execution_projection(
            fills,
            engine.cache.all_positions(),
            engine.cash_snapshot().cash,
        )

        rebuilt_cache = TradingCache()
        outcome = recover_trading_cache(store, rebuilt_cache)
        assert outcome.cash is not None, "the engine must have anchored cash"
        rebuilt_fills = [
            event.fill
            for event in store.replay("orders")
            if isinstance(event, OrderFilled)
        ]
        rebuilt = execution_projection(
            rebuilt_fills,
            rebuilt_cache.all_positions(),
            outcome.cash.cash,
        )
    finally:
        engine.shutdown()

    assert rebuilt == live, "event-store rebuild diverged from the live book"
    # A real round trip: flat at the end, with cash moved by the price delta
    # less fees. Guards against both sides being trivially empty.
    assert live["positions"][0]["quantity"] == "0"
    assert live["cash"] != str(OPENING_CASH)
