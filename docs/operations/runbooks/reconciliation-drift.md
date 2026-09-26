# Runbook: Reconciliation drift (local vs broker)

**Scope:** `ReconciliationEngine` drift between local OMS/positions and broker snapshots — startup and periodic operator checks.

**Severity:** HIGH to CRITICAL — HIGH/CRITICAL order drift trips kill switch at startup and blocks `session.start()`.

## When this applies

- Log lines: `startup drift: …`, `Trading HALTED: N unreconciled HIGH/CRITICAL drift item(s)`.
- `engine.reconcile` returns non-empty `DriftItem` list.
- Position qty/avg price mismatch vs broker portal.
- Missing local order for broker working order (`reason: missing local order`) or reverse.

## Detection

| Signal | How to check |
|--------|----------------|
| Startup | Critical log + session remains not started (live). |
| Drift kinds | `kind`: `position`, `order`, `funds`; `severity`: LOW–CRITICAL (orders use HIGH for missing/mismatch). |
| Positions | Compare `/positions` API to broker; note `mark_stale` is separate from qty drift. |
| Orders | Local list vs broker orderbook keys (`order_id`). |
| Funds | `compare_funds` balance/equity mismatch above tolerance. |

```bash
curl -sS -H "X-API-Key: $TRADEX_SERVE_API_KEY" http://127.0.0.1:8000/positions
curl -sS -H "X-API-Key: $TRADEX_SERVE_API_KEY" http://127.0.0.1:8000/orders
# Operator reconcile (Python shell bound to live session — illustrative)
# drifts = session.engine.reconcile(broker_orders=book, broker_positions=pos)
```

## Halt (immediate)

1. **Automatic:** Startup HIGH/CRITICAL drift → `trip_kill_switch(reason="startup_reconciliation_drift")` and **no** `session.start()`.
2. **Running session:** Trip kill switch if drift discovered during market hours with open risk.
3. Do not “fix” by submitting compensating orders until broker truth is understood.

## Diagnose

| Drift pattern | Likely cause |
|---------------|--------------|
| Missing local order | Unknown submission succeeded at broker; crash before cache write |
| Missing broker order | Local ghost; cancel never reached venue |
| Quantity / filled_qty mismatch | Partial fill lag, manual broker change |
| Position qty diff | Fill not applied locally, or wrong instrument key (exchange collision avoided by full `instrument_id`) |
| avg_price drift (MEDIUM) | Corporate action, mark vs cost basis |
| funds HIGH | Cash not synced after fills |
| status lag (LOW) | Benign timing — confirm before ignoring |

Instrument keying uses full `instrument_id` (NSE vs BSE collision fix) — verify drift symbol string includes exchange.

## Recover

1. **Establish broker truth:** Export orderbook + positions from broker API.
2. **Adopt broker rows:** Update local cache / SQLite via supported paths (status refresh loop at startup; manual `cache.update_order` in operator tooling only with audit).
3. **UNKNOWN / idempotency:** Follow [unknown-submission.md](./unknown-submission.md) for uncertain mutations.
4. **Positions:** Adjust local ledger only after confirming fills history; prefer broker qty as target state for live.
5. **Re-run reconcile:** Drifts empty or only LOW/MEDIUM accepted by policy.
6. **Clear kill switch** and `session.start()` if boot was blocked — only after sign-off.
7. **Prevent recurrence:** Enable SQLite persistence; ensure single writer; no blind retries.

## Audit (post-incident)

1. Save drift items (structured log lines) with timestamps.
2. Match each HIGH item to broker order id or instrument key.
3. Verify kill switch tripped once (`kill_switch.tripped` metric).
4. Document capital at risk during drift window (max absolute qty diff × mark).
5. Link to related feed or DB incidents if causal.

## Related metrics & alerts

| Metric | Alert |
|--------|--------|
| `kill_switch.tripped` | reason contains `reconciliation` |
| `reconciliation_drift_total` (planned) | by severity |
| `orders.submitted` during drift window | should be zero when halted |

Target architecture requires **reconciliation drift** as a first-class metric — catalog marks planned vs implemented. See [metrics-catalog.md](../metrics-catalog.md).
