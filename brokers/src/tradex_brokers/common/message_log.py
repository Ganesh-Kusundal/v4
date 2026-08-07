"""MessageLog — replayable, ordered event persistence.

``append`` stores any message; ``read`` returns ``MessageEnvelope`` wrappers in
append order. Domain objects serialize via their ``to_dict()``; dicts are
stored as-is.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    timestamp: datetime
    message: object


@runtime_checkable
class MessageLog(Protocol):
    def append(self, message: object) -> None: ...
    def read(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[MessageEnvelope]: ...
    def read_session(self, session_id: str) -> Iterator[object]: ...
    def clear(self) -> None: ...
    def close(self) -> None: ...


def _serialize(message: object) -> str:
    if isinstance(message, dict):
        return json.dumps(message)
    to_dict = getattr(message, "to_dict", None)
    if to_dict is not None:
        return json.dumps(to_dict())
    return json.dumps({"__repr__": repr(message)})


def _deserialize(payload: str) -> object:
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return payload


def _session_id(message: object) -> str | None:
    if isinstance(message, dict):
        value = message.get("session_id")
        return str(value) if value is not None else None
    return None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class InMemoryMessageLog:
    """In-memory message log for testing and short-lived sessions."""

    def __init__(self) -> None:
        self._rows: list[tuple[datetime, object]] = []

    def append(self, message: object) -> None:
        self._rows.append((_utcnow(), message))

    def read(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[MessageEnvelope]:
        for ts, message in self._rows:
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            yield MessageEnvelope(timestamp=ts, message=message)

    def read_session(self, session_id: str) -> Iterator[object]:
        for _ts, message in self._rows:
            if _session_id(message) == session_id:
                yield message

    def clear(self) -> None:
        self._rows.clear()

    def close(self) -> None:
        """Close the log — no-op for in-memory storage."""
        pass


class SQLiteMessageLog:
    """SQLite-backed log; ``close()`` is idempotent."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(self._path),
        )
        self._init_db()

    def _init_db(self) -> None:
        assert self._conn is not None
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                session_id TEXT,
                payload TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def append(self, message: object) -> None:
        assert self._conn is not None
        self._conn.execute(
            "INSERT INTO messages (ts, session_id, payload) VALUES (?, ?, ?)",
            (_utcnow().isoformat(), _session_id(message), _serialize(message)),
        )
        self._conn.commit()

    def read(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[MessageEnvelope]:
        assert self._conn is not None
        clauses: list[str] = []
        params: list[object] = []
        if start is not None:
            clauses.append("ts >= ?")
            params.append(start.isoformat())
        if end is not None:
            clauses.append("ts <= ?")
            params.append(end.isoformat())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        for row in self._conn.execute(
            f"SELECT ts, payload FROM messages{where} ORDER BY rowid",
            params,
        ):
            yield MessageEnvelope(
                timestamp=datetime.fromisoformat(row[0]),
                message=_deserialize(row[1]),
            )

    def read_session(self, session_id: str) -> Iterator[object]:
        assert self._conn is not None
        for row in self._conn.execute(
            "SELECT payload FROM messages WHERE session_id = ? ORDER BY rowid",
            (session_id,),
        ):
            yield _deserialize(row[0])

    def clear(self) -> None:
        assert self._conn is not None
        self._conn.execute("DELETE FROM messages")
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


__all__ = [
    "InMemoryMessageLog",
    "MessageEnvelope",
    "MessageLog",
    "SQLiteMessageLog",
]
