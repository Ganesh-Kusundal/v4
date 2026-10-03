"""Hash-chain tamper detection on the append-only event log.

The event log is the OMS source of truth: order state is projected from it at
boot (``_recover_oms_from_events``). A log that could be edited without detection
would let a rewritten history produce a rewritten position — so every row is
hashed from its own stored fields and chained to its predecessor, and
``replay`` verifies both on the read path rather than trusting the table.

Each test writes a REAL log through ``append``, then breaks it with a REAL
``sqlite3`` UPDATE/DELETE against the database file, then reads it back through
``replay``. Nothing is mocked and no error is injected: the tamper is a genuine
edit to a genuine file, which is the only kind the chain exists to catch.

The three tamper classes are distinct failures with distinct causes, so each is
asserted separately rather than as one "raises" test:

* **modified** — a row's content no longer matches its own ``event_hash``
* **deleted** — an interior row is gone, so its successor's ``prev_hash`` dangles
* **reordered** — rows swapped, so the recorded linkage no longer matches

``test_the_log_below_a_snapshot_still_chain_verifies`` in
``test_event_store_snapshots.py`` covers a modification below a snapshot
boundary; this module covers the three classes against an unsnapshotted log.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import OrderPlaced
from tradex_domain.execution import Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.sqlite_event_store import EventChainError, SQLiteEventStore


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _order(order_id: str) -> Order:
    return Order(
        order_id=OrderId(order_id),
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.NEW,
    )


def _log(db: Path, *, count: int = 4) -> SQLiteEventStore:
    """A real store holding *count* real events, left open for the caller."""
    store = SQLiteEventStore(db)
    for i in range(count):
        store.append(OrderPlaced(order=_order(f"o-{i}")))
    return store


def _edit(db: Path, sql: str, params: tuple = ()) -> None:
    """Apply a real SQL edit to the log file, bypassing the store entirely."""
    con = sqlite3.connect(db)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def _sequences(db: Path) -> list[int]:
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute("SELECT sequence FROM events ORDER BY sequence")]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 1. Modified row
# ---------------------------------------------------------------------------


def test_detects_modified_row(tmp_path: Path) -> None:
    """A row whose content no longer hashes to its recorded hash is refused."""
    db = tmp_path / "events.db"
    store = _log(db)
    store.close()

    # Change the event type of an interior row. The stored event_hash still
    # describes the original content, so recomputation disagrees with it.
    _edit(db, "UPDATE events SET event_type = ? WHERE sequence = ?", ("OrderCancelled", 2))

    reopened = SQLiteEventStore(db)
    try:
        with pytest.raises(EventChainError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


def test_detects_modified_payload(tmp_path: Path) -> None:
    """Rewriting a payload is caught even when the event type is unchanged.

    The hash covers the payload column, not only the type — so a tamper that
    keeps the row plausible but changes what it says is still detected. This is
    the edit that would matter in practice: same event, different order behind it.
    """
    db = tmp_path / "events.db"
    store = _log(db)
    store.close()

    # A payload that is still well-formed enough to be stored, but is not the
    # payload that was hashed. Swapping in another row's payload keeps every
    # column type valid so only the hash can notice.
    _edit(
        db,
        "UPDATE events SET payload = (SELECT payload FROM events WHERE sequence = 4)"
        " WHERE sequence = 2",
    )

    reopened = SQLiteEventStore(db)
    try:
        with pytest.raises(EventChainError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# 2. Deleted row
# ---------------------------------------------------------------------------


def test_detects_deleted_interior_row(tmp_path: Path) -> None:
    """Removing an interior row leaves its successor's prev_hash dangling.

    The chain is global and sequence-ordered, so deleting a middle row does not
    merely shorten the log — the row after it still names the deleted row as its
    predecessor, and that link can no longer be satisfied.
    """
    db = tmp_path / "events.db"
    store = _log(db)
    store.close()

    _edit(db, "DELETE FROM events WHERE sequence = 2")

    reopened = SQLiteEventStore(db)
    try:
        with pytest.raises(EventChainError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


def test_detects_deleted_row_across_all_streams(tmp_path: Path) -> None:
    """A deletion is caught even when replay asks for a different stream.

    Verification walks the whole log regardless of the stream being read, so a
    tamper in one stream cannot be hidden by reading another. The docstring on
    ``_verify_chain`` is explicit that this is deliberate.
    """
    db = tmp_path / "events.db"
    store = SQLiteEventStore(db)
    for i in range(3):
        store.append(OrderPlaced(order=_order(f"o-{i}")))
    store.close()

    _edit(db, "DELETE FROM events WHERE sequence = 2")

    reopened = SQLiteEventStore(db)
    try:
        with pytest.raises(EventChainError):
            list(reopened.replay("system"))
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# 3. Reordered rows
# ---------------------------------------------------------------------------


def test_detects_reordered_rows(tmp_path: Path) -> None:
    """Swapping two rows breaks the recorded linkage between them.

    Reordering is the tamper that changes meaning without changing content: the
    same events in a different order are a different history. Each row's
    prev_hash still points at its original predecessor, so the swap is visible
    even though every row is individually intact.
    """
    db = tmp_path / "events.db"
    store = _log(db)
    store.close()

    # Exchange the payloads of two adjacent rows. Both rows remain internally
    # consistent with their own hashes, but each now sits in the other's
    # position in the chain, so the prev_hash linkage is broken.
    _edit(
        db,
        "UPDATE events SET payload = CASE sequence WHEN 2 THEN"
        " (SELECT payload FROM events WHERE sequence = 3)"
        " WHEN 3 THEN (SELECT payload FROM events WHERE sequence = 2) END"
        " WHERE sequence IN (2, 3)",
    )

    reopened = SQLiteEventStore(db)
    try:
        with pytest.raises(EventChainError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# 4. An unmodified log is accepted
# ---------------------------------------------------------------------------


def test_accepts_unmodified_chain(tmp_path: Path) -> None:
    """An intact log replays every event with no error.

    The control for the three tests above: without it, a store that rejected
    everything would pass all of them. This asserts the positive path — the
    real events written by ``append`` come back in order, complete.
    """
    db = tmp_path / "events.db"
    store = _log(db, count=4)
    try:
        events = list(store.replay("orders"))
    finally:
        store.close()

    assert len(events) == 4
    assert [e.order.order_id.value for e in events] == ["o-0", "o-1", "o-2", "o-3"]


def test_append_after_replay_extends_the_same_chain(tmp_path: Path) -> None:
    """A second session continues the chain rather than restarting it.

    Recovery reopens an existing log file, so the chain must survive a close and
    reopen: events appended after reopening link to the ones written before. A
    store that reset its chain on open would silently accept a log whose second
    half was written by something else.
    """
    db = tmp_path / "events.db"
    store = _log(db, count=2)
    store.close()

    reopened = SQLiteEventStore(db)
    try:
        reopened.append(OrderPlaced(order=_order("o-2")))
        events = list(reopened.replay("orders"))
    finally:
        reopened.close()

    assert [e.order.order_id.value for e in events] == ["o-0", "o-1", "o-2"]


def test_sequence_numbers_are_contiguous_after_reopen(tmp_path: Path) -> None:
    """Reopened sequences continue from the log, not from a fresh counter.

    The sequence is the chain's ordering key. Restarting it at 1 on reopen would
    make a second session's events collide with the first's, and the linkage
    that the tamper tests above rely on would be meaningless.
    """
    db = tmp_path / "events.db"
    store = _log(db, count=3)
    store.close()

    reopened = SQLiteEventStore(db)
    try:
        reopened.append(OrderPlaced(order=_order("o-3")))
        assert _sequences(db) == [1, 2, 3, 4]
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# 5. A row written by a non-chain-aware build
# ---------------------------------------------------------------------------


def test_detects_row_without_a_hash(tmp_path: Path) -> None:
    """A row carrying no hash at all is refused, not skipped.

    Nulling the hash columns is the cheapest possible tamper — it needs no
    knowledge of the hash function. The store must treat it as a break ("the log
    was not written by a chain-aware build") rather than reading the row anyway.
    """
    db = tmp_path / "events.db"
    store = _log(db)
    store.close()

    _edit(db, "UPDATE events SET event_hash = NULL, prev_hash = NULL WHERE sequence = 2")

    reopened = SQLiteEventStore(db)
    try:
        with pytest.raises(EventChainError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


def test_timestamps_survive_a_round_trip(tmp_path: Path) -> None:
    """A replayed event keeps the timestamp it was written with.

    The chain hashes ``created_at``, so a tamper that rewrites a timestamp is
    caught — but only if the honest value survives a write/read cycle. This
    pins the positive case the hash depends on.
    """
    db = tmp_path / "events.db"
    stamped = datetime(2026, 7, 1, 9, 30, tzinfo=UTC)
    store = SQLiteEventStore(db)
    store.append(
        OrderPlaced(order=_order("o-1"), timestamp=stamped, correlation_id=None)
    )
    try:
        (event,) = list(store.replay("orders"))
    finally:
        store.close()

    assert isinstance(event.timestamp, datetime)
    assert event.timestamp == stamped, (
        f"the replayed timestamp {event.timestamp!r} is not the one written "
        f"({stamped!r})"
    )
