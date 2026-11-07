"""MP-1: durable fill dedup survives a restart.

Every test here must FAIL if the durable dedup regresses to plain
in-memory wiring.  The trick that makes that possible: a fresh engine
rebuilt over the same event store gets its OWN opening cash and does not
replay events at construction, so a re-applied fill is observable as a
change from *that* engine's opening balance — comparing against the first
engine's closing balance would be vacuous (both engines open at the same
number, so a double-booking would look identical to a correct no-op).
"""

from __future__ import annotations

import threading
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
INITIAL_CASH = Decimal("100000")
NOTIONAL = Decimal("1000")  # 10 shares x 100


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


def _engine(store: SQLiteEventStore, cash: Decimal = INITIAL_CASH) -> ExecutionEngine:
    return ExecutionEngine(
        bus=_bus(),
        fill_source=SimulatedFillSource(),
        cache=TradingCache(),
        event_store=store,
        cash=cash,
        fee_calculator=FeeCalculator(),
        require_durable_events=True,
    )


def _one_fill_totals() -> tuple[Decimal, Decimal]:
    """(cash, total_fees) after the fixture fill applies exactly once."""
    fee = FeeCalculator().calculate(_fill()).amount
    return INITIAL_CASH - NOTIONAL - fee, fee


def test_durable_fill_dedup_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    store = SQLiteEventStore(db)
    engine = _engine(store)
    engine._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001
    # Cash debited once, fee debited once, one Position.
    expected_cash, expected_fee = _one_fill_totals()
    snap = engine.cash_snapshot()
    assert snap.cash == expected_cash
    assert snap.total_fees == expected_fee
    assert len(engine.cache.all_positions()) == 1

    # "Restart": fresh engine, fresh in-memory state, DIFFERENT opening cash
    # so a re-booking cannot masquerade as the first engine's own close.
    engine2 = _engine(SQLiteEventStore(db), cash=Decimal("50000"))
    assert engine2._seen_applied_fill(_fill()) is True  # noqa: SLF001
    engine2._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001
    snap2 = engine2.cash_snapshot()
    assert snap2.cash == Decimal("50000")  # nothing rebooked
    assert snap2.total_fees == Decimal("0")
    assert len(engine2.cache.all_positions()) == 0


def test_republish_after_shutdown_does_not_reapply(tmp_path: Path) -> None:
    db = tmp_path / "events2.db"
    store = SQLiteEventStore(db)
    engine = _engine(store)
    engine._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001
    expected_cash, expected_fee = _one_fill_totals()
    snap = engine.cash_snapshot()
    assert snap.cash == expected_cash
    assert snap.total_fees == expected_fee
    engine.shutdown()
    engine._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001
    assert engine.cash_snapshot().cash == expected_cash
    assert engine.cash_snapshot().total_fees == expected_fee
    assert len(engine.cache.all_positions()) == 1

    # The same republish seen by a FRESH engine (in-memory dedup died with
    # the first one): only the durable record can refuse it here.
    engine2 = _engine(SQLiteEventStore(db), cash=Decimal("77777"))
    engine2._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001
    snap2 = engine2.cash_snapshot()
    assert snap2.cash == Decimal("77777")
    assert snap2.total_fees == Decimal("0")
    assert len(engine2.cache.all_positions()) == 0


def test_concurrent_duplicate_fill_applies_once(tmp_path: Path) -> None:
    """Two threads republishing the same fill book it exactly once (MP-1)."""
    db = tmp_path / "events3.db"
    engine = _engine(SQLiteEventStore(db))
    barrier = threading.Barrier(2, timeout=30)
    errors: list[BaseException] = []

    def republish() -> None:
        try:
            barrier.wait()
            engine._apply_fill(OrderFilled(fill=_fill()))  # noqa: SLF001
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=republish) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors

    expected_cash, expected_fee = _one_fill_totals()
    snap = engine.cash_snapshot()
    assert snap.cash == expected_cash  # debited once, not twice
    assert snap.total_fees == expected_fee  # fee once, not twice
    assert len(engine.cache.all_positions()) == 1
