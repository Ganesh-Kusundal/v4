"""Lane 4c, item 4 — the ``Snapshot`` type and the snapshots table.

The spec's requirement (``docs/specs/0002-production-readiness-programme``,
"Target data model"):

    Snapshot | NEW: stream_id, through_sequence, payload, created_at | yes
    Recovery replays the entire log into memory today, with no ceiling on
    growth. Written on reaching READY and every 10,000 events. Serialised as
    canonical JSON, not pickle, replacing the log below its
    through_sequence only after the venue reconciles.

And the invariants that hang off it:

    The log below a snapshot's through_sequence is never read except by that
    snapshot, and the prev_hash chain is verified across the boundary.

    A snapshot's through_sequence is the highest sequence it has folded, and
    replay starts strictly after it.

Two obligations follow for THIS lane, and only the first is a storage change:

* the store must be ABLE to write and read a snapshot on demand, keyed by the
  account stream, with a JSON payload and a position recovery can resume
  from. That is what the tests here cover.
* a caller must fold a real projection into one and must replay from it
  instead of from sequence 0. The fold is a recovery concern
  (``recover_trading_cache`` in ``recovery.py``) and lives outside this
  lane's files; the store's job is to hold the snapshot and report the
  position accurately. Wiring the fold is recorded as a follow-up, and the
  reason it is not done here is that doing it would mean editing
  ``recovery.py``, which this lane does not own.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import OrderPlaced
from tradex_domain.execution import Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.sqlite_event_store import (
    SNAPSHOT_INTERVAL,
    UNSCOPED_ACCOUNT,
    Snapshot,
    SQLiteEventStore,
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


def _fill_event(order_id: str, fill_id: str):
    from tradex_domain.events import OrderFilled
    from tradex_domain.execution import Fill

    return OrderFilled(
        fill=Fill(
            order_id=OrderId(order_id),
            instrument=_eq(),
            side=OrderSide.BUY,
            quantity=Quantity(Decimal("10")),
            price=Price(Decimal("2500")),
            fill_id=fill_id,
        ),
    )


def _store(db: Path, **kwargs) -> SQLiteEventStore:
    return SQLiteEventStore(db, **kwargs)


def _snapshots(db: Path) -> list[tuple]:
    import sqlite3

    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT stream_id, through_sequence, payload, reason "
            "FROM snapshots ORDER BY through_sequence",
        ).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# The snapshot table exists
# ---------------------------------------------------------------------------


def test_the_snapshots_table_exists(tmp_path: Path) -> None:
    """The table is created on open, so a caller can write a snapshot at once.

    A store that created the table lazily — on first snapshot — would make a
    READY snapshot impossible for a session that reaches READY without any
    other snapshot-worthy event.
    """
    db = tmp_path / "events.db"
    store = _store(db)
    store.close()

    assert _snapshots(db) == []


def test_snapshot_columns_hold_the_spec_fields() -> None:
    """``stream_id``, ``through_sequence``, ``payload``, ``created_at``."""
    snap = Snapshot(
        stream_id="PAPER:acc1:orders",
        through_sequence=42,
        payload={"cash": "1000"},
        created_at="2026-09-28T00:00:00+00:00",
        reason="ready",
    )
    assert snap.stream_id == "PAPER:acc1:orders"
    assert snap.through_sequence == 42
    assert snap.payload == {"cash": "1000"}
    assert snap.reason == "ready"


# ---------------------------------------------------------------------------
# Canonical JSON, not pickle
# ---------------------------------------------------------------------------


def test_snapshot_payload_is_canonical_json_not_pickle() -> None:
    """Equal states serialise to equal bytes.

    The spec is explicit that a snapshot is canonical JSON rather than pickle:
    a pickle couples the snapshot's readability to the class layout of the
    build that wrote it, and a snapshot is a recovery artefact that a LATER
    build has to read. Canonical JSON keeps the comparison byte-stable, which
    is also what makes two snapshots comparable by value.
    """
    state = {"cash": "1000", "orders": ["o-1", "o-2"], "nested": {"a": 1}}
    text = Snapshot.serialise(state)
    assert text == '{"cash":"1000","nested":{"a":1},"orders":["o-1","o-2"]}'
    # A pickle would not round-trip through a JSON parser at all.
    assert Snapshot.deserialise(text) == state


def test_a_written_snapshot_round_trips_as_json(tmp_path: Path) -> None:
    """The payload on disk is JSON, and it reads back as the same structure."""
    db = tmp_path / "events.db"
    store = _store(db, broker_id="PAPER", account_id="acc1")
    store.append(OrderPlaced(order=_order("o-1")))
    snap = store.write_snapshot({"cash": "1000", "orders": 3})
    store.close()

    assert snap is not None
    rows = _snapshots(db)
    assert len(rows) == 1
    stream_id, through, payload, reason = rows[0]
    assert stream_id == "PAPER:acc1:orders"
    assert through == 1
    assert reason == "periodic"
    # The stored payload is JSON, verifiable by parsing it.
    import json

    assert json.loads(payload) == {"cash": "1000", "orders": 3}


# ---------------------------------------------------------------------------
# through_sequence: replay starts strictly after it
# ---------------------------------------------------------------------------


def test_through_sequence_is_the_last_folded_sequence(tmp_path: Path) -> None:
    """A snapshot's position is the highest sequence it folded.

    Replay starts strictly AFTER it, so the position must be exactly the last
    row folded — not the count, not the next row. Here three events are
    appended, so a snapshot of the whole stream folds through sequence 3.
    """
    db = tmp_path / "events.db"
    store = _store(db, broker_id="PAPER", account_id="acc1")
    store.append(OrderPlaced(order=_order("o-1")))
    store.append(OrderPlaced(order=_order("o-2")))
    store.append(OrderPlaced(order=_order("o-3")))
    snap = store.write_snapshot({"cash": "100"})
    store.close()

    assert snap is not None
    assert snap.through_sequence == 3


def test_the_log_below_a_snapshot_still_chain_verifies(tmp_path: Path) -> None:
    """The chain is verified ACROSS the snapshot boundary.

    A snapshot does not free the store from proving the log it folds: the rows
    below ``through_sequence`` are part of the same global chain, and reading
    the store still walks them. A snapshot that excused the rows below it from
    verification would let a tamper hide behind a snapshot.
    """
    db = tmp_path / "events.db"
    store = _store(db)
    for i in range(4):
        store.append(OrderPlaced(order=_order(f"o-{i}")))
    store.write_snapshot(None)
    store.close()

    # Break a row BELOW the snapshot's position, then read the whole store.
    import sqlite3

    con = sqlite3.connect(db)
    con.execute("UPDATE events SET event_type = 'OrderCancelled' WHERE sequence = 2")
    con.commit()
    con.close()

    from tradex_execution.sqlite_event_store import EventChainError

    reopened = _store(db)
    try:
        with pytest.raises(EventChainError):
            list(reopened.replay("orders"))
    finally:
        reopened.close()


def test_latest_snapshot_is_the_newest_by_position(tmp_path: Path) -> None:
    """``latest_snapshot`` reads by position, not by insert order."""
    db = tmp_path / "events.db"
    store = _store(db, broker_id="PAPER", account_id="acc1")
    store.append(OrderPlaced(order=_order("o-1")))
    store.write_snapshot({"n": 1}, through_sequence=1)
    store.append(OrderPlaced(order=_order("o-2")))
    store.write_snapshot({"n": 2}, through_sequence=2)
    store.close()

    latest = store_latest(db)
    assert latest is not None
    assert latest.through_sequence == 2
    assert latest.payload == {"n": 2}


def store_latest(db: Path) -> Snapshot | None:
    store = _store(db, broker_id="PAPER", account_id="acc1")
    try:
        return store.latest_snapshot("orders")
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Written on reaching READY
# ---------------------------------------------------------------------------


def test_a_ready_snapshot_records_the_position_and_no_state(tmp_path: Path) -> None:
    """The READY snapshot is a position marker: it folds nothing of its own.

    Reaching READY is a boot milestone, not a fold. Its snapshot exists so the
    next replay starts after the position the session reached READY at, and
    its payload is empty because the READY transition itself carries no state
    to carry forward.
    """
    db = tmp_path / "events.db"
    store = _store(db, broker_id="PAPER", account_id="acc1")
    store.append(OrderPlaced(order=_order("o-1")))
    store.append(OrderPlaced(order=_order("o-2")))
    ready = store.write_snapshot(None, reason="ready")
    store.close()

    assert ready is not None
    assert ready.reason == "ready"
    assert ready.through_sequence == 2
    assert ready.payload == {}


def test_a_ready_snapshot_persists_across_a_reopen(tmp_path: Path) -> None:
    """The snapshot survives a restart, and its position is what a resume uses."""
    db = tmp_path / "events.db"
    store = _store(db, broker_id="PAPER", account_id="acc1")
    store.append(OrderPlaced(order=_order("o-1")))
    store.write_snapshot(None, reason="ready")
    store.close()

    store = _store(db, broker_id="PAPER", account_id="acc1")
    try:
        latest = store.latest_snapshot("orders")
    finally:
        store.close()

    assert latest is not None
    assert latest.reason == "ready"
    assert latest.through_sequence == 1


def test_a_store_with_no_events_has_no_snapshot(tmp_path: Path) -> None:
    """There is nothing to snapshot when no event has been appended."""
    db = tmp_path / "events.db"
    store = _store(db)
    try:
        assert store.write_snapshot(None) is None
        assert store.latest_snapshot() is None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Written every 10,000 events
# ---------------------------------------------------------------------------


def test_snapshot_interval_is_ten_thousand() -> None:
    """The spec sets the interval; a different number changes the guarantee."""
    assert SNAPSHOT_INTERVAL == 10_000


def test_maybe_snapshot_writes_once_the_interval_elapses(tmp_path: Path) -> None:
    """``maybe_snapshot`` fires exactly at the interval and resets the counter.

    The counter is the number of appends since the last snapshot, not the
    total — a store that reopens above the interval and has not snapshot since
    would otherwise fire on its first append, and a store that snapshots every
    10,000 events must not fire again 10,000 TOTAL events later.
    """
    db = tmp_path / "events.db"
    store = _store(db)
    try:
        for i in range(SNAPSHOT_INTERVAL - 1):
            store.append(OrderPlaced(order=_order(f"o-{i}")))
        assert store.maybe_snapshot({"n": 1}) is None, (
            "a snapshot fired before the interval elapsed"
        )
        store.append(OrderPlaced(order=_order("o-last")))
        first = store.maybe_snapshot({"n": 1})
        assert first is not None, "the interval elapsed and no snapshot fired"
        assert first.through_sequence == SNAPSHOT_INTERVAL

        # The counter reset, so the next 10,000 events do not fire early.
        store.append(OrderPlaced(order=_order("o-next")))
        assert store.maybe_snapshot({"n": 2}) is None
    finally:
        store.close()


def test_a_snapshot_resets_the_counter_after_a_reopen(tmp_path: Path) -> None:
    """A reopen measures from the last snapshot, not from the log's start.

    Without this the counter would be seeded with the whole row count and a
    store reopening at 10,000 rows — already snapshotted — would fire another
    snapshot on its first append.
    """
    db = tmp_path / "events.db"
    store = _store(db)
    try:
        for i in range(SNAPSHOT_INTERVAL):
            store.append(OrderPlaced(order=_order(f"o-{i}")))
        snap = store.maybe_snapshot({"n": 1})
        assert snap is not None
    finally:
        store.close()

    reopened = _store(db)
    try:
        # One append is not a full interval since the snapshot.
        reopened.append(OrderPlaced(order=_order("o-after")))
        assert reopened.maybe_snapshot({"n": 2}) is None, (
            "the counter was seeded from the row count, not the snapshot"
        )
    finally:
        reopened.close()


def test_periodic_snapshots_accumulate_in_the_table(tmp_path: Path) -> None:
    """More than one periodic snapshot is kept, at increasing positions."""
    db = tmp_path / "events.db"
    store = _store(db)
    try:
        store.append(OrderPlaced(order=_order("o-1")))
        first = store.write_snapshot({"n": 1}, through_sequence=1)
        store.append(OrderPlaced(order=_order("o-2")))
        second = store.write_snapshot({"n": 2}, through_sequence=2)
    finally:
        store.close()

    assert first is not None and second is not None
    rows = _snapshots(db)
    assert [row[1] for row in rows] == [1, 2]
    assert [row[3] for row in rows] == ["periodic", "periodic"]


# ---------------------------------------------------------------------------
# Account scoping
# ---------------------------------------------------------------------------


def test_a_snapshot_is_keyed_by_the_account_stream(tmp_path: Path) -> None:
    """Two accounts' snapshots are two rows, not one shared one.

    A snapshot is a folded view of ONE account's stream, so the account scope
    is part of the key. A store that keyed snapshots on the bare stream name
    would let one account's fold overwrite another's.
    """
    db = tmp_path / "events.db"
    for account in ("acc-1", "acc-2"):
        store = _store(db, broker_id="PAPER", account_id=account)
        store.append(OrderPlaced(order=_order(f"o-{account}")))
        store.write_snapshot({"account": account})
        store.close()

    rows = _snapshots(db)
    assert {row[0] for row in rows} == {
        "PAPER:acc-1:orders",
        "PAPER:acc-2:orders",
    }


def test_latest_snapshot_reads_by_bare_name_from_either_scope(
    tmp_path: Path,
) -> None:
    """A bare-name lookup finds the snapshot whichever scope wrote it."""
    db = tmp_path / "events.db"
    store = _store(db, broker_id="PAPER", account_id="acc1")
    store.append(OrderPlaced(order=_order("o-1")))
    store.write_snapshot({"n": 1}, reason="ready")
    store.close()

    unscoped = _store(db)
    try:
        latest = unscoped.latest_snapshot("orders")
    finally:
        unscoped.close()
    assert latest is not None
    assert latest.payload == {"n": 1}


def test_snapshot_count_is_scoped(tmp_path: Path) -> None:
    """``snapshot_count`` narrows to the account, and ``"*"`` counts all."""
    db = tmp_path / "events.db"
    for account in ("acc-1", "acc-2"):
        store = _store(db, broker_id="PAPER", account_id=account)
        store.append(OrderPlaced(order=_order(f"o-{account}")))
        store.write_snapshot({"account": account})
        store.close()

    reader = _store(db, broker_id="PAPER", account_id="acc-1")
    try:
        assert reader.snapshot_count("orders") == 1
        assert reader.snapshot_count() == 2
    finally:
        reader.close()


def test_an_unscoped_store_still_snapshots_a_bare_stream(tmp_path: Path) -> None:
    """The default scope writes under the bare name, as it does for events."""
    db = tmp_path / "events.db"
    store = _store(db)
    store.append(OrderPlaced(order=_order("o-1")))
    snap = store.write_snapshot({"n": 1})
    store.close()

    assert snap is not None
    assert snap.stream_id == "orders"
    assert _snapshots(db) == [("orders", 1, '{"n":1}', "periodic")]


# ---------------------------------------------------------------------------
# Un-scoped placeholder
# ---------------------------------------------------------------------------


def test_the_unscoped_placeholder_is_not_an_account() -> None:
    """``"*"`` means "no account scope", and it keeps the bare stream name."""
    assert UNSCOPED_ACCOUNT == "*"
