# Runbook: Unknown order submission outcome

**Scope:** Live broker mutations where the venue may have accepted the order but the client received timeout/5xx or lost the response after crossing the submission boundary.

**Severity:** CRITICAL — blind retry can **double-submit**. Idempotency reservation may remain held until reconciliation.

## When this applies

- `OrderSubmissionUnknownError` raised from fill source / provider client after `submission_boundary_crossed`.
- HTTP 5xx or hang on `POST /orders` **after** the broker adapter marked the boundary crossed.
- Idempotency key stuck: retries return `idempotency_in_flight` or guard refuses release.
- SQLite idempotency table shows reserved key without `record_result`.
- Logs: `"Order submission failed after crossing broker boundary"`.

Distinct from:

- **Risk reject** — terminal, cid released.
- **Pre-boundary network error** — rejected receipt, cid released.
- **Feed not ready** — no venue call.

## Detection

| Signal | How to check |
|--------|----------------|
| API error | 500/`OrderSubmissionUnknownError` on submit path (operator tools should surface typed error). |
| Metrics | `orders.rejected` may **not** increment; watch `orders.idempotency_replay` vs stuck pending. |
| OMS | Local order missing or status UNKNOWN; broker orderbook may show working order. |
| Logs | Engine pipeline after fill step; broker `submit_mutation` uncertain tracker. |
| Idempotency | Same `Idempotency-Key` must not be reused with different payload (`IdempotencyKeyReuseMismatch`). |

```bash
# List local OMS view (CLI when session bound)
python -m tradex_trading.interface.cli orders
curl -sS -H "X-API-Key: $TRADEX_SERVE_API_KEY" http://127.0.0.1:8000/orders
```

## Halt (immediate)

1. **Stop submitting** for that instrument/strategy until outcome is known.
2. **Trip kill switch** for the session if automation might retry: halts new submissions and cancels open **local** working orders (verify broker-side cancels succeeded).
3. **Do not** reuse the same idempotency key for a different order shape.
4. **Do not** blindly retry the same market order without broker orderbook lookup.

## Diagnose

1. **Broker orderbook (source of truth):** Fetch via broker API/portal for the correlation window (time, symbol, side, qty).
2. **Local cache vs SQLite:** If `PersistenceConfig.path` set, inspect orders + idempotency rows; compare to broker.
3. **Boundary flag:** Confirm failure happened post-boundary (`BrokerFillSource.submission_boundary_crossed`).
4. **Partial fills:** If broker shows partial fill, local projection may lag — treat as drift, not as “no order”.
5. **Bracket orders:** Composite super-order at venue — cancel/modify must use bracket endpoint semantics.

## Recover

1. **If broker shows NO order:** Release idempotency reservation only after explicit reconcile procedure documented in store (see sqlite guard messages: `release() or reconcile manually`). Mark local intent abandoned; new submission needs **new** idempotency key.
2. **If broker shows order:** Adopt broker row into OMS (`cache.update_order` / persistence mirror); `record_result` on idempotency guard with broker order id so retries replay safely.
3. **If UNKNOWN status locally:** Use `OrderManager.apply_unknown` pattern in reconciliation flows; then normalize status from broker poll.
4. **Re-enable trading:** Clear kill switch only after book matches broker and feed is READY ([stale-feed.md](./stale-feed.md)).
5. **Startup:** Next session boot runs `_run_startup_reconciliation` — HIGH/CRITICAL drift trips kill switch and refuses `session.start()`.

## Audit (post-incident)

1. Preserve: idempotency key, request fingerprint, broker request id if logged, timestamp, operator actions.
2. Document whether double exposure occurred; if yes, open [reconciliation-drift.md](./reconciliation-drift.md).
3. Verify no duplicate fills in event bus / SQLite append log.
4. Post-mortem: timeout thresholds, need for broker status poll webhook, persistence path enabled in prod.

## Related metrics & alerts

| Metric | Suggested alert |
|--------|-----------------|
| `orders.idempotency_replay` | unusual spike (may indicate retry storm) |
| `orders.rejected` | correlate with incidents (absence of reject on unknown is a clue) |
| `kill_switch.tripped` | manual trips during unknown events |
| `reconciliation_drift` (planned) | any HIGH severity |

See [metrics-catalog.md](../metrics-catalog.md) — **unknown outcomes** are a required observability dimension in target architecture §8.
