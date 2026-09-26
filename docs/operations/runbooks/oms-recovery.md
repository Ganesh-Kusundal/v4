# Runbook: OMS recovery (restart & event replay)

**Scope:** Rebuilding the in-memory OMS projection (`TradingCache`) from durable storage and broker truth after process crash, deploy, or split-brain.

**Severity:** CRITICAL when live trading resumes without verified projection.

## When this applies

- Process restart with `PersistenceConfig.path` pointing at SQLite order store.
- Empty or partial order list in UI/API after restart while broker shows working orders.
- `SessionRecovery.recover()` used in tests/single-process flows (event store replay).
- Startup logs from live reconciliation (`startup drift`, order-book refresh).

## Detection

| Signal | How to check |
|--------|----------------|
| API | `GET /orders` / CLI `orders` empty or stale vs broker portal. |
| Startup | `_run_startup_reconciliation` warnings; session stuck in `NEW` if reconcile tripped kill switch. |
| Persistence | SQLite file at configured path (default opt-in — in-memory if unset). |
| Events | Bus persistence via `attach_order_persistence` mirroring lifecycle events. |
| Recovery result | `SessionRecovery` → `orders_recovered`, `events_replayed` (operator scripts/tests). |

```bash
# After serve boot (example)
curl -sS -H "X-API-Key: $TRADEX_SERVE_API_KEY" http://127.0.0.1:8000/orders
python -m tradex_trading.interface.cli orders
ls -la "${TRADEX_RUNTIME_DIR:-.tradex_v4}"  # runtime + optional DB path from config
```

## Halt (immediate)

1. **Live mode:** If reconciliation trips kill switch at startup, session **does not** call `start()` — treat as halted until drift resolved ([reconciliation-drift.md](./reconciliation-drift.md)).
2. Do not enable strategies or arm UI until OMS matches broker orderbook.
3. Trip kill switch manually if you must restart twice without confirming book state.

## Diagnose

1. **Persistence enabled?** If `path` is None, OMS is in-memory only — restart loses local state; broker is truth.
2. **SQLite integrity:** File readable, no corruption errors on boot; check idempotency table for stuck reservations ([unknown-submission.md](./unknown-submission.md)).
3. **Event replay vs broker reconcile:** Target architecture expects event store authoritative; current production path emphasizes broker orderbook refresh at startup for status alignment.
4. **Bracket legs:** Verify protective columns restored from SQLite (`stop_loss_price`, `target_price`).
5. **Writer lock:** Live mode single-writer — ensure no second live process shares SQLite.

## Recover

### A. Standard live restart (wired in `startup.py`)

1. Boot session; `_run_startup_reconciliation` pulls broker orderbook + positions.
2. `engine.reconcile` compares local vs broker (side-effect free).
3. Status refresh: cache updated from broker rows when status differs.
4. If no HIGH/CRITICAL drift → `session.start()` → READY.
5. If drift → kill switch trip → **do not** start; fix drift first.

### B. Manual reconcile (running session)

1. Fetch broker orderbook and positions.
2. Call `engine.reconcile(broker_orders=book, broker_positions=positions)`.
3. Apply intentional cache updates from broker for status mismatches.
4. Resolve UNKNOWN orders before clearing kill switch.

### C. Event-backed rebuild (`SessionRecovery`)

For deployments with `EventStore` + `OrderRepository` wired:

1. `SessionRecovery(event_store, order_repository).recover()`.
2. Verify `orders_recovered` vs expected event count.
3. Still run broker reconcile before live admission — projection alone is not sufficient for money.

### D. Feed + OMS

OMS recovery does **not** replace feed recovery. After restart, wait for `feed_status.ready` before live orders ([stale-feed.md](./stale-feed.md)).

## Audit (post-incident)

1. Diff local orders vs broker export at T0 (restart) and T1 (ready).
2. List idempotency keys replayed (`orders.idempotency_replay` metric).
3. Confirm no orders submitted while session state was not READY.
4. Archive SQLite copy before manual edits.
5. Record deploy version and config hash (`AppConfig`).

## Related metrics & alerts

| Metric | Suggested alert |
|--------|-----------------|
| `kill_switch.tripped` at boot | immediate page |
| `orders.idempotency_replay` | spike after restart |
| `engine.errors.total` | persistence/subscriber errors |

See [metrics-catalog.md](../metrics-catalog.md).
