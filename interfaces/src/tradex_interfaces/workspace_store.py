"""Workspace persistence — opaque-blob store for chart state.

The engine (openalgo-charts host) owns the state shape; this module only
persists it (docs/superpowers/specs/2026-09-09-workspace-persistence-design.md).
SQLite, one row per layout_id, integer revision for optimistic concurrency.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace (
    layout_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class WorkspaceStore:
    """CRUD over the workspace table.

    One connection for the object's lifetime, matching the five other SQLite
    stores in this repo (``sqlite_store.py``, ``sqlite_event_store.py``,
    ``risk_baseline_store.py``, ``validation_report_store.py``). It used to
    open a connection per call and describe that as "execution store
    convention" — it is not, and the description was false in a way that cost
    correctness: a fresh ``sqlite3.connect(":memory:")`` is a **new, empty
    database**, so the default path returned nothing on every read and dropped
    every write. The default server passes a real file path
    (``fastapi_app.py:202``), which is why the defect stayed latent.

    :meth:`close` releases the handle; the store is safe to share across
    threads (the connection is opened ``check_same_thread=False`` and every
    statement is serialised on one lock).
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the FastAPI workspace route is served from a
        # worker thread while the boot-time store is constructed on the main one.
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def put(self, layout_id: str, data: Any, revision: int | None, force: bool) -> tuple[int, bool]:
        """Upsert. Returns ``(new_revision, conflicted)``. A conflict means
        the stored revision is newer than the supplied one and ``force`` was
        not set — nothing was written in that case (caller answers 409)."""
        payload = json.dumps(data, separators=(",", ":"))
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT revision FROM workspace WHERE layout_id = ?", (layout_id,)
            ).fetchone()
            if row is not None and revision is not None and not force and revision < row[0]:
                return row[0], True
            new_revision = (row[0] + 1) if row is not None else 1
            self._conn.execute(
                "INSERT INTO workspace (layout_id, data, revision, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(layout_id) DO UPDATE SET "
                "data = excluded.data, revision = excluded.revision, "
                "updated_at = excluded.updated_at",
                (layout_id, payload, new_revision, datetime.now(UTC).isoformat()),
            )
            return new_revision, False

    def get(self, layout_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data, revision, updated_at FROM workspace WHERE layout_id = ?",
                (layout_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "layout_id": layout_id,
            "data": json.loads(row[0]),
            "revision": row[1],
            "updated_at": row[2],
        }

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT layout_id, revision, updated_at FROM workspace ORDER BY updated_at DESC"
            ).fetchall()
        return [
            {"layout_id": r[0], "revision": r[1], "updated_at": r[2]} for r in rows
        ]

    def delete(self, layout_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM workspace WHERE layout_id = ?", (layout_id,))

    def close(self) -> None:
        """Close the connection. Idempotent."""
        with self._lock:
            self._conn.close()
