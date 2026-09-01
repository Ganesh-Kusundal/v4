"""Tests for the append-only event store (single source of truth)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import UTC, datetime

import pytest

from tradex_trading.events.store import Event, EventStore


@pytest.fixture
def store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    store = EventStore(path)
    yield store
    store.close()
    os.unlink(path)


def test_append_and_read(store):
    event = Event(
        event_id="evt-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, 9, 15, 1, tzinfo=UTC),
        correlation_id="corr-001",
        session_id="sess-001",
        type="OrderPlaced",
        payload={"order_id": "ord-001", "instrument": "NSE:RELIANCE"},
    )
    appended = store.append(event)
    assert appended.sequence_number == 1

    events = store.read_all("sess-001")
    assert len(events) == 1
    assert events[0].event_id == "evt-001"
    assert events[0].sequence_number == 1


def test_sequence_numbers_are_monotonic(store):
    for i in range(5):
        store.append(Event(
            event_id=f"evt-{i:03d}",
            event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
            processed_time=datetime(2026, 1, 1, 9, 15, 1, tzinfo=UTC),
            correlation_id=f"corr-{i:03d}",
            session_id="sess-001",
            type="TestEvent",
            payload={"index": i},
        ))

    events = store.read_all("sess-001")
    seqs = [e.sequence_number for e in events]
    assert seqs == [1, 2, 3, 4, 5]


def test_read_after(store):
    for i in range(5):
        store.append(Event(
            event_id=f"evt-{i:03d}",
            event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
            processed_time=datetime(2026, 1, 1, 9, 15, 1, tzinfo=UTC),
            correlation_id=f"corr-{i:03d}",
            session_id="sess-001",
            type="TestEvent",
            payload={"index": i},
        ))

    events = store.read_after("sess-001", 3)
    assert len(events) == 2
    assert events[0].sequence_number == 4
    assert events[1].sequence_number == 5
    # Verify full ordering: sequences must be gapless and ascending
    seqs = [e.sequence_number for e in events]
    assert seqs == [4, 5]
    # Verify event_ids match expected ordering
    assert events[0].event_id == "evt-003"
    assert events[1].event_id == "evt-004"


def test_isolation_between_sessions(store):
    store.append(Event(
        event_id="evt-a",
        event_time=datetime(2026, 1, 1, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, tzinfo=UTC),
        correlation_id="corr-a",
        session_id="sess-a",
        type="TestEvent",
        payload={},
    ))
    store.append(Event(
        event_id="evt-b",
        event_time=datetime(2026, 1, 1, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, tzinfo=UTC),
        correlation_id="corr-b",
        session_id="sess-b",
        type="TestEvent",
        payload={},
    ))

    assert len(store.read_all("sess-a")) == 1
    assert len(store.read_all("sess-b")) == 1
    assert store.read_all("sess-a")[0].event_id == "evt-a"


def test_get_last_sequence_empty(store):
    assert store.get_last_sequence("sess-empty") == 0


def test_get_last_sequence_returns_max(store):
    for i in range(3):
        store.append(Event(
            event_id=f"evt-{i:03d}",
            event_time=datetime(2026, 1, 1, tzinfo=UTC),
            processed_time=datetime(2026, 1, 1, tzinfo=UTC),
            correlation_id=f"corr-{i:03d}",
            session_id="sess-001",
            type="TestEvent",
            payload={},
        ))
    assert store.get_last_sequence("sess-001") == 3


def test_duplicate_event_id_raises_integrity_error(store):
    event = Event(
        event_id="evt-dup",
        event_time=datetime(2026, 1, 1, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, tzinfo=UTC),
        correlation_id="corr-dup",
        session_id="sess-001",
        type="TestEvent",
        payload={},
    )
    store.append(event)

    with pytest.raises(sqlite3.IntegrityError):
        store.append(event)


def test_payload_roundtrip_preserves_dict(store):
    payload = {"key": "value", "nested": {"a": 1}, "list": [1, 2, 3]}
    event = Event(
        event_id="evt-payload",
        event_time=datetime(2026, 1, 1, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, tzinfo=UTC),
        correlation_id="corr-payload",
        session_id="sess-001",
        type="TestEvent",
        payload=payload,
    )
    store.append(event)

    events = store.read_all("sess-001")
    assert events[0].payload == payload


def test_read_after_returns_empty_when_no_events(store):
    events = store.read_after("sess-001", 100)
    assert events == []


def test_read_all_returns_empty_for_unknown_session(store):
    events = store.read_all("nonexistent-session")
    assert events == []


def test_gapless_per_session_sequence_with_interleaved_sessions(store):
    """When multiple sessions interleave writes, per-session sequences must be gapless."""
    # Interleave writes: sess-a, sess-b, sess-a, sess-b, sess-a
    store.append(Event(
        event_id="evt-a1",
        event_time=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, 9, 0, 1, tzinfo=UTC),
        correlation_id="corr-a1",
        session_id="sess-a",
        type="TestEvent",
        payload={"n": 1},
    ))
    store.append(Event(
        event_id="evt-b1",
        event_time=datetime(2026, 1, 1, 9, 1, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, 9, 1, 1, tzinfo=UTC),
        correlation_id="corr-b1",
        session_id="sess-b",
        type="TestEvent",
        payload={"n": 1},
    ))
    store.append(Event(
        event_id="evt-a2",
        event_time=datetime(2026, 1, 1, 9, 2, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, 9, 2, 1, tzinfo=UTC),
        correlation_id="corr-a2",
        session_id="sess-a",
        type="TestEvent",
        payload={"n": 2},
    ))
    store.append(Event(
        event_id="evt-b2",
        event_time=datetime(2026, 1, 1, 9, 3, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, 9, 3, 1, tzinfo=UTC),
        correlation_id="corr-b2",
        session_id="sess-b",
        type="TestEvent",
        payload={"n": 2},
    ))
    store.append(Event(
        event_id="evt-a3",
        event_time=datetime(2026, 1, 1, 9, 4, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, 9, 4, 1, tzinfo=UTC),
        correlation_id="corr-a3",
        session_id="sess-a",
        type="TestEvent",
        payload={"n": 3},
    ))

    # sess-a should have gapless sequence [1, 2, 3]
    events_a = store.read_all("sess-a")
    seqs_a = [e.sequence_number for e in events_a]
    assert seqs_a == [1, 2, 3], f"Expected gapless [1, 2, 3], got {seqs_a}"
    assert [e.event_id for e in events_a] == ["evt-a1", "evt-a2", "evt-a3"]

    # sess-b should have gapless sequence [1, 2]
    events_b = store.read_all("sess-b")
    seqs_b = [e.sequence_number for e in events_b]
    assert seqs_b == [1, 2], f"Expected gapless [1, 2], got {seqs_b}"
    assert [e.event_id for e in events_b] == ["evt-b1", "evt-b2"]


def test_read_after_uses_gapless_sequence(store):
    """read_after should filter by gapless per-session sequence, not global AUTOINCREMENT."""
    # Interleave: a1, b1, a2, b2, a3
    for eid, sid in [("evt-a1", "sess-a"), ("evt-b1", "sess-b"),
                     ("evt-a2", "sess-a"), ("evt-b2", "sess-b"),
                     ("evt-a3", "sess-a")]:
        store.append(Event(
            event_id=eid,
            event_time=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            processed_time=datetime(2026, 1, 1, 9, 0, 1, tzinfo=UTC),
            correlation_id=f"corr-{eid}",
            session_id=sid,
            type="TestEvent",
            payload={},
        ))

    # After gapless seq 1 for sess-a, we should get [2, 3]
    events = store.read_after("sess-a", 1)
    seqs = [e.sequence_number for e in events]
    assert seqs == [2, 3], f"Expected [2, 3], got {seqs}"
    assert [e.event_id for e in events] == ["evt-a2", "evt-a3"]


def test_close_releases_connection(store):
    """close() should release the database connection without error."""
    store.append(Event(
        event_id="evt-before-close",
        event_time=datetime(2026, 1, 1, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, tzinfo=UTC),
        correlation_id="corr-1",
        session_id="sess-001",
        type="TestEvent",
        payload={},
    ))
    store.close()
    # After close, operations should fail
    with pytest.raises(sqlite3.ProgrammingError):
        store.read_all("sess-001")


def test_store_is_thread_safe(tmp_path):
    """Concurrent appends from multiple threads must not corrupt the log.

    The broker stream handler (live mode) runs on a different thread than
    the session that created the store. With sqlite's default
    check_same_thread=True this raises ProgrammingError; with the lock it
    must serialize cleanly and preserve exact sequence ordering.
    """
    import threading

    store = EventStore(str(tmp_path / "threads.db"))
    n_threads, per_thread = 4, 50
    barrier = threading.Barrier(n_threads)

    def worker(tid: int) -> None:
        barrier.wait()  # maximize contention
        for i in range(per_thread):
            store.append(
                Event(
                    event_id=f"evt-t{tid}-{i}",
                    event_time=datetime.now(UTC),
                    processed_time=datetime.now(UTC),
                    correlation_id=f"corr-t{tid}-{i}",
                    session_id="thread-session",
                    type="OrderPlaced",
                    payload={"thread": tid, "i": i},
                )
            )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = store.read_all("thread-session")
    assert len(events) == n_threads * per_thread

    # Sequence numbers must be a gapless 1..N ordering (serialized appends)
    seqs = [e.sequence_number for e in events]
    assert seqs == sorted(seqs)
    assert min(seqs) == 1
    assert max(seqs) == n_threads * per_thread

    # No duplicate event_ids (each append assigned a unique sequence)
    ids = {e.event_id for e in events}
    assert len(ids) == n_threads * per_thread

    store.close()
