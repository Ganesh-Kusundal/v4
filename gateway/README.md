# `tradex_gateway` — dual-broker market-data gateway

`tradex_gateway` is TradeX v4's data-only service: one process that owns both
live brokers (Dhan + Upstox) and exposes a single stable HTTP/WebSocket
contract for historical bars, quotes, market depth, option chains and live
feeds. It never places orders — brokers are built with
`allow_order_operations=False`.

## What it is

- Workspace member `gateway/` (package `tradex-gateway`), installed as
  `tradex_gateway`.
- Allowed imports: `tradex_domain`, `tradex_brokers`, `tradex_config`,
  `tradex_market_data`, `tradex_runtime`, `tradex_observability`. It
  deliberately does **not** depend on `tradex_interfaces`, `tradex_execution`,
  `tradex_strategy`, `tradex_replay`, `tradex_application` or `duck_analytics`.
- Single process only: startup refuses `workers > 1` because two processes
  would hold independent broker limiters and multiply the vendor quota.
- Read-only guarantee: no order, portfolio or execution code path is reachable;
  only market data is served.

## `/v1` policy

All endpoints live under the `/v1` base path (e.g. `/v1/health`,
`/v1/history/{instrument_id}`). Auth is an API key: `X-API-Key` header for REST,
`?api_key=` query parameter for WebSocket. The interfaces app stays unversioned
at its root mounts; the gateway owns the versioned `/v1` contract.

## Launch

```bash
python -m tradex_gateway            # serves on GATEWAY_HOST:GATEWAY_PORT
tradex-gateway serve --host 0.0.0.0 --port 8100
tradex-gateway check               # build brokers and print status
```

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `GATEWAY_HOST` | `127.0.0.1` | bind address |
| `GATEWAY_PORT` | `8100` | port |
| `GATEWAY_API_KEY` | unset | required unless anon is allowed on loopback |
| `GATEWAY_ALLOW_ANON` | `false` | allow unauthenticated loopback clients |
| `GATEWAY_BROKERS` | `dhan,upstox` | enabled brokers |
| `GATEWAY_PRIMARY_LIVE` | `dhan` | live primary |
| `GATEWAY_PRIMARY_QUOTES` | `upstox` | REST quote primary |
| `GATEWAY_PRIMARY_HISTORY` | `upstox` | history primary |
| `GATEWAY_PRIMARY_CHAIN` | `dhan` | option-chain primary |
| `GATEWAY_REST_RATE_PER_SECOND` | `20` | consumer REST rate |
| `GATEWAY_REST_BURST` | `40` | consumer REST burst |
| `GATEWAY_MAX_SUBSCRIPTIONS` | `1000` | WS instrument cap per client |
| `GATEWAY_STALE_AFTER_SECONDS` | `15` | live feed staleness threshold |
| `GATEWAY_MASTER_CACHE_DIR` | runtime default | master cache directory |

Broker credentials come from the standard TradeX env (`.env.local`), never from
this service's config. No broker creds are baked into the image.

## Design

See `docs/superpowers/specs/2026-11-06-broker-data-gateway-design.md`.
