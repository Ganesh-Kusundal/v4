# Pending work — dependency graph (2026-09-24)

Bottleneck-first: hanging tests block every broad suite. Fix those before
feature work. Independent clusters can run in parallel once hangs are gone.

```text
                    ┌─────────────────────────────┐
                    │  B0  HANGING TESTS (P0)     │
                    │  WS receive_json forever    │
                    │  after replay_step          │
                    └─────────────┬───────────────┘
                                  │ blocks all interface CI
                                  ▼
         ┌────────────────────────┴────────────────────────┐
         │                                                 │
┌────────▼────────┐                              ┌─────────▼────────┐
│ B1 PATH DEPTHS  │                              │ B2 ASSERTIONS    │
│ (extraction)    │                              │ (may share cause) │
│ parents[N]      │                              │                  │
│ - datalake root │                              │ - audit_identity │
│ - universe CSV  │                              │ - chart_backtest │
│ - UI dist       │                              │ - chart history  │
│ - CLI __file__  │                              │ - indicators 422 │
└────────┬────────┘                              └─────────┬────────┘
         │  independent of B2                               │
         ▼                                                  ▼
┌────────────────┐                               ┌──────────────────┐
│ B3 LEDGER      │                               │ B4 FAST VERIFY   │
│ close stale    │                               │ non-WS slice     │
│ Open/IN PROG   │                               │ after B0+B1      │
└────────────────┘                               └──────────────────┘
```

## Parallelism rules

| Cluster | Depends on | Can parallelize with |
|---------|------------|----------------------|
| B0 WS hang | — | B1 (paths), B3 (docs) |
| B1 path anchors | — | B0, B3 |
| B2 assertions | ideally B1 if path-related | B0 after diagnosis |
| B3 ledger | — | everything |
| B4 verify | B0 + B1 (+ B2 if touching interface) | — |

## Known failure taxonomy (from interface suite)

1. **Hang (dev killer):** `test_ws_bars_replay` — `ws.receive_json()` waits forever because `replay_step` emits no frame/ack. Stack: Starlette TestClient → anyio portal → `selector.select`.
2. **Wrong repo root after extraction:** `parents[4]` still assumes `trading/src/tradex_trading/...` depth; packages now live at `{pkg}/src/tradex_*` → need `parents[3]`.
3. **Strategy discovery / 422:** unknown strategy returns 200 — chart backtest path likely not seeing extracted strategy registry.
4. **CLI/serve mocks:** module entry / uvicorn forward tests — often `__file__` or import path after extraction.

## Status (updated)

| Cluster | Status |
|---------|--------|
| B0 WS hang | **FIXED** — monkeypatches targeted shim modules; stream uses `tradex_market_data` |
| B1 path anchors | **FIXED** — `parents[3]` for extracted layouts |
| B2 mock targets | **FIXED** — tests patch `tradex_interfaces` / `tradex_market_data` / `tradex_runtime` |
| B3 ledger | refreshed |
| B4 verify | interface suite ~359+ passing after fixes |

### Root causes (for faster development)

1. **Hangs:** `ws.receive_json()` blocked forever because `ParquetStorage` monkeypatch hit the shim, not the module stream imports → replay_step never got fake candles → no ack. Failures took 15–120s each.
2. **Wrong repo root:** `parents[4]` assumed `trading/src/tradex_trading/...` depth after extraction.
3. **Silent mock misses:** `patch("tradex_trading.interface.fastapi_app.X")` does not affect code that imports `tradex_interfaces.fastapi_app`.
