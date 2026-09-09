# Drawings/layout workspace persistence — design

Date: 2026-09-09. Opaque-blob workspace store (option a).

## Contract

REST under `/api/charts/workspace`:
- `PUT /{layout_id}` — upsert. Body: `{data: <opaque JSON>, revision?: int}`.
  When the stored revision is newer than the supplied one, 409 with the
  stored revision (optimistic concurrency: last-writer-wins only when the
  client explicitly passes `force: true` or no revision).
- `GET /{layout_id}` — `{layout_id, data, revision, updated_at}`; 404 when
  absent.
- `GET /` — list: `[{layout_id, updated_at, revision}]` (no blobs).
- `DELETE /{layout_id}` — 204; deleting an absent id is still 204.

`layout_id` is an opaque client-chosen string (e.g.
`NSE:RELIANCE_1m_default`) — the backend never parses it. `data` is opaque:
the engine owns the state shape (drawings, indicator list, panel layout,
replay position), the backend only persists it, so engine evolution never
requires a schema migration here.

## Storage

SQLite at `runtime/workspace.sqlite` (runtime/ is gitignored state, same
convention as the token/instrument caches). One table:

```
workspace(layout_id TEXT PRIMARY KEY, data TEXT NOT NULL,
          revision INTEGER NOT NULL, updated_at TEXT NOT NULL)
```

Store class `WorkspaceStore` in
`trading/src/tradex_trading/interface/workspace_store.py` (default
`:memory:` for tests; the route wires the file path). SQLite holds
connection-per-call like `execution/sqlite_store.py` — WAL not needed at
this write volume.

## Testing

`trading/tests/interface/test_workspace_persistence.py`:
- put/get round-trip preserves blob exactly;
- revision conflict -> 409; force override succeeds;
- list returns metadata without blobs; delete idempotent;
- 404 on absent get;
- layout_id validated (non-empty, sane length) -> 422.
Session-less TestClient pattern.
