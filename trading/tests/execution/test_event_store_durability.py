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


@pytest.fixture
def preserved_registry():
    """Snapshot the decoder registry and restore it exactly after the test.

    The chain verifier derives the versions it can hash from the decoder
    registry, so a test that pops a version to simulate a build without it
    leaks into every later test in the session — including ones in other
    modules, which then fail with "version 1 has no registered decoder" for
    reasons that have nothing to do with their own assertions. A test may also
    move ``SCHEMA_VERSION`` forward to simulate a bump, and that is a module
    global the store reads at append time. Restoring both makes each registry
    mutation local to its test.
    """
    from tradex_execution import sqlite_event_store as ses

    decoders = dict(ses._DECODERS)
    scoped = set(ses._SCOPED_VERSIONS)
    version = ses.SCHEMA_VERSION
    yield ses
    ses.SCHEMA_VERSION = version
    ses._DECODERS.clear()
    ses._DECODERS.update(decoders)
    ses._SCOPED_VERSIONS.clear()
    ses._SCOPED_VERSIONS.update(scoped)


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


def _write_v1_rows(db: Path, n: int = 1) -> None:
    """Write rows in the exact shape version 1 was written in.

    Version 1 is the retired account-UNSCOPED format: a bare stream name and
    a v1 hash over it. The rows are chained by hand, through the v1 hash
    function, because that is how a v1-era build wrote them — reconstructing
    them any other way would not be a v1 log, and the point is that the real
    ones still verify.
    """
    from tradex_execution import sqlite_event_store as ses

    # Open the store once so the schema exists, then write the rows by hand in
    # the v1 shape — an open store writes them at the CURRENT version, which
    # would make this a v2 log rather than the retired format it is testing.
    store = ses.SQLiteEventStore(db)
    store.close()

    con = sqlite3.connect(db)
    previous = ses.GENESIS_HASH
    for i in range(n):
        payload = ses._encode(OrderPlaced(order=_order(f"legacy-{i}")))
        digest = ses._row_hash(
            i + 1, "orders", "OrderPlaced", f"legacy-id-{i}", None,
            payload, f"2026-01-0{i + 1}T00:00:00+00:00", 1, previous,
        )
        con.execute(
            "INSERT INTO events (sequence, stream_id, event_type, event_id, "
            "correlation_id, payload, created_at, schema_version, "
            "prev_hash, event_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                i + 1, "orders", "OrderPlaced", f"legacy-id-{i}", None,
                payload, f"2026-01-0{i + 1}T00:00:00+00:00", 1,
                previous, digest,
            ),
        )
        previous = digest
    con.commit()
    con.close()


def test_a_registered_legacy_decoder_reads_its_own_version(
    tmp_path: Path, preserved_registry,
) -> None:
    """Registering a decoder for a retired version is what keeps it readable.

    The rows are written in the v1 format (bare stream name, v1 hash), then a
    decoder is registered for version 1 that records what it was called with.
    Replay must route those rows through THAT decoder, not the current one —
    the registry is the mechanism, and a build that forgot to register the
    retired version has no business reading its rows.
    """
    ses = preserved_registry

    db = tmp_path / "events.db"
    _write_v1_rows(db)

    seen: list[bytes] = []

    def _v1_decoder(raw: bytes):
        seen.append(raw)
        return ses._unpickle(raw)

    ses.register_decoder(1, _v1_decoder)
    reopened = _store(tmp_path)
    try:
        events = list(reopened.replay("orders"))
    finally:
        reopened.close()
    assert len(events) == 1
    assert seen, "the registered decoder was not the one used"


def test_legacy_rows_replay_with_verification_passing(tmp_path: Path) -> None:
    """THE history-preservation test for the account-scope bump.

    Version 2 changed the stored content of ``stream_id`` — from a bare stream
    name to ``"{broker}:{account}:{stream}"`` — which changes every row's hash.
    The v1 rows on disk were never rewritten, so they must still verify with
    the hash function they were written under, and they must still replay.

    Hashing a v1 row with the v2 scheme would fail an honest log here, which is
    the exact regression this test exists to catch.
    """
    db = tmp_path / "events.db"
    _write_v1_rows(db, n=4)

    store = _store(tmp_path)
    try:
        events = list(store.replay("orders"))
        assert [e.order.order_id.value for e in events] == [  # type: ignore[union-attr]
            "legacy-0", "legacy-1", "legacy-2", "legacy-3",
        ]
    finally:
        store.close()


def test_a_v1_log_and_a_v2_log_hash_differently_and_both_verify(
    tmp_path: Path,
) -> None:
    """The same event under two versions is two different honest digests.

    This is the reason the hash function is keyed on the row's version rather
    than applied uniformly: the scope change moved ``stream_id``, so a v1 and a
    v2 row for one order cannot hash alike. Both verify, because each is hashed
    the way it was written.
    """
    from tradex_execution import sqlite_event_store as ses

    payload = ses._encode(OrderPlaced(order=_order("o-1")))

    legacy = ses._row_hash(
        1, "orders", "OrderPlaced", "id-1", None,
        payload, "2026-01-01T00:00:00+00:00", 1, ses.GENESIS_HASH,
    )
    scoped = ses._row_hash(
        1, "DHAN:acc1:orders", "OrderPlaced", "id-1", None,
        payload, "2026-01-01T00:00:00+00:00", ses.SCHEMA_VERSION, ses.GENESIS_HASH,
    )
    assert legacy != scoped, "the account scope does not participate in the hash"


def test_scoping_a_stream_does_not_silently_re_route_legacy_rows(
    tmp_path: Path,
) -> None:
    """A v1 log read by an account-scoped store still finds its rows.

    The scope is applied to rows the store WRITES; rows already on disk keep
    the stream id they were written with, and replay matches on the bare
    stream name so an operator reopening an old log under a scoped build still
    sees the same history — filed under the same stream it was written to.
    """
    db = tmp_path / "events.db"
    _write_v1_rows(db, n=2)

    scoped = SQLiteEventStore(
        db, broker_id="DHAN", account_id="acc1",
    )
    try:
        events = list(scoped.replay("orders"))
    finally:
        scoped.close()
    assert len(events) == 2, "a scoped open lost an un-scoped log's history"


def test_history_survives_a_version_bump(tmp_path: Path, preserved_registry) -> None:
    """THE forward-compatibility test: a bump must not invalidate history.

    Rows are written under the current version, then the build is simulated as
    having moved on: ``SCHEMA_VERSION`` is bumped to 3 and a decoder is
    registered for the retired version. The version-2 rows must STILL decode.

    Before the registry this raised ``EventDecodeError`` on every old row, so a
    routine version bump made the entire durable log unreadable — precisely the
    regression the registry exists to prevent.
    """
    ses = preserved_registry

    store = _store(tmp_path)
    store.append(OrderPlaced(order=_order("o-1")))
    store.append(OrderFilled(fill=_fill("o-1")))
    store.close()

    def _v2_decoder(raw: bytes):
        return ses._unpickle(raw)

    # The retired version keeps its decoder — this is the whole point.
    ses.register_decoder(2, _v2_decoder)
    # ...and the build moves on to a new format.
    ses.SCHEMA_VERSION = 3
    ses.register_decoder(3, ses._unpickle)

    reopened = _store(tmp_path)
    try:
        events = list(reopened.replay("orders"))
    finally:
        reopened.close()

    assert len(events) == 2, "history was invalidated by a version bump"
    assert isinstance(events[0], OrderPlaced)
    assert isinstance(events[1], OrderFilled)


def test_a_new_row_after_a_bump_does_not_break_the_old_chain(
    tmp_path: Path, preserved_registry,
) -> None:
    """Old and new rows coexist: the bump is not a fork in the log.

    The rows are written under two different versions, which means two
    different hash functions (version 2 hashes the account-scoped stream id,
    version 3 the same scheme under a new number). Both verify, in one chain,
    because the function is chosen by each row's own ``schema_version``.
    """
    ses = preserved_registry

    store = _store(tmp_path)
    store.append(OrderPlaced(order=_order("o-1")))
    store.close()

    ses.SCHEMA_VERSION = 3
    ses.register_decoder(3, ses._unpickle)
    store = _store(tmp_path)
    store.append(OrderPlaced(order=_order("o-2")))
    store.close()

    reopened = _store(tmp_path)
    try:
        events = list(reopened.replay("orders"))
    finally:
        reopened.close()
    assert [e.order.order_id.value for e in events] == ["o-1", "o-2"]  # type: ignore[union-attr]


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
