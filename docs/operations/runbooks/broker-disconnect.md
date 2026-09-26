# Runbook: Broker disconnect (market data & API)

**Scope:** Live sessions using Dhan/Upstox adapters. Socket healing is owned by broker backends (`AutoReconnectMixin`); **readiness** is owned by `FeedSupervisor` + `FeedRecoveryCoordinator`.

**Severity:** HIGH for live trading; MEDIUM for read-only chart if history/datalake still serves bars.

## When this applies

- WebSocket quote stream drops; depth/quotes silent.
- REST order or history calls fail with auth/network errors.
- `feed_state` transitions: `ready` → `degraded` or `connecting`; `feed_generation` increments.
- Broker logs: reconnect/backoff, token refresh, subscription replay.

## Detection

| Signal | How to check |
|--------|----------------|
| Feed supervisor | WS `feed_status.state` in (`degraded`, `connecting`, `resynchronizing`, `halted`). |
| Readiness | `GET /health/ready` → 503 when feed not ready or session not `READY`. |
| Metrics | `feed_generation` increase; `feed_state` ≠ 3; optional `engine.errors.total` on bus handlers. |
| Broker | CLI `watch` / `quote` against same credentials; vendor status page. |
| Orders | Submissions fail with venue errors; **not** the same as unknown submission (see [unknown-submission.md](./unknown-submission.md)). |

```bash
curl -sS http://127.0.0.1:8000/health/ready | jq .
curl -sS http://127.0.0.1:8000/metrics | rg 'feed_|kill_switch|orders\.'
```

## Halt (immediate)

1. Treat disconnect as **degraded feed**: live orders disabled via `feed_not_ready` and UI `live_orders_enabled: false`.
2. If disconnect happened **during** an order mutation window, switch to [unknown-submission.md](./unknown-submission.md) protocol — do not retry blindly.
3. Trip kill switch if automated strategies remain subscribed and might submit on stale marks: `trip_kill_switch(reason="broker_disconnect")`.
4. Do **not** restart live on a non-loopback bind until production auth design is in place (interim policy: loopback only).

## Diagnose

1. **Data plane vs control plane:** Quotes dead but REST orderbook works → likely WS/token/subscription issue. Both dead → credentials or broker outage.
2. **Generation:** Each reconnect bumps `feed_generation`; recovery must complete for that generation before READY.
3. **Auth:** Dhan DH-901/DH-906-style invalid token — safe reads may refresh; mutations stay one-shot.
4. **Instrument cap:** `MarketFeed` enforces `max_stream_instruments`; oversubscribe raises at subscribe time (not silent drop).
5. **Session vs socket:** `session.start()` does **not** implicitly recover feed; recovery runs on feed start/reconnect hooks.

## Recover

1. Allow broker auto-reconnect to reopen socket and replay subscriptions.
2. Ensure reconnect hook invokes **feed recovery** (history resync → `recovery_succeeded`), not socket-alone READY.
3. Operator trigger: `session.recover_feed()` when coordinator is bound.
4. Client: reconnect `/ws/stream`, request `feed_status`, resubscribe `subscribe_bars` with seed snapshot when available.
5. If recovery fails → `halted`; fix history source or gap, then retry recovery or restart session:
   ```bash
   # Example local serve (loopback, paper for drill)
   python -m tradex_trading.interface.cli serve --broker paper --port 8000
   ```
6. After READY, verify quotes on watched symbols and `feed_age_seconds` decay.

## Audit (post-incident)

1. Timeline: disconnect time, `feed_generation` values, recovery duration (`feed_resync_duration_seconds`).
2. Orders: list submissions between disconnect and halt; reconcile any UNKNOWN or in-flight idempotency keys in SQLite store.
3. Positions: `engine.reconcile(broker_orders=..., broker_positions=...)` or broker portal export.
4. Token/credential rotation if auth was root cause; no secrets in tickets — reference vault/key id only.

## Related metrics & alerts

| Metric | Suggested alert |
|--------|-----------------|
| `feed_state` | not READY during market hours |
| `feed_generation` | change without subsequent ready state within N minutes |
| `feed_recovery_failures_total` | increment |
| `kill_switch.tripped` | increment during disconnect window |

See [metrics-catalog.md](../metrics-catalog.md).
