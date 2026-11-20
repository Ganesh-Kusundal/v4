# TradeX Professional Algorithmic Trading Platform — Architecture

## Vision
Institutional-grade algorithmic trading platform with **zero-discrepancy** guarantee:
identical strategy decisions across backtesting, paper trading, live trading,
and replay environments. Built on DDD, event-driven architecture, SOLID principles,
and DRY methodology with comprehensive TDD.

## Bounded Contexts (DDD)

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Trading Platform Bounded Contexts                 │
├──────────────┬──────────────┬──────────────┬────────────────────────┤
│  Market Data │  Execution   │  Strategy    │  Risk Management       │
│  Context     │  Context     │  Context     │  Context               │
├──────────────┼──────────────┼──────────────┼────────────────────────┤
│  Portfolio   │  Screening   │  Backtesting │  Optimization          │
│  Context     │  Context     │  Context     │  Context               │
├──────────────┼──────────────┼──────────────┼────────────────────────┤
│  Replay      │  Analytics   │  Compliance  │  Notification          │
│  Context     │  Context     │  Context     │  Context               │
└──────────────┴──────────────┴──────────────┴────────────────────────┘
```

## Zero-Discrepancy Guarantee (Core Invariant)

The fundamental architectural invariant: **a strategy receives the identical
input sequence (bars, quotes, events) in all environments.** This is achieved by:

1. **Single Event Bus**: All environments publish to the same `DomainEvent` schema.
2. **Deterministic Replay**: Replay engine feeds historical events through the
   identical `Strategy.on_bar()` / `Strategy.on_event()` entry points.
3. **Clock Abstraction**: `TradingClock` interface — `LiveClock`, `SimClock`,
   `ReplayClock` all implement `now()`; strategies never call `datetime.now()`.
4. **Data Provider Abstraction**: `MarketDataProvider` interface — `LiveProvider`,
   `BacktestProvider`, `ReplayProvider` all return `Bar`/`Quote` objects.
5. **Execution Abstraction**: `OrderGateway` interface — `LiveGateway`,
   `PaperGateway`, `SimGateway` all accept `OrderRequest` and return `Fill`.
6. **State Isolation**: Each environment has its own `Portfolio` instance, but
   the strategy logic is environment-agnostic.

### Environment Matrix

| Environment | Clock | Data | Orders | Portfolio |
|-------------|-------|------|--------|-----------|
| Live        | LiveClock | LiveProvider | LiveGateway | LivePortfolio |
| Paper       | LiveClock | LiveProvider | PaperGateway | PaperPortfolio |
| Backtest    | SimClock | BacktestProvider | SimGateway | SimPortfolio |
| Replay      | ReplayClock | ReplayProvider | SimGateway | SimPortfolio |

## Design Patterns Applied

| Pattern | Where | Purpose |
|---------|-------|---------|
| **Event Sourcing** | All state changes | Audit trail, replay, time-travel debugging |
| **CQRS** | Read/write separation | Fast queries, consistent writes |
| **Strategy** | Trading strategies | Pluggable algorithms |
| **Factory** | Environment creation | Consistent environment setup |
| **Observer** | Event bus | Decoupled component communication |
| **Repository** | Data access | Testable, swappable persistence |
| **Adapter** | Broker integration | Unified broker interface |
| **Builder** | Complex object construction | Readable test setup |
| **Decorator** | Risk checks, logging | Cross-cutting concerns |
| **Chain of Responsibility** | Order validation | Composable validators |
| **State Machine** | Order lifecycle | Explicit state transitions |
| **Template Method** | Strategy base | Consistent strategy lifecycle |
| **Singleton** | Event bus, config | Shared infrastructure |

## Domain Model (Core Entities)

### Value Objects (Immutable)
- `Price`, `Quantity`, `Money`, `Percentage`
- `InstrumentId`, `OrderId`, `TradeId`, `StrategyId`
- `Timeframe`, `Bar`, `Quote`, `Depth`
- `Signal`, `SignalStrength`

### Entities (Mutable, Identity-based)
- `Order` (state machine: Pending→Submitted→Filled/Cancelled/Rejected)
- `Position` (tracks entry, P&L, risk)
- `Portfolio` (aggregates positions, cash, P&L)
- `Strategy` (generates signals from market data)
- `Trade` (completed round-trip)

### Aggregates
- `OrderBook` (collection of orders with invariants)
- `Portfolio` (positions + cash + risk limits)
- `StrategyInstance` (strategy + params + state)

### Domain Events
- `BarReceived`, `QuoteReceived`
- `SignalGenerated`
- `OrderSubmitted`, `OrderFilled`, `OrderCancelled`, `OrderRejected`
- `PositionOpened`, `PositionClosed`, `PositionUpdated`
- `RiskLimitBreached`
- `StrategyStarted`, `StrategyStopped`

## TDD Strategy

### Test Pyramid
```
         ┌─────────────┐
         │  E2E (5%)   │  Full environment integration
         ├─────────────┤
         │ Integration │  Component interaction
         │   (25%)     │
         ├─────────────┤
         │   Unit      │  Domain logic, pure functions
         │   (70%)     │
         └─────────────┘
```

### Test Categories
1. **Domain Tests**: Pure logic, no I/O, fast
2. **Contract Tests**: Protocol conformance
3. **Integration Tests**: Component interaction
4. **Property-Based Tests**: Invariants (hypothesis)
5. **Zero-Discrepancy Tests**: Cross-environment determinism
6. **Performance Tests**: Latency, throughput benchmarks

## Backtesting Architecture

### Components
- `BacktestEngine`: Orchestrates simulation
- `SimClock`: Advances time deterministically
- `BacktestDataProvider`: Serves historical bars
- `SimOrderGateway`: Simulates fills with slippage/commission
- `SimPortfolio`: Tracks simulated positions
- `MetricsCalculator`: Sharpe, Sortino, max drawdown, win rate, etc.

### Fill Simulation
- Market orders: fill at next bar open + slippage
- Limit orders: fill when price crosses limit
- Stop orders: trigger on price touch, fill with slippage
- Commission model: configurable (flat, percentage, tiered)
- Slippage model: configurable (fixed bps, volume-based)

### Optimization
- Grid search, random search, Bayesian optimization
- Walk-forward analysis (in-sample/out-of-sample)
- Parameter sensitivity analysis
- Monte Carlo simulation

## Advanced Stock Screening

### Screening Pipeline
1. **Universe Filter**: Index constituents, sector, market cap
2. **Fundamental Filter**: P/E, ROE, debt/equity, revenue growth
3. **Technical Filter**: RSI, MACD, moving averages, volume
4. **Custom Screener**: User-defined expressions
5. **Ranking**: Multi-factor scoring
6. **Optimization**: Find optimal screening parameters

### Screening Optimization
- Parameter tuning for screening thresholds
- Backtest-driven validation of screen results
- Performance attribution of screening factors

## Implementation Phases

### Phase 0: Foundation (Weeks 1-2)
- Event bus (pub/sub with ordering guarantees)
- Domain event schema
- Clock abstraction
- Value objects

### Phase 1: Market Data (Weeks 3-4)
- Bar/Quote value objects
- Market data provider interface
- Historical data loader
- Real-time data feed adapter

### Phase 2: Execution (Weeks 5-6)
- Order state machine
- Order gateway interface
- Fill simulation
- Portfolio management

### Phase 3: Strategy (Weeks 7-8)
- Strategy base class (template method)
- Signal generation
- Strategy registry
- Parameter management

### Phase 4: Backtesting (Weeks 9-10)
- Backtest engine
- Metrics calculation
- Report generation

### Phase 5: Screening (Weeks 11-12)
- Screening pipeline
- Factor library
- Screening optimization

### Phase 6: Zero-Discrepancy Testing (Weeks 13-14)
- Cross-environment determinism tests
- Replay engine
- Performance benchmarks

### Phase 7: Optimization (Weeks 15-16)
- Parameter optimization
- Walk-forward analysis
- Monte Carlo simulation

## Technology Stack

- **Language**: Python 3.12+ (type hints, dataclasses)
- **Event Bus**: In-process pub/sub (asyncio) + Redis for distributed
- **Persistence**: Parquet (market data), SQLite (events/orders), DuckDB (analytics)
- **API**: FastAPI (REST + WebSocket)
- **Testing**: pytest, hypothesis (property-based), coverage
- **Optimization**: scipy, optuna (Bayesian)
- **Metrics**: numpy, pandas, scipy

## Quality Gates

1. **Import boundaries**: import-linter contracts
2. **Complexity**: max cyclomatic complexity 10
3. **Type safety**: mypy strict mode
4. **Coverage**: 90%+ line coverage
5. **Zero-discrepancy**: Cross-environment determinism tests
6. **Performance**: <1ms per bar processing in backtest
EOF
echo "Design doc created"