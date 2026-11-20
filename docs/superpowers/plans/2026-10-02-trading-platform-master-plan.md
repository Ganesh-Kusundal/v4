# TradeX Professional Trading Platform — Master Plan

## Goal
Build institutional-grade algorithmic trading platform with zero-discrepancy guarantee
across backtest/live/paper/replay. DDD, event-driven, SOLID, TDD.

## Architecture
- Zero-discrepancy via 3 protocol abstractions: TradingClock, MarketDataProvider, OrderGateway
- EnvironmentFactory creates LIVE/PAPER/BACKTEST/REPLAY with identical strategy surface
- Event-sourced, CQRS, repository pattern, strategy pattern

## Workstreams (parallel, independent)

### WS-A: Backtest Engine Core
- SimOrderGateway (deterministic fills: market/limit/stop, slippage, commission)
- SimPortfolio (positions, cash, P&L, risk limits)
- BacktestEngine (orchestrates clock + data + gateway + metrics)
- MetricsCalculator (Sharpe, Sortino, max DD, win rate, profit factor)

### WS-B: Screening Pipeline
- UniverseFilter (index constituents, sector, market cap)
- FundamentalFilter (P/E, ROE, debt/equity, growth)
- TechnicalFilter (RSI, MACD, MA, volume)
- CustomScreener (user expressions)
- RankingEngine (multi-factor scoring)
- ScreeningOptimizer (threshold tuning + backtest validation)

### WS-C: Risk Management
- RiskManager (position sizing, exposure limits, drawdown guards)
- PreTradeValidator (chain of responsibility)
- PostTradeMonitor (real-time P&L, alerts)

### WS-D: Zero-Discrepancy Tests
- Cross-environment determinism test harness
- Replay engine (feed historical events through live code paths)
- Performance benchmarks (<1ms/bar)

### WS-E: Strategy Framework
- Strategy base (template method)
- Signal generation + validation
- Strategy registry + parameter management
- Walk-forward optimization

## Implementation Order (TDD)
1. WS-A SimOrderGateway + tests
2. WS-A SimPortfolio + tests
3. WS-B ScreeningPipeline + tests
4. WS-C RiskManager + tests
5. WS-D Determinism tests
6. WS-E Strategy framework + optimization

## Quality Gates
- Import boundaries enforced
- Complexity max 10
- mypy strict
- 90%+ coverage
- Zero-discrepancy: identical fills across environments
EOF
echo created