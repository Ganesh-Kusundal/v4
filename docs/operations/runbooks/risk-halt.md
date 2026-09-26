# Runbook: Risk halt & kill switch

**Scope:** `RiskManager` denials, daily loss/drawdown budgets, fresh-mark gates, rate limits, and the session **kill switch** (`KillSwitch`).

**Severity:** HIGH (risk reject on one order) to CRITICAL (kill switch / startup reconciliation trip).

## When this applies

- Orders return `risk_check_failed` or HTTP 422/4xx with risk reason.
- Kill switch active: submissions return `kill_switch_active`.
- Metrics: `risk.rejected`, `kill_switch.tripped` increment.
- Startup: log `Refusing to start session: kill switch tripped during startup reconciliation`.
- Config: `TRADEX_KILL_SWITCH=true` / `kill_switch_default=True` (intentional arm — session may still reach READY but rejects orders).

## Detection

| Signal | How to check |
|--------|----------------|
| Order receipt | `message`: `kill_switch_active`, `risk_check_failed`, or risk reason string from `RiskCheckResult`. |
| Metrics | `risk.rejected`, `orders.rejected`, `kill_switch.tripped`. |
| Risk flags | `require_fresh_marks`, `max_mark_age_seconds`, `reject_unknown_market_value`, daily loss / drawdown env vars. |
| Feed coupling | Risk may reject when marks stale even if UI allows arm — check `feed_age_seconds` and position `mark_stale` on `/positions`. |
| Session state | `GET /health/ready` — session `READY` but orders still blocked when kill switch set by design. |

```bash
curl -sS http://127.0.0.1:8000/metrics | rg 'risk\.|kill_switch|orders\.rejected'
curl -sS -H "X-API-Key: $TRADEX_SERVE_API_KEY" http://127.0.0.1:8000/positions
```

## Halt (immediate)

**Kill switch trip (automatic or manual):**

1. New submissions halted; pipeline filter drops async commands.
2. `trip_kill_switch` cancels non-terminal open orders via engine `cancel` (track failures returned from trip).
3. `RiskManager.live_orders_enabled` set false on trip.

**Risk rejection (single order):**

1. No session-wide halt unless policy requires — treat as expected guardrail.
2. If repeated denials from a runaway strategy, pause strategy engine or trip kill switch.

## Diagnose

| Cause | Indicators |
|-------|------------|
| Kill switch (config) | Tripped at boot with `startup_reconciliation_drift` or operator trip |
| Max order / position value | Single large order; `max_order_value` / `max_position_value` |
| Rate limit | `max_orders_per_minute` deque full |
| Daily loss / drawdown | Session PnL vs `max_daily_loss_amt` / `max_drawdown_pct` |
| Fresh marks | `require_fresh_marks` + old quote; [stale-feed.md](./stale-feed.md) |
| Unknown notional | `reject_unknown_market_value` + missing quote for MARKET |
| Cash gate | BUY exceeds `cash_provider` balance |
| Per-strategy budget | `RiskBudget` exhaustion for `strategy_id` |

## Recover

1. **Intentional kill switch (config):** Fix underlying issue; clear switch only with operator approval:
   - `engine.kill_switch = False` and re-enable `risk.live_orders_enabled = True` in controlled shell, **or** restart with `TRADEX_KILL_SWITCH=false` after audit.
2. **Reconciliation trip:** Complete [reconciliation-drift.md](./reconciliation-drift.md) before clearing — session may have refused `start()`.
3. **Risk limit breach:** Adjust limits via config/env only after governance approval; restart session if needed.
4. **Stale marks:** Recover feed to READY before re-arming live orders.
5. **Failed cancels on trip:** Manually cancel remaining orders at broker; reconcile book.

Re-arm checklist:

- Feed READY, reconciliation clean, kill switch clear, strategies paused until verified.

## Audit (post-incident)

1. Count `risk.rejected` vs `orders.submitted` during window.
2. List kill switch reason string from logs (`Kill switch tripped: …`).
3. Export open orders at trip time vs broker final state.
4. Verify cancel failures from `trip_kill_switch` return list.
5. Document config changes (env vars in `tradex_trading.config.env`).

## Related metrics & alerts

| Metric | Suggested alert |
|--------|-----------------|
| `kill_switch.tripped` | any increment |
| `risk.rejected` | rate spike |
| `orders.rejected` | correlated spike |
| `feed_age_seconds` | high when `require_fresh_marks` enabled |

See [metrics-catalog.md](../metrics-catalog.md).
