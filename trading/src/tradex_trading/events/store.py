"""Append-only event store — the single source of truth."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True, slots=True)
class Event:
    """Immutable event record."""

    event_id: str
    event_time: datetime
    processed_time: datetime
    correlation_id: str
    session_id: str
    type: str
    payload: dict
    sequence_number: int = 0  # Assigned by store.append()


class EventStore:
    """SQLite-backed append-only event log."""

    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                sequence_number INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                event_time TEXT NOT NULL,
                processed_time TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                type TEXT NOT NULL,
                payload TEXT NOT NULL
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_session_seq
            ON events(session_id, sequence_number)
        """)
        self._db.commit()

    def append(self, event: Event) -> Event:
        """Append an event. Returns the event with sequence_number assigned."""
        cursor = self._db.execute(
            """INSERT INTO events
               (event_id, event_time, processed_time, correlation_id,
                session_id, type, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.event_time.isoformat(),
                event.processed_time.isoformat(),
                event.correlation_id,
                event.session_id,
                event.type,
                json.dumps(event.payload),
            ),
        )
        self._db.commit()
        return Event(
            event_id=event.event_id,
            event_time=event.event_time,
            processed_time=event.processed_time,
            correlation_id=event.correlation_id,
            session_id=event.session_id,
            type=event.type,
            payload=event.payload,
            sequence_number=cursor.lastrowid,
        )

    def read_all(self, session_id: str) -> list[Event]:
        """Read all events for a session, in sequence order.

        Uses ROW_NUMBER() to produce a gapless per-session sequence,
        since global AUTOINCREMENT sequence_numbers have gaps when
        multiple sessions interleave writes.
        """
        cursor = self._db.execute(
            """SELECT event_id, event_time, processed_time, correlation_id,
                      session_id, type, payload,
                      ROW_NUMBER() OVER (ORDER BY sequence_number) AS per_session_seq
               FROM events WHERE session_id = ?
               ORDER BY sequence_number""",
            (session_id,),
        )
        return [self._row_to_event(row) for row in cursor]

    def read_after(self, session_id: str, after_seq: int) -> list[Event]:
        """Read events after a specific per-session sequence number.

        The after_seq refers to the gapless per-session sequence (1, 2, 3...),
        not the global AUTOINCREMENT value.
        """
        cursor = self._db.execute(
            """SELECT event_id, event_time, processed_time, correlation_id,
                      session_id, type, payload,
                      ROW_NUMBER() OVER (ORDER BY sequence_number) AS per_session_seq
               FROM events
               WHERE session_id = ?
               ORDER BY sequence_number""",
            (session_id,),
        )
        all_events = [self._row_to_event(row) for row in cursor]
        return [e for e in all_events if e.sequence_number > after_seq]

    def get_last_sequence(self, session_id: str) -> int:
        """Get the last sequence number for a session (0 if no events)."""
        cursor = self._db.execute(
            """SELECT MAX(sequence_number) FROM events WHERE session_id = ?""",
            (session_id,),
        )
        row = cursor.fetchone()
        return row[0] if row and row[0] is not None else 0

    def close(self) -> None:
        """Close the underlying database connection."""
        self._db.close()

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        return Event(
            event_id=row["event_id"],
            event_time=datetime.fromisoformat(row["event_time"]),
            processed_time=datetime.fromisoformat(row["processed_time"]),
            correlation_id=row["correlation_id"],
            session_id=row["session_id"],
            type=row["type"],
            payload=json.loads(row["payload"]),
            sequence_number=row["per_session_seq"],
        )
