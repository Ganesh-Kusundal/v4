"""A corrupt or unrecognised event must stop recovery, not shrink the book.

The store is the sole authority for what happened. A row that cannot be decoded
used to be logged and skipped, so a restarted process silently rebuilt a book
with a fill missing — and reported success. Losing money quietly is worse than
refusing to start, so an unreadable row is a hard failure.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide
from tradex_domain.events import CashAccountInitialized, OrderFilled
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.recovery import InMemoryEventStore, recover_trading_cache
from tradex_execution.sqlite_event_store import (
    EventDecodeError,
    SQLiteEventStore,
)
from tradex_execution.trading_cache import TradingCache


def _fill_event() -> OrderFilled:
    return OrderFilled(
        fill=Fill(
            order_id=OrderId("o1"),
            instrument=Equity.of("NSE", "RELIANCE"),
            side=OrderSide.BUY,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
            fill_id="f1",
        ),
    )


@pytest.fixture
def store_path(tmp_path):
    return str(tmp_path / "events.db")


def test_a_corrupt_row_stops_recovery(store_path) -> None:
    """A skipped row means a fill the venue executed is missing from the book."""
    import sqlite3

    store = SQLiteEventStore(store_path)
    store.append(CashAccountInitialized(amount=Decimal("1000")))
    store.append(_fill_event())
    store.close()

    # Corrupt one payload the way a partial write or a bad migration would.
    con = sqlite3.connect(store_path)
    con.execute(
        "UPDATE events SET payload = 'not-a-pickle' WHERE event_type = 'OrderFilled'",
    )
    con.commit()
    con.close()

    reopened = SQLiteEventStore(store_path)
    try:
        with pytest.raises(EventDecodeError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


def test_an_unknown_schema_version_stops_recovery(store_path) -> None:
    """A row written by a newer build must not be guessed at."""
    import sqlite3

    store = SQLiteEventStore(store_path)
    store.append(_fill_event())
    store.close()

    con = sqlite3.connect(store_path)
    con.execute("UPDATE events SET schema_version = 99")
    con.commit()
    con.close()

    reopened = SQLiteEventStore(store_path)
    try:
        with pytest.raises(EventDecodeError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


def test_the_current_schema_version_is_still_readable(store_path) -> None:
    """The guard must not reject the version this build writes."""
    store = SQLiteEventStore(store_path)
    try:
        store.append(_fill_event())
        events = list(store.replay("orders"))
        assert len(events) == 1
    finally:
        store.close()


def test_recovery_reports_a_short_book_rather_than_succeeding(
    store_path,
) -> None:
    """A rebuild that could not read every event must not look like success."""
    import sqlite3

    store = SQLiteEventStore(store_path)
    store.append(CashAccountInitialized(amount=Decimal("1000")))
    store.append(_fill_event())
    store.close()

    con = sqlite3.connect(store_path)
    con.execute(
        "UPDATE events SET payload = 'not-a-pickle' WHERE event_type = 'OrderFilled'",
    )
    con.commit()
    con.close()

    reopened = SQLiteEventStore(store_path)
    try:
        with pytest.raises(EventDecodeError):
            recover_trading_cache(reopened, TradingCache())
    finally:
        reopened.close()


def test_in_memory_store_is_unaffected() -> None:
    """The in-memory store never persists, so it has nothing to corrupt."""
    store = InMemoryEventStore()
    store.append(CashAccountInitialized(amount=Decimal("1000")))
    store.append(_fill_event())

    outcome = recover_trading_cache(store, TradingCache())

    assert outcome.cash is not None
    assert outcome.cash.cash == Decimal("0")
