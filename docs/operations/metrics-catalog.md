# TradeX v4 — Metrics & alerts catalog

**Exposition:** `GET /metrics` (Prometheus text format from `MetricsRegistry.render_prometheus()`).

**Health:**

| Endpoint | Meaning |
|----------|---------|
| `GET /health` | Process up |
| `GET /health/live` | Liveness |
| `GET /health/ready` | Session + feed readiness (503 when not ready) |
| WS `/ws/stream` `feed_status` | Same readiness facts as UI gate (`live_orders_enabled`) |

**Checklist source:** Target architecture operations requirements (`docs/target-architecture-design.md` §8) and Phase 10 observability list (`docs/deep-refactor-plan.md` — feed age, gaps, drops, resync duration, order latency, rejection reasons, unknown outcomes, slippage, exposure, drawdown, reconciliation drift).

Legend: **Implemented** = emitted in code today; **Planned** = required by checklist, not yet wired or partial.

---

## Feed health & recovery

| Metric | Type | Status | Description | Suggested alert | Runbook |
|--------|------|--------|-------------|-----------------|---------|
| `feed_age_seconds` | gauge | Implemented | Seconds since last quote/event on supervisor | > `stale_after` (default 30s) for 2 scrapes | [stale-feed.md](./runbooks/stale-feed.md) |
| `feed_state` | gauge | Implemented | Encoded supervisor state: 0=new, 1=connecting, 2=resynchronizing, 3=ready, 4=degraded, 5=halted | ≠ 3 during market session | [stale-feed.md](./runbooks/stale-feed.md), [broker-disconnect.md](./runbooks/broker-disconnect.md) |
| `feed_generation` | gauge | Implemented | Reconnect generation counter | Changes without return to 3 within SLA | [broker-disconnect.md](./runbooks/broker-disconnect.md) |
| `feed_resync_duration_seconds` | histogram | Implemented | Wall time for one `FeedRecoveryCoordinator.recover()` | p95 > 60s | [stale-feed.md](./runbooks/stale-feed.md) |
| `feed_recovery_failures_total` | counter | Implemented | Recovery returned not-ok / halted | rate > 0 | [stale-feed.md](./runbooks/stale-feed.md) |
| `feed_gaps_total` | counter | Implemented | Aggregate integrity + recovery gap signals | spike | [stale-feed.md](./runbooks/stale-feed.md) |
| `feed_duplicate_events_total` | counter | Implemented | Duplicate tick keys | sustained rate | [stale-feed.md](./runbooks/stale-feed.md) |
| `feed_out_of_order_events_total` | counter | Implemented | Out-of-order events | sustained rate | [stale-feed.md](./runbooks/stale-feed.md) |
| `feed_large_jumps_total` | counter | Implemented | Large time jumps on tape | any during session | [stale-feed.md](./runbooks/stale-feed.md) |
| `feed_missing_bars_total` | counter | Implemented | Missing closed bars in integrity/recovery | > 0 on live | [stale-feed.md](./runbooks/stale-feed.md) |
| `feed_duplicate_closed_bars_total` | counter | Implemented | Duplicate closed bar stamps | low-rate warn | [stale-feed.md](./runbooks/stale-feed.md) |
| `feed_session_boundary_violations_total` | counter | Implemented | Events outside cash session bounds | off-hours noise vs leak | [stale-feed.md](./runbooks/stale-feed.md) |
| `feed_queue_drops_total` | counter | Implemented | WS outbound queue drops (`stream.py` `_track_drop`) | rate > 0 | [broker-disconnect.md](./runbooks/broker-disconnect.md) |

---

## Order execution & OMS

| Metric | Type | Status | Description | Suggested alert | Runbook |
|--------|------|--------|-------------|-----------------|---------|
| `orders.submit_latency_seconds` | histogram | Implemented | End-to-end sync submit | p99 > SLO | [unknown-submission.md](./runbooks/unknown-submission.md) |
| `orders.process_latency_seconds` | histogram | Implemented | Pipeline processing segment | p99 > SLO | [unknown-submission.md](./runbooks/unknown-submission.md) |
| `orders.submitted` | counter | Implemented | Orders reaching submitted state | anomaly vs baseline | [oms-recovery.md](./runbooks/oms-recovery.md) |
| `orders.filled` | counter | Implemented | Filled orders | — | [oms-recovery.md](./runbooks/oms-recovery.md) |
| `orders.rejected` | counter | Implemented | Terminal rejects (risk, venue, feed) | spike | [risk-halt.md](./runbooks/risk-halt.md) |
| `orders.idempotency_replay` | counter | Implemented | Duplicate cid replays | spike after incident | [unknown-submission.md](./runbooks/unknown-submission.md) |
| `kill_switch.tripped` | counter | Implemented | Kill switch activations | any increment | [risk-halt.md](./runbooks/risk-halt.md) |
| `orders.unknown_outcome_total` | counter | Implemented | Explicit unknown submission outcomes (boundary-crossed fill errors) | > 0 | [unknown-submission.md](./runbooks/unknown-submission.md) |
| `orders.rejection_reason.<code>` | counter | Implemented | Per-gate reason counters (`live_orders_disabled`, `order_value_exceeded`, `kill_switch_active`, `feed_not_ready`, `daily_loss_exceeded`, `drawdown_exceeded`, `marks_not_fresh`, …) | by reason spike | [risk-halt.md](./runbooks/risk-halt.md) |
| `reconciliation_drift_total.<severity>.<kind>` | counter | Implemented | Drift items keyed by severity + kind (e.g. `low.position`, `high.order`) | HIGH/CRITICAL > 0 | [reconciliation-drift.md](./runbooks/reconciliation-drift.md) |

---

## Risk & exposure

| Metric | Type | Status | Description | Suggested alert | Runbook |
|--------|------|--------|-------------|-----------------|---------|
| `risk.rejected` | counter | Implemented | Risk gate denials | rate spike | [risk-halt.md](./runbooks/risk-halt.md) |
| `exposure_notional` | gauge | Implemented | Absolute position notional (qty × mark/avg) after each risk check | policy limit | [risk-halt.md](./runbooks/risk-halt.md) |
| `drawdown_pct` | gauge | Implemented | Session drawdown vs peak (fraction) after each risk check | > `max_drawdown_pct` | [risk-halt.md](./runbooks/risk-halt.md) |
| `daily_loss` | gauge | Implemented | Session daily loss (positive when net PnL below day baseline) | > `max_daily_loss_amt` | [risk-halt.md](./runbooks/risk-halt.md) |
| `mark_age_seconds` | gauge | Implemented | Age of last applied mark on quote-driven MTM | > `max_mark_age_seconds` when `require_fresh_marks` | [stale-feed.md](./runbooks/stale-feed.md) |

---

## Execution quality

| Metric | Type | Status | Description | Suggested alert | Runbook |
|--------|------|--------|-------------|-----------------|---------|
| `slippage_bps` | histogram | Planned | Fill vs reference | tail breach | — |
| `fill_reference` | info | Implemented | Config: `next_open` / `signal_close` | — | — |

---

## Event bus & engine

| Metric | Type | Status | Description | Suggested alert | Runbook |
|--------|------|--------|-------------|-----------------|---------|
| `bus.messages.published` | counter | Implemented | Bus publish count | — | — |
| `bus.messages.dropped` | counter | Implemented | Backpressure drops | rate > 0 | [database-failure.md](./runbooks/database-failure.md) |
| `bus.drain.exceeded` | counter | Implemented | Drain budget exceeded | rate > 0 | — |
| `bus.messages.subscriber_errors` | counter | Implemented | Handler exceptions | rate > 0 | [database-failure.md](./runbooks/database-failure.md) |
| `bus.applied_fills.evicted` | counter | Implemented | Fill dedup evictions | — | — |
| `engine.errors.total` | counter | Implemented | Engine error hook | rate > 0 | [database-failure.md](./runbooks/database-failure.md) |

---

## Alert routing (recommended)

| Priority | Condition | Action |
|----------|-----------|--------|
| P1 | `kill_switch.tripped` or `feed_state` == 5 | Page on-call; follow [risk-halt.md](./runbooks/risk-halt.md) |
| P1 | `orders.unknown_outcome_total` or confirmed `OrderSubmissionUnknownError` | Page; [unknown-submission.md](./runbooks/unknown-submission.md) |
| P2 | `feed_state` == 4 or `feed_age_seconds` high | Ticket; [stale-feed.md](./runbooks/stale-feed.md) |
| P2 | `feed_recovery_failures_total` | Ticket; feed recovery |
| P2 | `reconciliation_drift_total.high.*` / `reconciliation_drift_total.critical.*` | Halt trading; [reconciliation-drift.md](./runbooks/reconciliation-drift.md) |
| P3 | `risk.rejected` / `orders.rejected` elevated | Review limits/strategy |
| P3 | `bus.messages.dropped` | Scale or reduce fan-out |

---

## Operator scrape example

```bash
export TRADEX_BASE=http://127.0.0.1:8000
curl -sS "$TRADEX_BASE/metrics" | rg '^feed_|^orders\.|^kill_switch|^risk\.'
curl -sS -o /dev/null -w "ready_http=%{http_code}\n" "$TRADEX_BASE/health/ready"
```

For live API mutations, set `TRADEX_SERVE_API_KEY` and send `X-API-Key` (development loopback boundary only per remediation design).
