# TradeX v4 — Endpoint & Flow Trace

## REST Endpoints (prefixes per router)

### Auth (`/auth`)
- `GET /auth/session` — session info
- `POST /auth/login` — login
- `POST /auth/logout` — logout

### Health (no prefix)
- `GET /health`
- `GET /health/live`
- `GET /health/ready`

### Charts (`/api/charts`)
- `GET /api/charts/symbols`
- `GET /api/charts/history/{exchange}:{symbol}`
- `GET /api/charts/indicators`
- `POST /api/charts/indicators/compute`
- `POST /api/charts/transforms/{transform_id}`
- `POST /api/charts/profiles/{profile_id}`
- `POST /api/charts/seasonality`
- `GET /api/charts/book`
- `GET /api/charts/account`
- `GET /api/charts/strategies`
- `GET /api/charts/safety`
- `POST /api/charts/backtest`
- `POST /api/charts/scanner/run`
- `GET /api/charts/workspace`
- `GET /api/charts/workspace/{layout_id}`
- `PUT /api/charts/workspace/{layout_id}`
- `DELETE /api/charts/workspace/{layout_id}`

### Orders (no prefix, auth-gated)
- `GET /orders` — list orders
- `GET /orders/{order_id}` — get order
- `POST /orders` — place order
- `POST /orders/bracket` — place bracket order
- `PUT /orders/{order_id}` — modify order
- `DELETE /orders/{order_id}` — cancel order

### Portfolio (`/positions`, `/holdings`)
- `GET /positions`
- `GET /holdings`

### Market Data
- `GET /quotes/{exchange}:{symbol}`
- `GET /search`
- `GET /history/{instrument_id}`
- `GET /option-chain/{underlying}`
- `GET /future-chain/{underlying}`

### Account
- `GET /account`

### Extensions
- `GET /extensions`

### Metrics
- `GET /metrics` (Prometheus format)

### WebSocket
- `WS /ws/stream` — reactive bus bridge (quotes, bars, orders, replay control)

## Key Flows

### 1. Backtest Flow
```
POST /api/charts/backtest
  → BacktestEngine.run(bars)
    → SimClock advances per bar
    → Strategy.on_bar(bar, ctx) → orders
    → SimOrderGateway.simulate_fill() → fills
    → SimPortfolio.apply_fill() → positions/cash
    → equity curve recorded
  → returns BacktestResult (metrics, equity, trades)
```

### 2. Scanner Flow
```
POST /api/charts/scanner/run
  → ScreeningPipeline.run(candidates)
    → UniverseFilter → FundamentalFilter → TechnicalFilter → CustomScreener
    → RankingEngine.rank() → sorted ScreenResults
  → returns ranked results
```

### 3. Order Flow (Live)
```
POST /orders
  → _build_order_request()
  → RiskManager.check() (chain of validators)
  → ExecutionEngine.submit()
    → BrokerAdapter.submit_order() → venue
  → AuditLog.append("ORDER_SUBMITTED")
  → returns OrderResponse
```

### 4. Live Market Data Flow
```
WS /ws/stream (subscribe_bars)
  → MarketFeed → broker WebSocket
  → Quote → BarAggregator → forming bars
  → published to ReactiveBus → WS clients
```

### 5. Replay Flow
```
WS /ws/stream (replay_start)
  → SyntheticTickGenerator feeds historical bars
  → Same BarAggregator path as live
  → Strategy receives identical bar sequence
  → SimOrderGateway fills (deterministic)
```

### 6. Zero-Discrepancy Guarantee
```
Backtest: SimClock + BacktestProvider + SimGateway + SimPortfolio
Replay:   ReplayClock + ReplayProvider + SimGateway + SimPortfolio
Paper:    LiveClock + LiveProvider + PaperGateway + PaperPortfolio
Live:     LiveClock + LiveProvider + LiveGateway + LivePortfolio

All use identical Strategy.on_bar() entry point.
Same inputs → same decisions → same fills (sim envs).
```
