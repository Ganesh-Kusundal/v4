"""Workspace persistence — opaque-blob store for chart state.

The engine (openalgo-charts host) owns the state shape; this module only
persists it (docs/superpowers/specs/2026-09-09-workspace-persistence-design.md).
SQLite, one row per layout_id, integer revision for optimistic concurrency.
"""
from __future__ import annotations

import json
import sqlite3
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
    """CRUD over the workspace table. Connection-per-call (execution store
    convention); ``:memory:`` keeps every connection on the same scratch db."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        if self._db_path == ":memory:":
            conn = sqlite3.connect(":memory:")
        else:
            conn = sqlite3.connect(self._db_path)
        conn.execute(_SCHEMA)
        return conn

    def put(self, layout_id: str, data: Any, revision: int | None, force: bool) -> tuple[int, bool]:
        """Upsert. Returns ``(new_revision, conflicted)``. A conflict means
        the stored revision is newer than the supplied one and ``force`` was
        not set — nothing was written in that case (caller answers 409)."""
        payload = json.dumps(data, separators=(",", ":"))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT revision FROM workspace WHERE layout_id = ?", (layout_id,)
            ).fetchone()
            if row is not None and revision is not None and not force and revision < row[0]:
                return row[0], True
            new_revision = (row[0] + 1) if row is not None else 1
            conn.execute(
                "INSERT INTO workspace (layout_id, data, revision, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(layout_id) DO UPDATE SET "
                "data = excluded.data, revision = excluded.revision, "
                "updated_at = excluded.updated_at",
                (layout_id, payload, new_revision, datetime.now(UTC).isoformat()),
            )
            return new_revision, False

    def get(self, layout_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
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
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT layout_id, revision, updated_at FROM workspace ORDER BY updated_at DESC"
            ).fetchall()
        return [
            {"layout_id": r[0], "revision": r[1], "updated_at": r[2]} for r in rows
        ]

    def delete(self, layout_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM workspace WHERE layout_id = ?", (layout_id,))
