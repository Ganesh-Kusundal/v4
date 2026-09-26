"""Durable SQLite append-only event log (R1).

Replaces the in-memory ``deque(maxlen=10_000)`` message log so the bus
survives process restarts. On a fresh boot a caller can ask
:meth:`ReactiveBus.replay` to drain the live ``_pending`` queue first, then
walk this log from id 0 onwards to rebuild a "what did we think happened?"
view for reconciliation.

The schema is intentionally tiny: one table, three columns, no migrations.
``payload`` is the JSON-serialised event (best-effort: dataclass via
``tradex_domain.serialization.to_dict``, else ``repr()`` plus the wall-clock
timestamp). The ``id`` autoincrements so ``replay(after_id=N)`` is a
range scan, not a full table walk.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    payload BLOB NOT NULL
)
"""


def _serialize(event: object) -> bytes:
    """Best-effort JSON for the event payload.

    Falls back to ``repr()`` (with a timestamp key) when the value isn't
    JSON-serialisable directly or via ``tradex_domain.serialization.to_dict``.
    """
    try:  # pragma: no cover - exercised via integration tests
        from tradex_domain.serialization import to_dict as _to_dict
    except ImportError:  # pragma: no cover - tradex_domain optional
        _to_dict = None

    candidate: Any = event
    if _to_dict is not None and not isinstance(event, (str, int, float, bool, type(None))):
        try:
            candidate = _to_dict(event)
        except Exception:  # noqa: BLE001 - best-effort
            candidate = event
    try:
        return json.dumps(candidate, default=str).encode("utf-8")
    except TypeError:
        return json.dumps({"_repr": repr(event), "_ts": time.time()}).encode("utf-8")


class SQLEventLog:
    """Append-only SQLite-backed event log.

    Drop-in compatible with the bus's existing ``log.append(event)`` /
    ``iter(log)`` shape: any container with ``append`` and ``__iter__`` is a
    valid message log for :class:`ReactiveBus`.
    """

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        # WAL = concurrent readers (replay) don't block the writer (publish).
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()

    def append(self, event: object) -> None:
        """Persist one event. Compatible with the bus's ``self._log.append(...)``."""
        payload = _serialize(event)
        self._conn.execute(
            "INSERT INTO events (ts, payload) VALUES (?, ?)",
            (time.time(), payload),
        )
        self._conn.commit()

    def replay(self, after_id: int = 0) -> Iterator[object]:
        """Yield events in insertion order with id > ``after_id``.

        Snapshot read: no transaction is held across the iterator, so it's
        safe to call concurrently with new :meth:`append` writes — the
        caller just sees whatever was committed at fetch time.
        """
        cursor = self._conn.execute(
            "SELECT id, payload FROM events WHERE id > ? ORDER BY id",
            (after_id,),
        )
        for _id, payload in cursor:
            try:
                yield json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):  # pragma: no cover
                yield payload

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()


