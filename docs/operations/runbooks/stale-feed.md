# Runbook: Stale live feed

**Scope:** Live mode only (`TRADEX_MODE=live`). Paper/backtest/replay have no `FeedSupervisor` gate.

**Severity:** HIGH — stale marks can reject risk checks or, if marks are ignored, drive wrong exposure. The engine rejects new orders when `feed_supervisor.ready` is false.

## When this applies

- Quotes stop updating while the broker WebSocket appears connected.
- Chart forming bars freeze; `#feed-status` shows `degraded` or age climbing.
- `StaleFeed` events on the session bus (logged by subscribers).
- Metrics: `feed_age_seconds` above the feed’s `stale_after` threshold (default **30s**), or `feed_state` gauge **4** (`degraded`).

## Detection

| Signal | How to check |
|--------|----------------|
| Readiness | `GET /health/ready` → **503** with `feed not ready: FeedState.DEGRADED` (or non-READY state). |
| Feed facts | WebSocket `feed_status` on `/ws/stream` (`ready`, `state`, `age_seconds`, `generation`, `last_event_at`). UI mirrors `live_orders_enabled`. |
| Prometheus | `GET /metrics` — gauges `feed_age_seconds`, `feed_state`, `feed_generation`. State codes: 0=new, 1=connecting, 2=resynchronizing, 3=ready, 4=degraded, 5=halted. |
| Logs | `FeedStaleMonitor` poll path; `MarketFeed.check_stale()` publishing `StaleFeed`. |
| Order path | HTTP submit returns receipt/message `feed_not_ready` (sync path). |

```bash
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/health/ready
curl -sS http://127.0.0.1:8000/metrics | rg 'feed_(age_seconds|state|generation)'
```

## Halt (immediate)

1. **Do not arm or submit live orders.** UI should already disable controls when `feed_status.ready === false`; verify before any manual API call.
2. **Optional — trip kill switch** if strategies might still fire via automation: set `engine.kill_switch` / `TRADEX_KILL_SWITCH=true` on restart, or invoke `trip_kill_switch` from an operator shell bound to the session (cancels open orders).
3. **Leave the process running** unless the feed is `halted` and recovery keeps failing — liveness is OK; readiness is what must fail closed.

## Diagnose

1. Confirm mode is live and a supervisor exists (`feed_state` not null on WS).
2. Compare `feed_age_seconds` to `MarketFeed.stale_after` (default 30s).
3. Check whether state is **degraded** vs **resynchronizing** vs **halted**:
   - **resynchronizing** — recovery in flight; orders should stay blocked until READY.
   - **degraded** — ticks missing or disconnect without completed recovery.
   - **halted** — recovery failed; do not clear manually without root-cause fix.
4. Inspect broker-side market status (exchange halt, symbol suspended, token expiry).
5. Review integrity counters on `/metrics`: `feed_missing_bars_total`, `feed_large_jumps_total`, `feed_out_of_order_events_total` (sudden spikes often precede forced resync).

## Recover

1. **Preferred — automatic path:** Broker reconnect should call `MarketFeed.notify_reconnect()` (or equivalent), which runs `FeedRecoveryCoordinator.recover()`:
   - History fetch from last accepted closed bar through now.
   - Reseed aggregators; supervisor → `READY` only on success.
2. **Manual — session API:** If wired, `TradingSession.recover_feed()` runs the same coordinator (raises if no history provider).
3. **Client resync:** Reconnect chart WebSocket; send `feed_status` request after connect. Resubscribe bars with last closed bar seed per remediation design.
4. **If stuck in RESYNCHRONIZING:** History provider failure or missing subscription set — fix broker auth/history, then trigger reconnect recovery again.
5. **If HALTED:** Resolve underlying gap (datalake/broker hole, allowance exceeded), then restart feed or full session after audit.

Success criteria:

- `feed_state` = `ready`, `feed_ready` = true, `/health/ready` = **200**.
- `feed_age_seconds` < `stale_after` under active market.
- `feed_resync_duration_seconds` observed once; `feed_recovery_failures_total` not incrementing.

## Audit (post-incident)

1. Export `/metrics` snapshot and `/health/ready` JSON at incident start/end.
2. Correlate `feed_generation` bumps with reconnect times.
3. List orders attempted during degraded window (search logs for `feed_not_ready`).
4. Verify positions vs broker (`engine.reconcile` / broker portal) if any fill occurred near the stale window.
5. Record root cause (broker outage, token, symbol, network, missing history wiring).

## Related metrics & alerts

| Metric | Suggested alert |
|--------|-----------------|
| `feed_age_seconds` | > `stale_after` for 2 consecutive scrapes |
| `feed_state` | == 4 (degraded) or == 5 (halted) |
| `feed_recovery_failures_total` | rate > 0 |
| `feed_resync_duration_seconds` | p95 above SLO (e.g. 60s) |
| `feed_gaps_total` / `feed_missing_bars_total` | spike during incident |

See [metrics-catalog.md](../metrics-catalog.md).
