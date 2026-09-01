"""Tests for the append-only event store (single source of truth)."""

from __future__ import annotations

import os
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

    with pytest.raises(Exception):
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
