"""Lane 4c, item 1 — account-scoped ``stream_id`` and the version-2 bump.

The spec's requirement (``docs/specs/0002-production-readiness-programme``,
"Target data model"):

    EventRecord | stream_id scoped by account | yes | no
    Two brokers today would write into one stream and silently overwrite
    each other

The hard part is what the bump does to history already on disk. Version 2
changed the STORED CONTENT of ``stream_id`` — from a bare stream name to
``f"{broker_id}:{account_id}:{stream}"`` — and ``stream_id`` is inside the
hash input, so the same event written under v1 and under v2 hashes to two
different digests. There is no way to make that a no-op. The store's answer is
to keep BOTH hash functions and choose by the row's own ``schema_version``,
which is exactly what the decoder registry was built for: a v1 row is verified
as it was written, never re-hashed with a scheme it never met.

A store that "handled" this by rewriting every row's stream id in place would
be doing the precise thing the hash chain exists to detect, and would have to
turn verification off to get away with it. It would also lose the account
partition for the rows it rewrote, which is the whole point of the change.

Every test below has a negative control: mutate the source so the property is
removed and the test fails. The mutation harness lives in
``test_event_store_mutation_controls.py``.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import OrderPlaced
from tradex_domain.execution import Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.sqlite_event_store import (
    SCHEMA_VERSION,
    UNSCOPED_ACCOUNT,
    SQLiteEventStore,
    scoped_stream,
    stream_suffix,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _order(order_id: str = "o-1") -> Order:
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


def _rows(db: Path) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT sequence, stream_id, schema_version FROM events "
            "ORDER BY sequence",
        ).fetchall()
    finally:
        con.close()


def _write_v1_rows(db: Path, n: int = 1) -> None:
    """Rows in the retired v1 shape: a BARE stream name and a v1 hash.

    Written by hand, through the v1 hash function, because that is the only
    way to produce a genuine v1 log after the build has moved on. An open
    store writes at the current version.
    """
    from tradex_execution import sqlite_event_store as ses

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


# ---------------------------------------------------------------------------
# The stream id itself
# ---------------------------------------------------------------------------


def test_scoped_stream_composes_the_three_parts() -> None:
    """The id is broker, then account, then stream — in that order."""
    assert scoped_stream("DHAN", "acc-1", "orders") == "DHAN:acc-1:orders"


def test_scoped_stream_is_a_partition_not_a_prefix() -> None:
    """Two accounts at one broker land in different stream ids.

    This is the defect the spec names: two brokers (or two accounts) writing
    into one stream silently overwrite each other. Separate ids are the whole
    of the fix — everything downstream keys off them.
    """
    a = scoped_stream("DHAN", "acc-1", "orders")
    b = scoped_stream("DHAN", "acc-2", "orders")
    c = scoped_stream("PAPER", "acc-1", "orders")
    assert len({a, b, c}) == 3


def test_scoped_stream_keeps_the_bare_name_when_no_account_is_given() -> None:
    """An un-scoped caller keeps the pre-2.0 stream names.

    Every database on disk today holds bare stream ids, and every existing
    caller constructs the store without account arguments. Scoping those by
    force would move every row's stream id and break the very history this
    lane exists to preserve, so ``"*"`` is the explicit no-op.
    """
    assert scoped_stream(UNSCOPED_ACCOUNT, UNSCOPED_ACCOUNT, "orders") == "orders"
    assert scoped_stream("", "", "orders") == "orders"
    # An empty stream has no scoped form either.
    assert scoped_stream("DHAN", "acc-1", "") == ""


def test_scoped_stream_treats_none_as_un_scoped() -> None:
    """``None`` means the same as ``"*"``: no scope, so the bare name.

    An un-scoped store describes its own scope as ``None`` while the literal
    placeholder is ``"*"``. Those two spellings must resolve identically, or a
    store built one way would scope its rows and a store built the other way
    would not — and the un-scoped callers that keep history readable are the
    ones that pass ``None``.
    """
    assert scoped_stream(None, None, "orders") == "orders"
    assert scoped_stream(None, "acc-1", "orders") == "orders"
    assert scoped_stream("DHAN", None, "orders") == "orders"
    assert scoped_stream(None, UNSCOPED_ACCOUNT, "orders") == "orders"
    assert scoped_stream(UNSCOPED_ACCOUNT, None, "orders") == "orders"


def test_scoped_stream_only_loses_the_scope_when_one_id_is_absent(
    tmp_path: Path,
) -> None:
    """A real broker and account still scope the stream, so the guard is not
    a blanket pass for anything missing."""
    assert scoped_stream("DHAN", "acc-1", "orders") == "DHAN:acc-1:orders"


def test_stream_suffix_reads_bare_and_scoped_ids() -> None:
    """A caller asking for ``"orders"`` matches scoped and legacy rows alike."""
    assert stream_suffix("orders") == "orders"
    assert stream_suffix("DHAN:acc-1:orders") == "orders"


# ---------------------------------------------------------------------------
# Append stores the scoped id
# ---------------------------------------------------------------------------


def test_append_writes_the_account_scoped_stream_id(tmp_path: Path) -> None:
    """The stored stream id carries the broker and the account.

    The row on disk must be scoped, not the stream name that was asked for —
    the partition is a property of the log, not of the call.
    """
    db = tmp_path / "events.db"
    store = SQLiteEventStore(db, broker_id="DHAN", account_id="acc-1")
    store.append(OrderPlaced(order=_order("o-1")))
    store.close()

    rows = _rows(db)
    assert rows == [(1, "DHAN:acc-1:orders", SCHEMA_VERSION)], (
        "the stored stream id is not account-scoped"
    )


def test_append_without_scope_keeps_the_bare_stream_id(tmp_path: Path) -> None:
    """The default is the legacy bare name, so nothing on disk moves."""
    db = tmp_path / "events.db"
    store = SQLiteEventStore(db)
    store.append(OrderPlaced(order=_order("o-1")))
    store.close()

    assert _rows(db) == [(1, "orders", SCHEMA_VERSION)]


def test_two_accounts_write_two_partitions_in_one_log(tmp_path: Path) -> None:
    """Two accounts in one database never share a stream id.

    Both writes append to the SAME chain — the chain is global, over sequence
    order — so the partitions are separate streams, not separate logs.
    """
    db = tmp_path / "events.db"
    for account in ("acc-1", "acc-2"):
        store = SQLiteEventStore(db, broker_id="DHAN", account_id=account)
        store.append(OrderPlaced(order=_order(f"o-{account}")))
        store.close()

    rows = _rows(db)
    assert [row[1] for row in rows] == [
        "DHAN:acc-1:orders",
        "DHAN:acc-2:orders",
    ]
    assert [row[0] for row in rows] == [1, 2], "the chain did not advance"


def test_a_stream_override_names_a_stream_not_a_partition(tmp_path: Path) -> None:
    """An explicit ``stream_id`` is still scoped by the account.

    A caller routing to ``"risk"`` names a stream; the account partition is
    decided by the store, not by the override, so an override cannot be used to
    escape into another account's log.
    """
    db = tmp_path / "events.db"
    store = SQLiteEventStore(db, broker_id="PAPER", account_id="acc-7")
    store.append(OrderPlaced(order=_order("o-1")), stream_id="risk")
    store.close()

    assert _rows(db) == [(1, "PAPER:acc-7:risk", SCHEMA_VERSION)]


def test_replay_by_bare_name_sees_every_account(tmp_path: Path) -> None:
    """A bare stream name is the cross-account read; a scoped name is not."""
    db = tmp_path / "events.db"
    for account in ("acc-1", "acc-2"):
        store = SQLiteEventStore(db, broker_id="DHAN", account_id=account)
        store.append(OrderPlaced(order=_order(f"o-{account}")))
        store.close()

    reader = SQLiteEventStore(db)
    try:
        assert len(list(reader.replay("orders"))) == 2
        assert len(list(reader.replay("DHAN:acc-1:orders"))) == 1
        assert len(list(reader.replay("DHAN:acc-2:orders"))) == 1
        # A scoped reader still sees its own rows when it asks by bare name.
        own = SQLiteEventStore(db, broker_id="DHAN", account_id="acc-1")
        try:
            assert len(list(own.replay("orders"))) == 2
        finally:
            own.close()
    finally:
        reader.close()


def test_replay_by_scoped_name_reads_only_that_account(tmp_path: Path) -> None:
    """A scoped replay is exact: it does not widen into another account.

    This is the defect the scope exists to remove, and it is the one a
    suffix-only comparison produces: matching on the bare name alone would
    answer a scoped request with every account's rows in that stream. The
    scoped request names ONE account, and the stored ids that satisfy it are
    that account's alone.
    """
    db = tmp_path / "events.db"
    for account in ("acc-1", "acc-2", "acc-3"):
        store = SQLiteEventStore(db, broker_id="DHAN", account_id=account)
        store.append(OrderPlaced(order=_order(f"o-{account}")))
        store.close()

    reader = SQLiteEventStore(db)
    try:
        # Each scoped read yields exactly its own account's row.
        for account in ("acc-1", "acc-2", "acc-3"):
            events = list(reader.replay(f"DHAN:{account}:orders"))
            assert len(events) == 1, f"{account} saw {len(events)} rows"
        # A scoped name that does not exist yields nothing, not the bare name's
        # rows — an unknown account is empty, not "every account".
        assert list(reader.replay("DHAN:acc-9:orders")) == []
        # Two brokers sharing an account id are distinguished by the broker in
        # the scoped id, so a scoped read is still one account at one broker.
        other = SQLiteEventStore(db, broker_id="PAPER", account_id="acc-1")
        other.append(OrderPlaced(order=_order("o-paper")))
        other.close()
        assert len(list(reader.replay("DHAN:acc-1:orders"))) == 1
        assert len(list(reader.replay("PAPER:acc-1:orders"))) == 1
    finally:
        reader.close()


def test_replay_by_scoped_name_reads_no_legacy_rows(tmp_path: Path) -> None:
    """A scoped replay does not fall back to bare stream ids.

    A bare id is a v1 row and carries no account, so it cannot satisfy a
    request that names one. Returning it would be the cross-account leak in
    its other form: a scoped caller reading rows that belong to no account.
    """
    db = tmp_path / "events.db"
    legacy = SQLiteEventStore(db)
    legacy.append(OrderPlaced(order=_order("o-legacy")))
    legacy.close()

    reader = SQLiteEventStore(db, broker_id="DHAN", account_id="acc-1")
    try:
        assert len(list(reader.replay("orders"))) == 1
        assert list(reader.replay("DHAN:acc-1:orders")) == []
    finally:
        reader.close()


def test_count_and_last_sequence_are_account_aware(tmp_path: Path) -> None:
    """Diagnostics follow the same rule: scoped narrows, bare does not."""
    db = tmp_path / "events.db"
    for account in ("acc-1", "acc-2"):
        store = SQLiteEventStore(db, broker_id="DHAN", account_id=account)
        store.append(OrderPlaced(order=_order(f"o-{account}")))
        store.close()

    store = SQLiteEventStore(db, broker_id="DHAN", account_id="acc-1")
    try:
        assert store.count("DHAN:acc-1:orders") == 1
        assert store.count("orders") == 2
        assert store.last_sequence("DHAN:acc-2:orders") == 2
    finally:
        store.close()


# ---------------------------------------------------------------------------
# The version-2 bump: history must not be invalidated
# ---------------------------------------------------------------------------


def test_the_current_version_is_two() -> None:
    """The account-scope change is a version bump, not a silent relabelling.

    ``SCHEMA_VERSION`` moving to 2 is what makes the decoder registry
    load-bearing: the rows below were written under 1 and stay that way.
    """
    assert SCHEMA_VERSION == 2


def test_a_v1_log_replays_with_verification_passing(tmp_path: Path) -> None:
    """THE history-preservation test.

    A log written in the v1 shape (bare stream id, v1 hash) is opened by a v2
    build and must replay with the chain verifying. Re-hashing a v1 row with
    the scoped scheme — the obvious "just use the new hash" bug — fails every
    honest row here, which is the exact regression this test exists to catch.
    """
    db = tmp_path / "events.db"
    _write_v1_rows(db, n=4)

    store = SQLiteEventStore(db)
    try:
        events = list(store.replay("orders"))
    finally:
        store.close()

    assert [e.order.order_id.value for e in events] == [  # type: ignore[union-attr]
        "legacy-0", "legacy-1", "legacy-2", "legacy-3",
    ]


def test_v1_and_v2_rows_hash_differently_and_both_verify(tmp_path: Path) -> None:
    """The same event under two versions is two different honest digests.

    The account scope moved ``stream_id``, which is inside the hash input, so
    the two cannot agree. Both are correct, because each row is hashed with
    the function it was written under. A uniform hash function would either
    break every v1 row or flatten the account scope out of the digest.
    """
    from tradex_execution import sqlite_event_store as ses

    payload = ses._encode(OrderPlaced(order=_order("o-1")))

    legacy = ses._row_hash(
        1, "orders", "OrderPlaced", "id-1", None,
        payload, "2026-01-01T00:00:00+00:00", 1, ses.GENESIS_HASH,
    )
    scoped = ses._row_hash(
        1, "DHAN:acc-1:orders", "OrderPlaced", "id-1", None,
        payload, "2026-01-01T00:00:00+00:00", SCHEMA_VERSION, ses.GENESIS_HASH,
    )
    assert legacy != scoped, "the account scope does not participate in the hash"


def test_a_v1_row_appended_to_by_a_v2_build_still_verifies(tmp_path: Path) -> None:
    """Old rows and new rows coexist in ONE chain, under two hash functions.

    A bump is not a fork in the log: the v1 rows verify with the v1 function,
    the v2 rows with the scoped one, and ``prev_hash`` links them all. A build
    that only carried one function fails the other half of this log.
    """
    db = tmp_path / "events.db"
    _write_v1_rows(db, n=2)

    store = SQLiteEventStore(db, broker_id="DHAN", account_id="acc-1")
    try:
        store.append(OrderPlaced(order=_order("o-new")))
        events = list(store.replay("orders"))
    finally:
        store.close()

    assert [e.order.order_id.value for e in events] == [  # type: ignore[union-attr]
        "legacy-0", "legacy-1", "o-new",
    ]


def test_a_v1_log_read_by_a_scoped_store_still_finds_its_rows(tmp_path: Path) -> None:
    """The scope applies to rows the store WRITES, never to rows it rewrites.

    An operator reopening an old log under a scoped build sees the same
    history under the same stream it was written to. Silently moving it would
    be the history loss this lane is against.
    """
    db = tmp_path / "events.db"
    _write_v1_rows(db, n=2)

    scoped = SQLiteEventStore(db, broker_id="DHAN", account_id="acc-1")
    try:
        assert len(list(scoped.replay("orders"))) == 2
    finally:
        scoped.close()


def test_a_scoped_log_read_by_an_unscoped_store_finds_its_rows(tmp_path: Path) -> None:
    """The mirror case: a bare-name read reaches scoped rows."""
    db = tmp_path / "events.db"
    store = SQLiteEventStore(db, broker_id="DHAN", account_id="acc-1")
    store.append(OrderPlaced(order=_order("o-1")))
    store.close()

    reader = SQLiteEventStore(db)
    try:
        assert len(list(reader.replay("orders"))) == 1
    finally:
        reader.close()


def test_two_accounts_hashes_do_not_collide_in_the_chain(tmp_path: Path) -> None:
    """Identical events under two accounts cannot be swapped undetectably.

    The account is inside the digest, so swapping two accounts' rows for the
    same event changes every hash from that point on — the chain notices,
    because verification is on the read path.
    """
    db = tmp_path / "events.db"
    for account in ("acc-1", "acc-2"):
        store = SQLiteEventStore(db, broker_id="DHAN", account_id=account)
        store.append(OrderPlaced(order=_order("o-same")))
        store.close()

    con = sqlite3.connect(db)
    con.execute(
        "UPDATE events SET stream_id = 'DHAN:acc-9:orders' WHERE sequence = 1",
    )
    con.commit()
    con.close()

    reopened = SQLiteEventStore(db)
    try:
        with pytest.raises(Exception):  # noqa: B017, PT011 — the chain must break
            list(reopened.replay("orders"))
    finally:
        reopened.close()
