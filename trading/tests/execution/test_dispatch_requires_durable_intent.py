"""A dispatch intent that cannot be persisted must not reach the broker.

The intent write is the only durable record that an order may be live at the
venue. If it fails and the order is sent anyway, a crash immediately afterward
leaves an order at the broker that no local record accounts for.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine
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


def _engine_with_failing_intent() -> tuple[ExecutionEngine, InMemoryEventStore]:
    store = InMemoryEventStore()
    engine = ExecutionEngine(
        bus=_bus(),
        fill_source=SimulatedFillSource(),
        cache=TradingCache(),
        event_store=store,
        cash=Decimal("100000"),
        require_durable_events=True,
    )
    original = store.append
    state = {"failed": False}

    def flaky(event):
        if type(event).__name__ == "BrokerOrderRequested" and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("disk full")
        return original(event)

    store.append = flaky  # type: ignore[method-assign]
    engine._intent_failed = state  # type: ignore[attr-defined]
    return engine, store


def test_broker_is_not_called_when_the_intent_write_fails() -> None:
    engine, _store = _engine_with_failing_intent()
    try:
        engine.submit(_request())
    except Exception:
        pass  # a rejection is acceptable; crossing the boundary is not

    assert engine._intent_failed["failed"] is True, (  # type: ignore[attr-defined]
        "the intent write never failed, so this test proves nothing"
    )
    assert engine._fill.submit_calls == 0, (  # type: ignore[attr-defined]
        "the order reached the broker despite a failed durable intent write"
    )
    assert engine.kill_switch is True
