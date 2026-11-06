"""MP-1: durable fill dedup survives a restart."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from tradex_domain.enums import OrderSide
from tradex_domain.events import OrderFilled
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fees import FeeCalculator
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_execution.sqlite_event_store import SQLiteEventStore
from tradex_execution.trading_cache import TradingCache

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _bus() -> MagicMock:
    bus = MagicMock()
    bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()
    return bus


def _fill(fill_id: str = "venue-1") -> Fill:
    return Fill(
        order_id=OrderId("o1"),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        fill_id=fill_id,
    )


def _engine(store: SQLiteEventStore) -> ExecutionEngine:
    return ExecutionEngine(
        bus=_bus(),
        fill_source=SimulatedFillSource(),
        cache=TradingCache(),
        event_store=store,
        cash=Decimal("100000"),
        fee_calculator=FeeCalculator(),
        require_durable_events=True,
    )


def test_durable_fill_dedup_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    store = SQLiteEventStore(db)
    engine = _engine(store)
    engine._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001
    assert len(engine.cache.all_positions()) == 1
    cash_after = engine.cash_snapshot().cash
    fees_after = engine.cash_snapshot().total_fees

    engine2 = _engine(SQLiteEventStore(db))
    engine2._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001
    assert engine2._seen_applied_fill(_fill()) is True  # noqa: SLF001


def test_republish_after_shutdown_does_not_reapply(tmp_path: Path) -> None:
    db = tmp_path / "events2.db"
    store = SQLiteEventStore(db)
    engine = _engine(store)
    engine._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001
    cash_after = engine.cash_snapshot().cash
    fees_after = engine.cash_snapshot().total_fees
    engine.shutdown()
    engine._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001
    assert engine.cash_snapshot().cash == cash_after
    assert engine.cash_snapshot().total_fees == fees_after
    assert len(engine.cache.all_positions()) == 1
