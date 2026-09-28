"""The event store must not lose history to a version bump, nor hide tampering.

Two defects, both of which used to be true of this store:

1. ``_decode`` RAISED on any ``schema_version`` other than the current one, so
   bumping the version invalidated every row ever written. The log is the sole
   authority for what happened; a bump made the whole thing unreadable. There is
   now a per-version DECODER REGISTRY, and the load-bearing test here is the
   forward-compatibility one: write under version 1, bump the build to version
   2, and assert the version-1 rows STILL decode through their own decoder.

2. Nothing chained the rows, so an edited, reordered, or removed row was
   indistinguishable from an honest log. There is now a ``prev_hash`` /
   ``event_hash`` chain, and the load-bearing test is that replay VERIFIES it on
   the READ path and refuses a tampered row.

Both properties are enforced on read. A check that cannot fail is not a check.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import OrderFilled, OrderPlaced
from tradex_domain.execution import Fill, Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.sqlite_event_store import (
    GENESIS_HASH,
    EventChainError,
    EventDecodeError,
    SQLiteEventStore,
    registered_versions,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _fill(order_id: str) -> Fill:
    return Fill(
        order_id=OrderId(order_id),
        instrument=_eq(),
        side=OrderSide.BUY,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("2500")),
    )


def _store(path: Path) -> SQLiteEventStore:
    return SQLiteEventStore(path / "events.db")


def _sql(db: Path, statement: str) -> None:
    """Run a statement against a store's file, as an outsider would."""
    con = sqlite3.connect(db)
    try:
        con.execute(statement)
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Item 2 — schema-version decoder registry
# ---------------------------------------------------------------------------


def test_the_current_version_has_a_registered_decoder() -> None:
    """Nothing is decodable by accident; the registry is what decodes it."""
    from tradex_execution.sqlite_event_store import SCHEMA_VERSION

    assert SCHEMA_VERSION in registered_versions()


def test_an_unregistered_version_is_still_refused(tmp_path: Path) -> None:
    """A shape with no decoder must not be guessed at.

    The registry makes OLD versions decodable; it must not make UNKNOWN ones
    decodable, because a newer build's row may have a shape that would silently
    produce wrong numbers.
    """
    store = _store(tmp_path)
    store.append(OrderPlaced(order=_order("o-1")))
    store.close()

    _sql(tmp_path / "events.db", "UPDATE events SET schema_version = 4242")

    reopened = _store(tmp_path)
    try:
        with pytest.raises(EventDecodeError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


def test_a_registered_legacy_decoder_reads_its_own_version(tmp_path: Path) -> None:
    """Registering a decoder for an old version is what keeps it readable."""
    from tradex_execution import sqlite_event_store as ses

    store = _store(tmp_path)
    store.append(OrderPlaced(order=_order("o-1")))
    store.close()

    seen: list[bytes] = []

    def _v1_decoder(raw: bytes):
        seen.append(raw)
        return ses._unpickle(raw)

    try:
        ses.register_decoder(1, _v1_decoder)
        reopened = _store(tmp_path)
        try:
            events = list(reopened.replay("orders"))
        finally:
            reopened.close()
        assert len(events) == 1
        assert seen, "the registered decoder was not the one used"
    finally:
        ses._DECODERS.pop(1, None)
        ses.register_decoder(ses.SCHEMA_VERSION, ses._unpickle)


def test_history_survives_a_version_bump(tmp_path: Path) -> None:
    """THE forward-compatibility test: a bump must not invalidate history.

    Rows are written under the current version, then the build is simulated as
    having moved on: ``SCHEMA_VERSION`` is bumped to 2 and a decoder is
    registered for the retired version 1. The version-1 rows must STILL decode.

    Before the registry this raised ``EventDecodeError`` on every old row, so a
    routine version bump made the entire durable log unreadable — precisely the
    regression the registry exists to prevent.
    """
    from tradex_execution import sqlite_event_store as ses

    store = _store(tmp_path)
    store.append(OrderPlaced(order=_order("o-1")))
    store.append(OrderFilled(fill=_fill("o-1")))
    store.close()

    def _v1_decoder(raw: bytes):
        return ses._unpickle(raw)

    original_version = ses.SCHEMA_VERSION
    try:
        # The retired version keeps its decoder — this is the whole point.
        ses.register_decoder(1, _v1_decoder)
        # ...and the build moves on to a new format.
        ses.SCHEMA_VERSION = 2
        ses.register_decoder(2, ses._unpickle)

        reopened = _store(tmp_path)
        try:
            events = list(reopened.replay("orders"))
        finally:
            reopened.close()

        assert len(events) == 2, "history was invalidated by a version bump"
        assert isinstance(events[0], OrderPlaced)
        assert isinstance(events[1], OrderFilled)
    finally:
        ses.SCHEMA_VERSION = original_version
        ses._DECODERS.pop(1, None)
        ses._DECODERS.pop(2, None)
        ses.register_decoder(original_version, ses._unpickle)


def test_a_new_row_after_a_bump_does_not_break_the_old_chain(tmp_path: Path) -> None:
    """Old and new rows coexist: the bump is not a fork in the log."""
    from tradex_execution import sqlite_event_store as ses

    store = _store(tmp_path)
    store.append(OrderPlaced(order=_order("o-1")))
    store.close()

    original_version = ses.SCHEMA_VERSION
    try:
        ses.SCHEMA_VERSION = 2
        ses.register_decoder(2, ses._unpickle)
        store = _store(tmp_path)
        store.append(OrderPlaced(order=_order("o-2")))
        store.close()

        reopened = _store(tmp_path)
        try:
            events = list(reopened.replay("orders"))
        finally:
            reopened.close()
        assert [e.order.order_id.value for e in events] == ["o-1", "o-2"]  # type: ignore[union-attr]
    finally:
        ses.SCHEMA_VERSION = original_version
        ses._DECODERS.pop(2, None)
        ses.register_decoder(original_version, ses._unpickle)


# ---------------------------------------------------------------------------
# Item 3 — prev_hash chain, verified on replay
# ---------------------------------------------------------------------------


def test_every_appended_row_is_hashed_and_chained(tmp_path: Path) -> None:
    """The chain is actually written: row N points at row N-1."""
    store = _store(tmp_path)
    store.append(OrderPlaced(order=_order("o-1")))
    store.append(OrderPlaced(order=_order("o-2")))
    store.close()

    con = sqlite3.connect(tmp_path / "events.db")
    rows = con.execute(
        "SELECT sequence, prev_hash, event_hash FROM events ORDER BY sequence",
    ).fetchall()
    con.close()

    assert len(rows) == 2
    (seq1, prev1, hash1), (seq2, prev2, hash2) = rows
    assert prev1 == GENESIS_HASH
    assert prev2 == hash1, "the second row does not chain to the first"
    assert hash1 and hash2 and hash1 != hash2


def test_an_untouched_log_replays_cleanly(tmp_path: Path) -> None:
    """The negative control: verification must pass on an honest log."""
    store = _store(tmp_path)
    for i in range(5):
        store.append(OrderPlaced(order=_order(f"o-{i}")))
    events = list(store.replay("orders"))
    store.close()
    assert len(events) == 5


def test_a_row_edited_in_place_is_refused(tmp_path: Path) -> None:
    """THE tamper test: a payload swapped under the store is caught on replay.

    This is the mutation that matters — a row whose bytes were changed after
    they were written. The recorded ``event_hash`` no longer matches the
    content, and replay refuses rather than rebuilding a book from it.
    """
    store = _store(tmp_path)
    store.append(OrderPlaced(order=_order("o-1")))
    store.append(OrderFilled(fill=_fill("o-1")))
    store.append(OrderPlaced(order=_order("o-3")))
    store.close()

    _sql(
        tmp_path / "events.db",
        "UPDATE events SET payload = 'bm90LWEtcGlja2xl' WHERE sequence = 2",
    )

    reopened = _store(tmp_path)
    try:
        with pytest.raises(EventChainError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


def test_tampering_the_middle_of_a_long_log_is_refused(tmp_path: Path) -> None:
    """The break is found in the middle, not only at the ends."""
    store = _store(tmp_path)
    for i in range(8):
        store.append(OrderPlaced(order=_order(f"o-{i}")))
    store.close()

    _sql(
        tmp_path / "events.db",
        "UPDATE events SET event_type = 'OrderCancelled' WHERE sequence = 5",
    )

    reopened = _store(tmp_path)
    try:
        with pytest.raises(EventChainError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


def test_removing_an_interior_row_breaks_the_chain(tmp_path: Path) -> None:
    """A row excised from the middle leaves a link that points at nothing.

    The successor still names the deleted row's hash, so the chain no longer
    describes the log and replay refuses. This is the shape a covert removal of
    a trade from the record would take.
    """
    store = _store(tmp_path)
    for i in range(6):
        store.append(OrderPlaced(order=_order(f"o-{i}")))
    store.close()

    _sql(tmp_path / "events.db", "DELETE FROM events WHERE sequence = 4")

    reopened = _store(tmp_path)
    try:
        with pytest.raises(EventChainError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


def test_the_chain_is_verified_on_read_not_only_on_write(tmp_path: Path) -> None:
    """A row whose hash was rewritten to match is still caught by its successor.

    Forging a row in place is not enough: the forged row changes what its
    successor links to, so the break surfaces one row later. This is what makes
    the chain a property of the READ path rather than a field written once.
    """
    store = _store(tmp_path)
    for i in range(5):
        store.append(OrderPlaced(order=_order(f"o-{i}")))
    store.close()

    # Recompute the tampered row's own hash so it is internally self-consistent.
    from tradex_execution import sqlite_event_store as ses

    con = sqlite3.connect(tmp_path / "events.db")
    row = con.execute(
        "SELECT sequence, stream_id, event_type, event_id, correlation_id, "
        "payload, created_at, schema_version, prev_hash FROM events "
        "WHERE sequence = 3",
    ).fetchone()
    forged = ses._row_hash(
        row[0], row[1], "OrderCancelled", row[3], row[4],
        row[5], row[6], row[7], row[8],
    )
    con.execute(
        "UPDATE events SET event_type = 'OrderCancelled', event_hash = ? "
        "WHERE sequence = 3",
        (forged,),
    )
    con.commit()
    con.close()

    reopened = _store(tmp_path)
    try:
        with pytest.raises(EventChainError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


def test_a_stream_filter_still_verifies_the_whole_log(tmp_path: Path) -> None:
    """Chain verification is not narrowed by which stream was asked for.

    The chain spans the whole log, so replaying one stream must still walk
    every row to carry the chain forward — otherwise a break in an unrelated
    stream would be invisible to whoever read this one.
    """
    store = _store(tmp_path)
    store.append(OrderPlaced(order=_order("o-1")), stream_id="orders")
    store.append(OrderPlaced(order=_order("o-sys")), stream_id="system")
    store.append(OrderPlaced(order=_order("o-2")), stream_id="orders")

    assert len(list(store.replay("orders"))) == 2
    assert len(list(store.replay("system"))) == 1
    store.close()

    # Break the "system" row, then read only "orders".
    _sql(
        tmp_path / "events.db",
        "UPDATE events SET event_type = 'OrderCancelled' WHERE sequence = 2",
    )
    reopened = _store(tmp_path)
    try:
        with pytest.raises(EventChainError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


def test_a_legitimate_head_truncation_is_still_readable(tmp_path: Path) -> None:
    """Removing the EARLIEST rows is store surgery, not tampering.

    The platform drops a bad opening anchor by deleting the head of the log (an
    operator correcting a corrupt opening balance). The survivors are
    internally consistent and only the first row points at a predecessor that
    is gone, so replay must still work — refusing here would break a
    legitimate and already-tested recovery path.
    """
    store = _store(tmp_path)
    for i in range(4):
        store.append(OrderPlaced(order=_order(f"o-{i}")))
    store.close()

    _sql(tmp_path / "events.db", "DELETE FROM events WHERE sequence = 1")

    reopened = _store(tmp_path)
    try:
        events = list(reopened.replay("orders"))
    finally:
        reopened.close()
    assert [e.order.order_id.value for e in events] == ["o-1", "o-2", "o-3"]  # type: ignore[union-attr]


def test_a_row_with_no_hash_is_refused(tmp_path: Path) -> None:
    """A row outside the chain is not quietly accepted."""
    store = _store(tmp_path)
    store.append(OrderPlaced(order=_order("o-1")))
    store.close()

    _sql(tmp_path / "events.db", "UPDATE events SET event_hash = NULL")

    reopened = _store(tmp_path)
    try:
        with pytest.raises(EventChainError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# Migration — an existing database must be upgraded, not replaced
# ---------------------------------------------------------------------------

_LEGACY_DDL = """
CREATE TABLE events (
    sequence       INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id      TEXT    NOT NULL,
    event_type     TEXT    NOT NULL,
    event_id       TEXT    NOT NULL UNIQUE,
    correlation_id TEXT,
    payload        TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
)
"""


def _write_legacy_db(db: Path) -> None:
    """A database in the exact shape this store had BEFORE the chain existed."""
    from tradex_execution.sqlite_event_store import _encode

    con = sqlite3.connect(db)
    con.executescript(_LEGACY_DDL)
    rows = []
    for i in range(4):
        event = OrderPlaced(order=_order(f"legacy-{i}"))
        rows.append((
            "orders", "OrderPlaced", f"legacy-id-{i}", None,
            _encode(event), f"2026-01-0{i + 1}T00:00:00+00:00", 1,
        ))
    con.executemany(
        "INSERT INTO events (stream_id, event_type, event_id, correlation_id, "
        "payload, created_at, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def test_a_legacy_database_is_migrated_and_its_history_kept(tmp_path: Path) -> None:
    """Existing databases must not need a fresh file.

    A migration that lost or invalidated existing history would be a serious
    regression: the log IS the record. This builds a pre-chain database by
    hand, opens it with the current store, and asserts every legacy row is
    still there, still decodes, and is now chain-verified.
    """
    db = tmp_path / "legacy.db"
    _write_legacy_db(db)

    store = SQLiteEventStore(db)
    try:
        events = list(store.replay("orders"))
        assert len(events) == 4, "legacy history was lost by the migration"
        assert [e.order.order_id.value for e in events] == [  # type: ignore[union-attr]
            "legacy-0", "legacy-1", "legacy-2", "legacy-3",
        ]
        # Appended to the migrated log, and the whole thing verifies.
        store.append(OrderPlaced(order=_order("new")))
        assert len(list(store.replay("orders"))) == 5
    finally:
        store.close()


def test_a_migrated_database_is_chain_verified_afterwards(tmp_path: Path) -> None:
    """The backfill is real: tampering with a migrated row is caught."""
    db = tmp_path / "legacy.db"
    _write_legacy_db(db)

    store = SQLiteEventStore(db)
    store.close()

    _sql(db, "UPDATE events SET event_type = 'OrderCancelled' WHERE sequence = 2")

    reopened = SQLiteEventStore(db)
    try:
        with pytest.raises(EventChainError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


def test_reopening_a_migrated_database_is_idempotent(tmp_path: Path) -> None:
    """A second open must not re-chain (and invalidate) an already-chained log."""
    db = tmp_path / "legacy.db"
    _write_legacy_db(db)

    for _ in range(3):
        store = SQLiteEventStore(db)
        try:
            assert len(list(store.replay("orders"))) == 4
        finally:
            store.close()
