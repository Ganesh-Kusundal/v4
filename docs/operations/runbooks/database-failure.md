# Runbook: Database / persistence failure (SQLite & workspace)

**Scope:** Optional order/idempotency SQLite (`PersistenceConfig.path`), workspace blob DB (`TRADEX_WORKSPACE_DB`), and runtime directory (`TRADEX_RUNTIME_DIR`).

**Severity:** HIGH for order SQLite (durability/idempotency); MEDIUM for workspace (layout loss, not trading truth).

## When this applies

- Boot errors opening SQLite (`sqlite3.OperationalError`, disk full, permission denied).
- Orders not surviving restart despite persistence enabled.
- Idempotency stuck after crash — duplicate-submit guard errors referencing manual reconcile.
- Workspace save/load failures; revision conflicts in UI.
- `engine.errors.total` increment from persistence subscribers.

## Detection

| Signal | How to check |
|--------|----------------|
| Logs | Exceptions from `SQLiteOrderStore`, `attach_order_persistence`, workspace routes. |
| Disk | `df` on volume hosting DB; SQLite WAL growth. |
| API | Order mutations fail with 5xx; workspace 409/conflict responses. |
| Files | Configured `persistence.path`; `TRADEX_WORKSPACE_DB` if set. |
| Metrics | `engine.errors.total`, `bus.messages.subscriber_errors` during order events. |

```bash
df -h "$(dirname "$PERSIST_PATH")"
sqlite3 "$PERSIST_PATH" "PRAGMA integrity_check;"   # when path configured
curl -sS http://127.0.0.1:8000/metrics | rg 'engine\.errors|bus\.messages\.subscriber'
```

## Halt (immediate)

1. **Trip kill switch** on live session if SQLite writes fail mid-mutation — OMS and idempotency may be inconsistent ([unknown-submission.md](./unknown-submission.md)).
2. Stop automated strategies.
3. Do not delete WAL/SHM files while process is running.
4. Workspace failures alone do **not** require trading halt — but do not rely on saved layout for safety state.

## Diagnose

1. **Order DB vs workspace DB** — separate concerns; identify which file errors.
2. **Read-only filesystem / permissions:** User running `serve` must write runtime dir.
3. **Corruption:** `PRAGMA integrity_check` not `ok`.
4. **Multi-process:** Two live writers corrupt SQLite — enforce single writer lock (live boot).
5. **Disk full:** Writes fail silently until exception propagates.
6. **Idempotency table:** Rows blocking new submits — correlate with unknown submission incidents.

## Recover

### Order / idempotency SQLite

1. Stop process cleanly (`session.stop()` / SIGTERM).
2. Backup `{path}`, `{path}-wal`, `{path}-shm`.
3. Run `PRAGMA integrity_check`; if corrupt, restore from backup or rebuild empty store **only** after broker reconcile ([oms-recovery.md](./oms-recovery.md)).
4. For stuck idempotency keys, follow store error guidance: reconcile with broker before `release()`.
5. Restart with verified `PersistenceConfig.path`; confirm `attach_order_persistence` subscribing.

### Workspace DB

1. If conflict — user must choose revision (no silent overwrite per remediation design).
2. Restore workspace file from backup or delete to factory layout (chart state only).
3. Set `TRADEX_WORKSPACE_DB` to isolated path for E2E/drills.

### Runtime directory

1. Default `.tradex_v4` under repo or `TRADEX_RUNTIME_DIR` — ensure writable.
2. Token/state files for brokers live here — do not commit to git.

## Audit (post-incident)

1. Preserve corrupt DB copy and application logs.
2. List orders submitted during write failure window; full broker reconcile mandatory.
3. Verify idempotency keys neither double-submit nor permanently lock.
4. Document disk capacity fix and monitoring on volume.
5. Confirm single-writer policy for live.

## Related metrics & alerts

| Metric | Alert |
|--------|--------|
| `engine.errors.total` | rate spike |
| `bus.messages.subscriber_errors` | during order lifecycle |
| Disk free space | < 10% on DB volume |

Persistence is **opt-in** by default — production live should explicitly set `path` or accept in-memory OMS limits. See [metrics-catalog.md](../metrics-catalog.md).
