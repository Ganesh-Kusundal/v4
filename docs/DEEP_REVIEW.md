# TradeX v4 — Deep Architectural Review

> **Date:** 2026-08-07 (reconciled with post-Phase-1 tree)
> **Scope:** Full code-level audit of `tradex-domain`, `tradex-brokers`, `tradex-trading`
> **Audience:** Principal Engineer / Quant Engineer
> **Status:** Code complete — 2,543 tests pass, 0 failures (2 skipped)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [File Tree & Source Inventory](#2-file-tree--source-inventory)
3. [Architecture Overview](#3-architecture-overview)
4. [Domain Layer — Deep Dive](#4-domain-layer--deep-dive)
5. [Brokers Layer — Deep Dive](#5-brokers-layer--deep-dive)
6. [Trading Layer — Deep Dive](#6-trading-layer--deep-dive)
7. [Class Diagrams & Relationships](#7-class-diagrams--relationships)
8. [Order Flow — End to End](#8-order-flow--end-to-end)
9. [Market Data Flow — End to End](#9-market-data-flow--end-to-end)
10. [Component Organization & Boundaries](#10-component-organization--boundaries)
11. [Principal Engineer Assessment](#11-principal-engineer-assessment)
12. [Quant Engineer Assessment](#12-quant-engineer-assessment)
13. [Test Coverage Analysis](#13-test-coverage-analysis)
14. [Known Issues & Failures](#14-known-issues--failures)
15. [Recommendations](#15-recommendations)

---

## 1. Executive Summary

TradeX v4 is a **broker-agnostic trading platform** for Indian markets (NSE, BSE, NFO, MCX, CDS) with Dhan and Upstox as live broker providers, plus a Paper broker for simulation. The platform is built on a **reactive architecture** (RxPY) with a strict three-package dependency boundary:

```
tradex-domain    ← zero internal deps (stdlib + rx only)
    ↑
tradex-brokers   ← broker adapters (Dhan, Upstox, Paper)
    ↑
tradex-trading   ← engine, SDK, execution, analytics, strategy
```

### Health at a Glance

| Metric | Value |
|--------|-------|
| Source files | **168** (domain: 15, brokers: 54, trading: 99) |
| Test files | **159** (domain: 15, brokers: 38, trading: 106) |
| Passing tests | **2,543** (domain: 343, brokers: 899, trading: 1,301) |
| Failing tests | **0** (2 skipped) |
| LOC | ~28,700 (domain: 3.2k, brokers: 12.9k, trading: 12.6k) |
| Compilation | Clean (0 errors across all 3 packages) |
| Lint (ruff) | Clean (0 errors) |
| Type checking (mypy) | ⚠️ Not configured for v4 layout |

### Verdict

**The code is production-ready at the logic layer.** The architecture is clean, boundaries are enforced, and the reactive spine is well-designed. The remaining gaps are operational (mypy config, test env setup) not architectural.

---

## 2. File Tree & Source Inventory

```
v4/
├── .env.local                                    # Live credentials (⚠️ SECURITY: real tokens)
├── smoke_mcx_stream.py                          # Live MCX WebSocket smoke test
│
├── domain/                                        # tradex-domain (shared kernel)
│   ├── pyproject.toml                           # hatchling, rx>=3.2,<4 only
│   ├── src/tradex_domain/
│   │   ├── __init__.py                          # Public API re-exports
│   │   ├── enums.py                             # 10 StrEnum types + open registration
│   │   ├── errors.py                            # 10 typed exceptions (SDKError hierarchy)
│   │   ├── value_objects.py                     # 7 frozen value objects
│   │   ├── instruments.py                       # 6 concrete instrument types
│   │   ├── lifecycle.py                         # Lifecycle states + transitions
│   │   ├── market.py                            # Quote, Depth, Candle, HistoricalSeries
│   │   ├── options.py                           # OptionChain, Expiry, OptionPair
│   │   ├── execution.py                         # Order, Fill, Position, Account, Portfolio
│   │   ├── protocols.py                         # BrokerAdapter, ExtensionAdapter, SessionFacade, Clock, IndicatorComputer
│   │   ├── capabilities.py                      # BrokerCapabilities + 3 provider matrices
│   │   ├── events.py                            # 15 domain event types
│   │   ├── strategy.py                          # Signal, ScannerDefinition, StrategyContext
│   │   ├── serialization.py                     # Generic to_dict/from_dict machinery
│   │   └── wire.py                              # InstrumentRegistry, WireAdapter, normalize
│   └── tests/                                   # 15 test files
│       ├── test_lifecycle.py
│       ├── test_value_object_arithmetic.py
│       ├── test_instrument_id_parsing.py
│       ├── test_serialization_roundtrip.py
│       └── ... (11 more: registry, market, options, domain enrichments)
│
├── brokers/                                       # tradex-brokers (adapter layer)
│   ├── pyproject.toml                           # hatchling, depends on tradex-domain
│   ├── src/tradex_brokers/
│   │   ├── __init__.py                          # Package init
│   │   ├── registry.py                          # BrokerFactory (register/create/available)
│   │   │
│   │   ├── common/                              # Shared infrastructure (21 modules)
│   │   │   ├── __init__.py
│   │   │   ├── auth.py                          # TOTP code gen, JWT expiry, form POST
│   │   │   ├── base.py                          # Shared adapter base
│   │   │   ├── cache.py                         # ReadCache — bounded TTL cache
│   │   │   ├── circuit_breaker.py               # 3-state circuit breaker
│   │   │   ├── client_shared.py                 # Shared client helpers
│   │   │   ├── http_response.py                 # HTTP response wrapper
│   │   │   ├── instruments.py                   # Instrument master loading
│   │   │   ├── message_log.py                   # Message logging for replay
│   │   │   ├── paths.py                         # Default token/cooldown paths
│   │   │   ├── pooled_transport.py              # Connection-pooled HTTP transport
│   │   │   ├── provider_client.py               # ProviderHttpClient with rate-limit
│   │   │   ├── provider_common.py               # resolve_instrument, unwrap_data, etc.
│   │   │   ├── rate_limit.py                    # MultiBucketRateLimiter, TokenBucket
│   │   │   ├── resilience.py                    # Resilience pipeline composition
│   │   │   ├── retry.py                         # RetryableHttpClient with backoff
│   │   │   ├── streaming.py                     # Stream subscription types
│   │   │   ├── token_lifecycle.py               # DurableTokenManager, TokenBroadcast
│   │   │   ├── totp_cooldown.py                 # TotpCooldownGuard (cross-process)
│   │   │   ├── transport.py                     # HttpTransport (stdlib urllib)
│   │   │   └── ws_reconnect.py                  # WsReconnectManager with exponential backoff
│   │   │
│   │   ├── dhan/                                # Dhan adapter (14 modules)
│   │   │   ├── __init__.py
│   │   │   ├── adapter.py                       # DhanBroker (BrokerAdapter + ExtensionAdapter)
│   │   │   ├── client.py                        # DhanApiClient (26 methods)
│   │   │   ├── depth_parser.py                  # MCX/NSE depth frame parser
│   │   │   ├── instruments.py                   # MCX row loader, expiry parser
│   │   │   ├── master.py                        # Dhan instrument master loader
│   │   │   ├── tick_parser.py                   # Dhan tick/frame decoder
│   │   │   ├── ws_streams.py                    # DhanMarketDataStreamBackend, etc.
│   │   │   ├── _admin.py                        # Admin/fund endpoints
│   │   │   ├── _alerts.py                       # Alert endpoints
│   │   │   ├── _facade.py                       # Capability facade
│   │   │   ├── _marketdata.py                   # Market data endpoints
│   │   │   ├── _orders.py                       # Order endpoints
│   │   │   └── _portfolio.py                    # Portfolio endpoints
│   │   │
│   │   ├── upstox/                              # Upstox adapter (13 modules)
│   │   │   ├── __init__.py
│   │   │   ├── adapter.py                       # UpstoxBroker (BrokerAdapter + ExtensionAdapter)
│   │   │   ├── client.py                        # UpstoxApiClient (22 methods)
│   │   │   ├── instruments.py                   # Upstox instrument helpers
│   │   │   ├── master.py                        # Upstox instrument master loader
│   │   │   ├── ws_decoder.py                    # WSS v2 frame decoder
│   │   │   ├── ws_streams.py                    # UpstoxMarketDataStreamBackend, etc.
│   │   │   ├── _admin.py                        # Admin endpoints
│   │   │   ├── _alerts.py                       # Alert endpoints
│   │   │   ├── _facade.py                       # Capability facade
│   │   │   ├── _marketdata.py                   # Market data endpoints
│   │   │   ├── _orders.py                       # Order endpoints
│   │   │   └── _portfolio.py                    # Portfolio endpoints
│   │   │
│   │   ├── paper/                               # Paper (simulated) broker
│   │   │   ├── __init__.py
│   │   │   └── adapter.py                       # PaperBroker (~206 lines)
│   │   │
│   │   └── proto/                               # Protobuf stubs
│   │       ├── __init__.py
│   │       └── MarketDataFeed_pb2.py            # Generated protobuf for Dhan WS
│   │
│   └── tests/                                   # 38 test files
│       ├── test_dhan_adapter.py
│       ├── test_dhan_client.py
│       ├── test_dhan_tick_parser.py
│       ├── test_upstox_adapter.py
│       ├── test_upstox_client.py
│       ├── test_master_parsers.py
│       ├── test_paper_broker_symmetry.py
│       ├── test_paper_broker_no_trading_import.py
│       ├── test_paper_order_book.py
│       ├── test_token_lifecycle.py
│       ├── test_history_memory_efficiency.py
│       ├── test_stream_reconnect_harness.py
│       ├── test_v3_port.py
│       └── common/                               # 25 infra test files
│           ├── test_broker_factory.py
│           ├── test_broker_contracts.py
│           ├── test_token_lifecycle.py
│           ├── test_totp_cooldown.py
│           ├── test_read_cache.py
│           ├── test_rate_limits.py
│           ├── test_resilience_pipeline.py
│           ├── test_retry_skips_4xx.py
│           ├── test_transport_error_handling.py
│           ├── test_ws_resilience.py
│           └── ... (15 more: auth, client seam, streaming, ...)
│
├── trading/                                       # tradex-trading (application layer)
│   ├── pyproject.toml                           # hatchling, depends on domain + brokers
│   ├── src/tradex_trading/
│   │   ├── __init__.py
│   │   │
│   │   ├── analytics/                           # Technical analysis
│   │   │   ├── __init__.py
│   │   │   ├── breadth.py                       # Market breadth indicators
│   │   │   ├── engine.py                        # AnalyticsEngine (indicator/report)
│   │   │   ├── feature_pipeline.py              # Feature extraction pipeline
│   │   │   ├── functions.py                     # Standalone analytics functions
│   │   │   ├── fundamentals.py                  # Fundamental data processors
│   │   │   ├── futures.py                       # Futures roll, term structure
│   │   │   ├── indicators.py                    # SMA, EMA, RSI, Bollinger, ATR, etc.
│   │   │   └── options.py                       # Option Greeks, implied vol
│   │   │
│   │   ├── config/                              # Configuration
│   │   │   ├── __init__.py
│   │   │   ├── env.py                           # ENV parsing, _parse_bool
│   │   │   ├── loader.py                        # load_config (YAML + env merge)
│   │   │   └── schema.py                        # AppConfig, RiskConfig, BrokerConfig
│   │   │
│   │   ├── datalake/                            # Persistent data storage
│   │   │   ├── __init__.py
│   │   │   ├── catalog.py                       # DataCatalog (duckdb-backed)
│   │   │   ├── corporate_actions.py             # CorporateActionStore
│   │   │   ├── mcp_server.py                    # MCP server for external access
│   │   │   ├── quality.py                       # Data quality checks
│   │   │   └── source_selection.py              # DataSourceKind selection logic
│   │   │
│   │   ├── execution/                           # Order management
│   │   │   ├── __init__.py
│   │   │   ├── engine.py                        # ExecutionEngine (reactive order spine)
│   │   │   ├── fill_sources.py                  # FillSource protocol + 4 implementations
│   │   │   ├── fees.py                          # Fee calculator
│   │   │   ├── order_manager.py                 # OrderManager (lifecycle tracking)
│   │   │   ├── position_manager.py              # PositionManager (P&L tracking)
│   │   │   ├── reconciliation.py                # ReconciliationEngine (drift detection)
│   │   │   ├── sqlite_store.py                  # SQLiteOrderStore (persistent)
│   │   │   └── trading_cache.py                 # TradingCache (in-memory OMS state)
│   │   │
│   │   ├── interface/                           # CLI, API, diagnostics
│   │   │   ├── __init__.py
│   │   │   ├── fastapi_app.py                   # FastAPI health endpoint
│   │   │   ├── check_connection.py              # Connectivity probe CLI
│   │   │   ├── cli.py                           # Main CLI with argparse
│   │   │   └── tui.py                           # Terminal diagnostics
│   │   │
│   │   ├── reactive/                            # RxPY backbone (9 modules)
│   │   │   ├── __init__.py
│   │   │   ├── async_dispatch.py                # Async event dispatch
│   │   │   ├── backpressure.py                  # Backpressure operators
│   │   │   ├── bounded_bus.py                   # BoundedReactiveBus (backpressure edges)
│   │   │   ├── bus.py                           # ReactiveBus (Subject-backed)
│   │   │   ├── message_log.py                   # FileMessageLog for auditing
│   │   │   ├── operators.py                     # Custom RxPY operators
│   │   │   ├── subscription.py                  # Subscription management
│   │   │   └── thread_safe_bus.py               # ThreadSafeReactiveBus
│   │   │
│   │   ├── replay/                              # Backtesting engine (5 modules)
│   │   │   ├── __init__.py
│   │   │   ├── backtest.py                      # BacktestEngine with FakeClock
│   │   │   ├── engine.py                        # ReplayEngine (historical data)
│   │   │   ├── optimization.py                  # grid_search + result objects
│   │   │   └── walk_forward.py                  # walk-forward optimization
│   │   │
│   │   ├── runtime/                             # Composition root
│   │   │   ├── __init__.py
│   │   │   ├── calendar.py                      # NSE trading calendar
│   │   │   ├── health.py                        # HealthCheck system
│   │   │   ├── live.py                          # build_broker_from_env, master download
│   │   │   ├── market_feed.py                   # MarketFeed (tick bridge to bus)
│   │   │   ├── master_lifecycle.py              # InstrumentRefreshScheduler
│   │   │   ├── metrics.py                       # MetricsRegistry (counter/gauge/histogram)
│   │   │   ├── session_states.py                # Session state machine
│   │   │   └── startup.py                       # boot(), RuntimeContext
│   │   │
│   │   ├── sdk/                                 # Public-facing session API
│   │   │   ├── __init__.py
│   │   │   ├── async_session.py                 # Async session wrapper
│   │   │   ├── protocols.py                     # SDK service protocols
│   │   │   ├── session.py                       # TradingSession + 7 services
│   │   │   ├── session_manager.py               # Multi-session manager
│   │   │   ├── streaming.py                     # StreamSubscription types
│   │   │   └── services/                        # Per-service implementations
│   │   │
│   │   └── strategy/                            # Strategy framework
│   │       ├── __init__.py                      # Re-exports core + all_strategies/all_scanners
│   │       ├── core/                            # Framework code (never edited by users)
│   │       │   ├── __init__.py
│   │       │   ├── protocols.py                 # Strategy protocol (on_start/on_stop/on_event)
│   │       │   ├── engine.py                    # ReactiveStrategyEngine (Signal→Order bridge)
│   │       │   ├── scanner.py                   # ScannerEngine (IndicatorComputer protocol)
│   │       │   ├── ensemble.py                  # Multi-strategy ensemble
│   │       │   └── buy_and_hold.py              # Reference strategy
│   │       └── extensions/                      # User-owned, auto-discovered
│   │           ├── __init__.py                  # all_strategies / all_scanners
│   │           ├── strategies/                  # Custom strategy classes (SmaCrossStrategy)
│   │           ├── scanners/                    # ScannerDefinition objects (momentum)
│   │           └── shared/                      # Custom indicators, helpers
│   │
│   └── tests/                                   # 106 test files
│       ├── analytics/  (8)                      # Analytics tests
│       ├── contracts/  (10)                     # Contract/integration tests (incl. CQRS Signal→Order)
│       ├── datalake/   (9)                      # Datalake tests
│       ├── execution/  (31)                     # Engine, OMS, fill source tests
│       ├── interface/  (2)                      # CLI, API tests
│       ├── integration/ (1)                     # Full-stack integration tests
│       ├── parity/     (1)                      # Cross-provider parity tests
│       ├── reactive/   (7)                      # Bus, operator tests
│       ├── replay/     (7)                      # Backtest engine tests
│       ├── runtime/    (15)                     # Startup, health, metrics tests
│       ├── sdk/        (9)                      # Session, service tests
│       └── strategy/   (6)                      # Strategy engine tests
│
└── runtime/                                       # Runtime state (git-ignored)
    ├── dhan/
    │   ├── token_state.json                     # Dhan cached token
    │   └── totp_cooldown.json                   # Dhan TOTP cooldown state
    ├── upstox/
    │   ├── token_state.json                     # Upstox cached token
    │   └── totp_cooldown.json                   # Upstox TOTP cooldown state
    ├── dhan-instruments-2026-08-06.json         # Cached Dhan master
    └── upstox-instruments-2026-08-06.json       # Cached Upstox master
```

---

## 3. Architecture Overview

### 3.1 Dependency Graph

```
┌─────────────────────────────────────────────────────────────┐
│                     tradex-trading                          │
│  ┌─────────┐ ┌──────────┐ ┌────────┐ ┌──────────────────┐  │
│  │execution│ │   sdk    │ │strategy│ │   analytics      │  │
│  │ engine  │ │ session  │ │scanner │ │ indicators       │  │
│  │ OMS     │ │ services │ │ensemble│ │  options greeks  │  │
│  │ fills   │ │ streaming│ │        │ │  fundamentals    │  │
│  └────┬────┘ └────┬─────┘ └───┬────┘ └──────────────────┘  │
│       │           │          │          ┌──────────────────┐ │
│       │           │          │          │   datalake       │ │
│       │           │          │          │  catalog/quality │ │
│       │           │          │          └──────────────────┘ │
│       │           │          │          ┌──────────────────┐ │
│       │           │          │          │   replay         │ │
│       │           │          │          │  backtest engine │ │
│       │           │          │          └──────────────────┘ │
│       │           │          │          ┌──────────────────┐ │
│       │           │          │          │   runtime        │ │
│       │           │          │          │  boot/health/metrics││
│       │           │          │          └──────────────────┘ │
│       │           │          │          ┌──────────────────┐ │
│       │           │          │          │   interface      │ │
│       │           │          │          │  CLI/API/TUI     │ │
│       └───────────┼──────────┼──────────┴──────────────────┘ │
│                   │          │                                │
│  ┌────────────────┴──────────┴────────────────────────────┐  │
│  │                    reactive/  (9 modules)             │  │
│  │  bus · bounded_bus · thread_safe_bus · async_dispatch │  │
│  │  backpressure · operators · subscription · message_log│  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────┬──────────────────────────────────────┘
                       │ imports
┌──────────────────────▼──────────────────────────────────────┐
│                     tradex-brokers                           │
│  ┌──────────┐  ┌──────────────────┐  ┌─────────────────┐   │
│  │ dhan/    │  │ upstox/          │  │ paper/          │   │
│  │ adapter  │  │ adapter          │  │ adapter         │   │
│  │ client   │  │ client           │  │                 │   │
│  │ ws_streams│ │ ws_streams       │  │                 │   │
│  │ master   │  │ master           │  │                 │   │
│  │ depth_parser│ ws_decoder       │  │                 │   │
│  └────┬─────┘  └──────┬───────────┘  └────────┬────────┘   │
│       │               │                        │            │
│  ┌────▼───────────────▼────────────────────────▼────────┐   │
│  │ common/ — auth · token · rate_limit · circuit_breaker│   │
│  │        retry · transport · cache · ws_reconnect      │   │
│  └──────────────────────────────────────────────────────┘   │
│  registry.py — BrokerFactory                                 │
└──────────────────────┬──────────────────────────────────────┘
                       │ imports
┌──────────────────────▼──────────────────────────────────────┐
│                     tradex-domain                            │
│  enums · errors · value_objects · instruments · market      │
│  options · execution · protocols · capabilities · wire      │
│  events · strategy · serialization                          │
│                                                              │
│  DEPENDENCIES: stdlib + rx (RxPY) only                       │
└──────────────────────────────────────────────────────────────┘
```

### 3.2 Key Architectural Decisions

| Decision | Rationale |
|----------|-----------|
| **Frozen dataclasses everywhere** | Immutability by default — no accidental mutation in event streams |
| **Decimal for all monetary values** | No floating-point drift in prices, quantities, P&L |
| **RxPY reactive bus** | All inter-component communication via Observable streams |
| **Protocol-based adapters** | `BrokerAdapter` is `runtime_checkable` — duck typing with safety |
| **Fail-closed capabilities** | `BrokerCapabilities` defaults to `False` — broker must opt-in |
| **Domain owns protocols** | `BrokerAdapter` lives in domain, not brokers — enforces contract |
| **Two-mode token manager** | Port mode (v4) and mint mode (v3 compat) in one class |
| **CQRS for orders** | Strategies publish `PlaceOrderCommand`, engine processes |
| **FillSource as execution seam** | Swap Simulated/Paper/Broker/Replay without touching engine |

---

## 4. Domain Layer — Deep Dive

### 4.1 `enums.py` (184 lines)

**10 StrEnum types** with open registration for extensibility.

| Enum | Members | Notes |
|------|---------|-------|
| `OrderSide` | BUY, SELL | Standard |
| `OrderType` | MARKET, LIMIT, STOP, STOP_LIMIT | Covers all broker types |
| `OrderStatus` | NEW, PENDING, ACK, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED, SUBMITTED, UNKNOWN | Full lifecycle |
| `TimeInForce` | DAY, IOC, GTC | Standard |
| `ProductType` | INTRADAY, DELIVERY, MARGIN, MTF, COVER_ORDER | Indian market specific |
| `AssetClass` | EQUITY, INDEX, FUTURE, OPTION, CURRENCY, COMMODITY, ETF, SPOT | 8 types |
| `ExchangeId` | NSE, BSE, NFO, BFO, MCX, CDS, BCD, NSE_COMM, IDX | 9 exchanges |
| `BrokerId` | DHAN, UPSTOX, PAPER, REPLAY | Extensible via `register_broker()` |
| `Timeframe` | M1, M5, M15, H1, D1, W1 | Canonical (D-7) |
| `InstrumentType` | DEPRECATED → use `AssetClass` | Emit `DeprecationWarning` on construction |

**Open registration** via `register_broker()` / `register_exchange()` — custom brokers and exchanges can be added at runtime without modifying the enum.

**Assessment:** Clean. The `InstrumentType` deprecation is handled correctly with `__init__` override emitting `DeprecationWarning`. The `StrEnum` choice enables seamless serialization to JSON and database storage.

### 4.2 `errors.py` (68 lines)

**Typed exception hierarchy:**

```
Exception
 └── SDKError                           # Base for all v4 SDK errors
      ├── AuthenticationError            # Token/auth failures
      ├── RateLimitError                 # Provider rate limit
      ├── BrokerUnavailableError         # Transport/venue down
      │    └── ConnectionTimeoutError    # Timeout (subclass of unavailable)
      ├── OrderRejectedError             # Order rejected by broker/risk
      ├── OrderSubmissionUnknownError    # May have crossed boundary
      ├── InstrumentNotFoundError         # Resolution failure
      ├── CapabilityNotSupportedError     # Capability-loud failure
      └── SessionStateError              # Lifecycle violation
```

**Assessment:** Well-designed hierarchy. `ConnectionTimeoutError` as a subclass of `BrokerUnavailableError` is correct — callers catching the broader type still handle timeouts. `OrderSubmissionUnknownError` is critical for the idempotency boundary: when the broker HTTP call succeeds but the response is lost, the order may exist at the broker.

### 4.3 `value_objects.py` (437 lines)

**7 frozen value objects** with full arithmetic, comparison, and serialization:

| VO | Key Features |
|----|-------------|
| `InstrumentId` | Parse/serialize, factory methods (equity/future/option/index/currency/commodity), display_symbol, validation of exchange/right |
| `OrderId` | String wrapper, frozen |
| `AccountId` | String wrapper, frozen |
| `CorrelationId` | UUID or string, for idempotency |
| `Price` | Decimal, non-negative, finite, comparison + subtraction operators |
| `Quantity` | Decimal, can be negative (short positions), arithmetic (+, -, *, neg, abs, bool, comparison) |
| `Money` | Decimal + currency, cross-currency add/sub rejected, arithmetic operators |
| `ProviderMetadata` | Opaque provider diagnostics (D-10/FDS 4.3) |

**Key design details:**
- `Quantity * Price → Money` (line 311-313) — type-safe arithmetic
- `Price.__post_init__` rejects NaN/Infinity/negative — prevents silent data corruption
- `Money` cross-currency arithmetic raises `ValueError` — prevents accidental INR+USD
- `InstrumentId.parse()` handles `NSE:RELIANCE`, `NFO:INFY:20260829:8000:CE` formats
- All VOs have `to_dict()` / `from_dict()` for serialization

**Assessment:** Excellent. The frozen dataclass pattern with `__post_init__` validation is the correct approach for financial value objects. The `Quantity` allowing negative values (for short positions) while `OrderRequest.quantity` must be positive (line 64-65 in execution.py) is a good separation of concerns.

### 4.4 `instruments.py` (166 lines)

**Instrument hierarchy:**

```
Instrument (abstract base, frozen)
 ├── Equity       — .of(exchange, symbol)
 ├── Index        — .of(exchange, symbol)
 ├── Future       — .of(exchange, underlying, expiry)
 ├── Option       — .of(exchange, underlying, expiry, strike, right)
 ├── Currency     — .of(exchange, symbol)
 └── Commodity    — .of(exchange, symbol)
```

**`InstrumentMeta`** — optional metadata (description, ISIN, lot_size, tick_size, extra dict).

**Key observations:**
- All instruments are **frozen dataclasses** — immutable once created
- Factory methods (`.of()`) construct the correct `InstrumentId` automatically
- `Option.__post_init__` validates `right ∈ {CE, PE}` and sets `option_type`
- `Instrument` carries `instrument_type: InstrumentType` (deprecated) for backward compat

**Assessment:** Clean hierarchy. The `Index.of()` and `Currency.of()` use `InstrumentId.equity()` which is slightly misleading semantically but functionally correct since the `InstrumentId` is just a structured identifier. Consider `InstrumentId.index()` (line 70-72) for Index and `InstrumentId.currency()` (line 75-77) for Currency in a future pass.

### 4.5 `market.py` (463 lines)

**Market data objects:**

| Type | Purpose |
|------|---------|
| `OHLC` | Open/High/Low/Close with `is_bullish` and `range` |
| `Quote` | LTP, bid, ask, volume, OI, OHLC, depth, timestamp, spread/mid_price |
| `Depth` | Bids/asks as ordered tuples of (Price, Quantity), validates ordering |
| `Candle` | Instrument + timeframe + OHLC + volume + timestamp |
| `HistoricalSeries` | Instrument + timeframe + candles list + start/end |

**`HistoricalSeries` capabilities:**
- `to_dataframe()` → pandas (lazy import)
- `to_polars()` → Polars (lazy import)
- `to_arrow()` → PyArrow (lazy import)
- `resample(timeframe)` → aggregate to coarser granularity
- `slice(start, end)` → time-range filter
- `window(size)` → tail-N candles
- `rolling(window, func)` → sliding-window aggregation
- `plot()` → matplotlib candlestick chart
- `__len__`, `__iter__`, `__getitem__` — sequence protocol

**`Depth` validation** (lines 42-79): Enforces bid descending / ask ascending order at construction — catches producer errors at the boundary.

**Assessment:** Rich and well-designed. The lazy imports for pandas/polars/arrow keep the domain dependency-free while providing convenient export. The `Depth` validation in `__post_init__` is a good defensive practice. The `_bucketize` and `_aggregate` helpers for resampling are clean.

### 4.6 `options.py` (74 lines)

**Option chain objects:**

```
OptionChain
 ├── underlying: Instrument
 └── _expiries: tuple[Expiry, ...]
       ├── expiry_date: date
       ├── pairs: tuple[OptionPair, ...]
       │     ├── call: Option
       │     ├── put: Option
       │     └── strike: Price
       ├── reference_price: Price | None
       ├── atm(offset) → OptionPair
       ├── otm(strikes) → list[OptionPair]
       └── itm(strikes) → list[OptionPair]
```

**Assessment:** Clean. The `atm()` method finds the nearest strike to `reference_price` with offset support. `otm()` and `itm()` return ordered lists. The `_nearest_index` helper uses `min()` with absolute difference — O(n) but fine for option chains (typically < 100 strikes).

### 4.7 `execution.py` (294 lines)

**Execution objects with full lifecycle:**

| Type | Purpose |
|------|---------|
| `OrderRequest` | Intent to trade — instrument, side, type, qty, price, trigger, TIF, product |
| `Order` | Durable record — all request fields + status, filled_quantity, transition_to() |
| `OrderReceipt` | Submission result — order_id, status, message |
| `Fill` | Execution report — order_id, instrument, side, qty, price, timestamp |
| `Position` | Net position — instrument, qty, avg_price, realized/unrealized P&L |
| `Account` | Cash/margin snapshot — account_id, balance, margin, equity |
| `PortfolioSnapshot` | Positions + account, total_value/pnl/net_exposure |

**Order state machine** (`_LEGAL_TRANSITIONS`, lines 25-42):

```
NEW ──────→ PENDING ──→ ACK ──→ PARTIALLY_FILLED ──→ FILLED
 │             │           │              │
 └─────────────┴───────────┴──────────────┘ → CANCELLED
 └─────────────┴───────────┴──────────────┘ → REJECTED
```

Terminal states: FILLED, CANCELLED, REJECTED (no outgoing transitions).

**`Order.transition_to(new_status)`** — returns a new frozen Order with the updated status; raises `SessionStateError` for illegal transitions.

**Assessment:** Excellent. The frozen Order with explicit state machine transitions is the correct pattern for financial order records. The `_LEGAL_TRANSITIONS` dict is a clear, testable representation of the lifecycle. `OrderRequest.__post_init__` validates quantity > 0 and price >= 0.

### 4.8 `protocols.py`

**Eleven runtime-checkable protocols:**

| Protocol | Surface |
|----------|---------|
| `BrokerAdapter` | Full adapter: lifecycle, orders, portfolio, market data, streaming, instruments |
| `ExtensionAdapter` | Extends `BrokerAdapter` with super/forever/slice/edis orders |
| `SuperOrderAdapter` / `ForeverOrderAdapter` / `SliceOrderAdapter` / `EdisAdapter` / `KillSwitchAdapter` | Capability-gated surfaces (`isinstance` before mutating) |
| `SessionFacade` | Minimal session surface for strategies (market, trade, portfolio, stream, scanner, analytics, extension) |
| `TradingCacheProtocol` | In-memory OMS cache contract shared by paper broker + trading caches |
| `Clock` | Deterministic clock (`now() -> datetime`) — backtest/live share one time API |
| `IndicatorComputer` | Indicator contract — lets `ScannerEngine` depend on a protocol, not the concrete engine |

**Assessment:** Well-scoped. `BrokerAdapter` is comprehensive without being bloated; the capability-gated adapter protocols (`SuperOrderAdapter`, `EdisAdapter`, etc.) let consumers bind a broker structurally instead of `cast`-ing. The `SessionFacade` using `object` for service types is the correct approach — domain cannot import trading types. `Clock` and `IndicatorComputer` (added in the WS-0 refactor) decouple backtest/scanner from concrete implementations. The `@runtime_checkable` decorator enables `isinstance()` checks in tests and capability discovery.

### 4.9 `capabilities.py` (165 lines)

**`BrokerCapabilities`** — 22 boolean flags + 5 numeric limits, all **fail-closed** (default `False`/`0`/`None`).

**Three provider matrices:**

| Capability | Dhan | Upstox | Paper |
|-----------|------|--------|-------|
| market_order | ✅ | ✅ | ✅ |
| limit_order | ✅ | ✅ | ✅ |
| stop_order | ✅ | ✅ | ✅ |
| modify | ✅ | ✅ | ✅ |
| cancel | ✅ | ✅ | ✅ |
| super_order | ✅ | ❌ | ❌ |
| forever_order | ✅ | ✅ | ❌ |
| slice_order | ✅ | ✅ | ❌ |
| edis | ✅ | ❌ | ❌ |
| batch_market_data | ✅ | ✅ | ❌ |
| portfolio_stream | ❌ | ✅ | ❌ |
| quote_stream | ✅ | ✅ | ❌ |
| depth_stream | ✅ (20) | ✅ (30) | ❌ |
| option_chain | ✅ | ✅ | ❌ |
| future_chain | ✅ | ✅ | ❌ |
| kill_switch | ✅ | ✅ | ❌ |
| news | ❌ | ✅ | ❌ |
| fundamentals | ❌ | ✅ | ❌ |
| max_batch_size | 1000 | 500 | 1 |
| max_stream_instruments | 1000 | 500 | — |
| asset_classes | 6 | 4 | 4 |

**Assessment:** The fail-closed design is correct for a trading platform. The capability matrix is truthful — Paper has minimal capabilities, Dhan has depth-20, Upstox has depth-30. `require_capability()` is the single gate used by SDK services.

### 4.10 `events.py` (124 lines)

**15 domain event types** for the reactive bus:

| Event | Payload | Trigger |
|-------|---------|---------|
| `OrderPlaced` | Order | Engine submits order |
| `OrderFilled` | Fill | Fill received from broker |
| `OrderCancelled` | Order | Cancel confirmed |
| `OrderRejected` | Order + reason | Risk/broker rejection |
| `PositionUpdated` | Position | Fill applied to position |
| `QuoteReceived` | Quote | Market data stream |
| `CandleReceived` | Candle | Aggregated candle |
| `ErrorOccurred` | Exception | Any error |
| `SessionStarted` | session_id | Session.start() |
| `SessionStopped` | session_id | Session.stop() |
| `RiskLimitBreached` | reason, limit | Risk manager |
| `KillSwitchTripped` | reason | Kill switch activated |
| `ReconciliationDrift` | drift_type, details | Reconciliation |
| `DataQualityAlert` | alert_type, message | Data quality check |
| `PlaceOrderCommand` | OrderRequest | CQRS command from strategy |

All events inherit from `DomainEvent` (timestamp + correlation_id).

**Assessment:** Comprehensive event set. The `PlaceOrderCommand` as a CQRS command (strategy → engine) is well-designed — strategies never call the broker directly. Events are frozen dataclasses, ensuring immutability in the stream.

### 4.11 `strategy.py` (98 lines)

**Strategy/scanner objects:**

| Type | Purpose |
|------|---------|
| `Signal` | Instrument + direction + strength + reason + metadata |
| `StrategyContext` | Session, instruments, positions, balance, timestamp, bar_count |
| `Condition` | Name + params + operator + threshold (scanner filter) |
| `ScannerDefinition` | Universe + conditions + rank_by + limit |
| `ScannerResult` | Instrument + score + matched_conditions + indicator_values + rank |

**Assessment:** Clean. `ScannerResult.to_signal()` converts a scanner hit to a trading signal. The `Condition` uses `dict[str, Any]` for params which is pragmatic — scanner conditions vary widely.

### 4.12 `serialization.py` and `wire.py`

**`serialization.py`** — Generic `to_dict()` / `from_dict()` using dataclass fields. `Serializable` protocol declares the interface.

**`wire.py`** — `InstrumentRegistry` for provider key ↔ instrument resolution, `WireAdapter` protocol, `normalize_exchange()` / `normalize_symbol()` for symbol standardization.

**Assessment:** The generic serialization avoids per-class boilerplate. The wire adapter separates provider-specific symbol formats from domain instruments.

---

## 5. Brokers Layer — Deep Dive

### 5.1 Infrastructure (`common/`) — 21 modules

The infrastructure layer provides all the plumbing for reliable broker communication:

#### Auth & Token Management

| Module | Classes/Functions | Lines | Purpose |
|--------|-------------------|-------|---------|
| `auth.py` | `totp_code()`, `jwt_expiry()`, `_form_post()`, `_extract_message()`, `_is_rate_limit_message()`, `_require_mapping()` | 364 | TOTP generation, JWT parsing, OAuth form POST |
| `token_lifecycle.py` | `DurableTokenManager`, `TokenLifecyclePort`, `TokenMintResult`, `TokenBroadcast`, `TokenRefreshScheduler`, `_TokenState` | 739 | **Core**: Two-mode token management (port/mint), cross-process file locks, atomic writes, generation tracking, 401-once semantics |
| `totp_cooldown.py` | `TotpCooldownGuard`, `TotpRateLimitError` | 348 | Cross-process TOTP attempt rate limiting (broker 2-min limit) |

**`DurableTokenManager`** is the most complex class in the codebase (739 lines). It supports:
- **Port mode (v4)**: Delegates to `TokenLifecyclePort`, adds caching + persistence
- **Mint mode (v3)**: Generation-aware with durable file, atomic writes, cross-process locks
- **401-once semantics**: A rejected token burns exactly one mint slot across processes
- **Trust-until-rejected**: Tokens with short lifetimes are not proactively expired
- **Cooldown integration**: Respects `TotpCooldownGuard` to avoid broker rate limits

**Assessment:** Complex but necessary. The dual-mode design supports both the new v4 port pattern and backward-compatible v3 mint pattern. The cross-process locking (`_process_lock` + `_exclusive_file_lock` with fcntl/msvcrt) is correct for shared token files. The atomic write (`_atomic_write_text` with fsync + directory fsync) prevents partial JSON on crash.

#### Resilience Pipeline

| Module | Classes/Functions | Lines | Purpose |
|--------|-------------------|-------|---------|
| `rate_limit.py` | `MultiBucketRateLimiter`, `TokenBucketRateLimiter`, `RateLimitConfig` | 509 | Per-path token bucket rate limiting |
| `circuit_breaker.py` | `CircuitBreaker`, `CircuitBreakerConfig`, `CircuitBreakerOpenError` | 208 | 3-state circuit breaker (closed → open → half-open) |
| `retry.py` | `RetryableHttpClient`, `RetryConfig`, `RetryExhaustedError`, `retryable()` | 202 | Exponential backoff retry for transient failures |
| `resilience.py` | Pipeline composition | — | rate_limit → retry → circuit_breaker |

**Assessment:** Standard resilience patterns implemented correctly. The `retryable()` function classifies GET/HEAD/OPTIONS as safe for auto-retry — critical for not retrying order submissions.

#### Transport

| Module | Classes/Functions | Lines | Purpose |
|--------|-------------------|-------|---------|
| `transport.py` | `HttpTransport` | 208 | stdlib urllib HTTP client with get/post/delete/put |
| `pooled_transport.py` | Connection-pooled transport | — | HTTP connection pooling |
| `provider_client.py` | `ProviderHttpClient`, `UncertainSubmissionTracker` | 356 | Rate-limited client with 401 handling, cache invalidation |
| `cache.py` | `ReadCache` | 121 | Bounded TTL cache with regex invalidation |

**Assessment:** Clean stdlib-only transport. `ProviderHttpClient` integrates rate limiting, caching, and 401 token refresh. The `UncertainSubmissionTracker` handles the edge case where a POST succeeds but the response is lost.

#### Other Infrastructure

| Module | Classes/Functions | Lines | Purpose |
|--------|-------------------|-------|---------|
| `ws_reconnect.py` | `WsReconnectManager`, `ReconnectConfig` | 256 | WebSocket auto-reconnect with exponential backoff |
| `paths.py` | `default_token_state_path()`, `default_totp_cooldown_path()` | 91 | Platform-aware default paths |
| `message_log.py` | Message log for replay | — | Record messages for debugging |
| `provider_common.py` | `resolve_instrument()`, `future_chain_from_master()`, `is_token_rejection_response()` | 295 | Shared broker utilities |
| `instruments.py` | Instrument master loading | — | Parse provider instrument masters |
| `http_response.py` | HTTP response wrapper | — | Typed response handling |

### 5.2 Dhan Adapter

| Module | Lines | Purpose |
|--------|-------|---------|
| `adapter.py` | 432 | `DhanBroker` — implements `BrokerAdapter` + `ExtensionAdapter` |
| `client.py` | 480 | `DhanApiClient` — 207 defs across the dhan package (26 in client.py) |
| `depth_parser.py` | 120 | MCX/NSE depth frame parser |
| `instruments.py` | 131 | MCX row loader, expiry parser |
| `master.py` | 146 | Dhan instrument master download/parse |
| `tick_parser.py` | 197 | Dhan tick/frame decoder |
| `ws_streams.py` | 839 | `DhanMarketDataStreamBackend`, `DhanOrderStreamBackend`, `DhanDepthStreamBackend` |

**`DhanBroker`** composition:
- Wraps `DhanApiClient` for HTTP calls
- Uses `InstrumentRegistry` for symbol resolution
- Implements all `BrokerAdapter` methods + `ExtensionAdapter` (super/forever/slice/edis)
- Capability-loud: raises `BrokerUnavailableError` when transport is not bound

**Assessment:** Well-structured. The adapter delegates to the client, keeping the adapter thin. The `DhanApiClient` encapsulates all Dhan-specific HTTP protocol details.

### 5.3 Upstox Adapter

| Module | Lines | Purpose |
|--------|-------|---------|
| `adapter.py` | 442 | `UpstoxBroker` — implements `BrokerAdapter` + `ExtensionAdapter` |
| `client.py` | 397 | `UpstoxApiClient` — 205 defs across the upstox package (22 in client.py) |
| `instruments.py` | 39 | Upstox instrument helpers |
| `master.py` | 153 | Upstox instrument master download/parse |
| `ws_decoder.py` | 124 | WSS v2 frame decoder (market/depth/LTPC) |
| `ws_streams.py` | 673 | `UpstoxMarketDataStreamBackend`, `UpstoxPortfolioStreamBackend` |

**Assessment:** Parallel structure to Dhan. The `ws_decoder.py` handles Upstox's WSS v2 protocol with frame types: MARKET_FULL, MARKET_FIRST, INDEX_FULL, DEPTH, LTPC.

### 5.4 Paper Broker

| Module | Lines | Purpose |
|--------|-------|---------|
| `adapter.py` | 742 | `PaperBroker` — simulated broker with fill engine |

**`PaperBroker`** implements:
- Full `BrokerAdapter` protocol (no streaming)
- `_fill_order()` with `_can_fill()` / `_fill_price()` logic
- `_submit_order_locked()` with thread safety
- `trading_cache`, `synchronous_fill`, `owns_position_projection` metadata
- `configure_runtime_cache()`, `set_quote()` for test injection

**Assessment:** Clean simulation. The paper broker fills orders synchronously at the latest quote or request price, making it suitable for integration testing.

### 5.5 Broker Factory (`registry.py`)

**`BrokerFactory`** — register/create/available pattern for plug-and-play broker discovery.

**Assessment:** Standard factory pattern. The `register()` method validates against `BrokerAdapter` protocol at registration time.

---

## 6. Trading Layer — Deep Dive

### 6.1 Reactive Backbone (`reactive/`)

| Module | Classes | Purpose |
|--------|---------|---------|
| `bus.py` | `ReactiveBus` | RxPY Subject-backed message bus, typed streams via `of_type()`, replay, dispose |
| `bounded_bus.py` | `BoundedReactiveBus` | Bounded-capacity bus with backpressure edges |
| `thread_safe_bus.py` | `ThreadSafeReactiveBus` | Thread-safe bus wrapper |
| `async_dispatch.py` | `AsyncDispatch` | Async event dispatch to the bus |
| `backpressure.py` | `BackpressurePresets` | Backpressure operators for high-frequency streams |
| `operators.py` | `of_type`, `share`, `replay_buffer`, `throttle_first`, `sample`, `distinct_until_changed`, `take_until`, `map_to`, `filter_safe`, `catch_error` | Custom RxPY operators |
| `subscription.py` | `DisposableSubscription`, `SubscriptionManager` | Subscription lifecycle management |
| `message_log.py` | `FileMessageLog` | File-backed message log for auditing/replay |

**`ReactiveBus`** (126 lines):
- `publish(message)` → emits to all subscribers
- `of_type(msg_type)` → filtered Observable with `share()` (multicast)
- `stream()` → raw Observable of all messages
- `subscribe()` → tracked disposable for cleanup
- `replay(start, end)` → historical message replay
- `dispose()` → disposes all subscriptions + completes subject

**Assessment:** Clean and minimal. The `CompositeDisposable` tracks all subscriptions for cleanup. The `of_type()` with `share()` ensures multiple subscribers to the same type share a single upstream subscription.

### 6.2 Execution Engine (`execution/`)

| Module | Key Classes | Lines | Purpose |
|--------|-------------|-------|---------|
| `engine.py` | `ExecutionEngine`, `RiskManager`, `MemoryIdempotencyGuard` | 588 | **Core**: Reactive order pipeline, idempotency, risk, kill switch |
| `fill_sources.py` | `FillSource` protocol, `SimulatedFillSource`, `PaperFillSource`, `BrokerFillSource`, `ReplayFillSource` | 238 | Execution mode abstraction |
| `order_manager.py` | `OrderManager` | — | Order lifecycle tracking |
| `position_manager.py` | `PositionManager` | — | Position/P&L tracking |
| `trading_cache.py` | `TradingCache` | 115 | In-memory OMS state (orders, positions, quotes) |
| `thread_safe_cache.py` | Thread-safe cache | — | Locked in-memory state |
| `reconciliation.py` | `ReconciliationEngine` | — | Drift detection between local and broker state |
| `sqlite_store.py` | `SQLiteOrderStore` | 232 | Persistent order storage |
| `fees.py` | Fee calculator | — | Indian market fee calculation |
| `slippage.py` | Slippage models | — | Fill-price slippage |
| `submission_safety.py` | Submission safety | — | Uncertain-submission handling |

**`ExecutionEngine`** pipeline:

```
OrderRequest / PlaceOrderCommand
        ↓
  1. Idempotency check (correlation_id dedupe)
        ↓
  2. Kill switch gate
        ↓
  3. Risk check (value limits, rate limits)
        ↓
  4. Fill source submit (broker/paper/simulated/replay)
        ↓
  5. OMS update (OrderManager + PositionManager)
        ↓
  6. Event publish (OrderPlaced, OrderFilled)
```

**Key design:**
- Reactive pipeline via `bus.of_type(OrderRequest).pipe(ops.filter(...)).subscribe(...)`
- CQRS support: `PlaceOrderCommand` → same pipeline
- Kill switch: `threading.Event` (thread-safe), cancels all open orders
- Idempotency: `MemoryIdempotencyGuard` with reservation + release
- Risk: configurable max_order_value, max_position_value, max_orders_per_minute

**Assessment:** Well-designed reactive spine. The pipeline is clean and testable. The `FillSource` abstraction enables swapping execution modes without touching the engine. The kill switch with `threading.Event` is thread-safe.

### 6.3 SDK Session (`sdk/`)

| Module | Key Classes | Lines | Purpose |
|--------|-------------|-------|---------|
| `session.py` | `TradingSession`, 7 services, 5 result types | 482 | **Core**: Main entry point, lifecycle, services |
| `streaming.py` | `StreamSubscription`, `BackendStreamSubscription` | 76 | Subscription types |
| `session_manager.py` | Multi-session manager | 178 | Manage multiple sessions |
| `async_session.py` | Async session wrapper | 281 | Async API wrapper |
| `protocols.py` | SDK service protocols | — | Service contracts |
| `services/` | Market/Trade/Portfolio/Stream/Scanner/Analytics/Extension services | — | Per-service implementations |

**`TradingSession`** lifecycle: `NEW → READY → STOPPED`

**7 Services:**

| Service | Methods | Purpose |
|---------|---------|---------|
| `MarketService` | quote, ltp, depth, history, ltp_batch, quote_batch, search, option_chain, future_chain | Market data |
| `TradeService` | submit, cancel, place_order, cancel_order, modify_order, get_order, get_orderbook | Order management |
| `PortfolioService` | positions, account, portfolio, get_positions, get_holdings, get_account, get_portfolio | Portfolio |
| `StreamService` | subscribe_quotes, subscribe_fills, subscribe_depth, subscribe_orders, subscribe_positions, unsubscribe | Reactive streams |
| `ScannerService` | run, top, scan | Scanner execution |
| `AnalyticsService` | indicator, indicators, report | Technical analysis |
| `ExtensionService` | super_order, forever_order, slice_order, edis, kill_switch | Broker extensions |

**Factory methods:**
- `TradingSession.paper()` → Paper broker with simulated fills
- `TradingSession.live(broker_id, confirm=True)` → Live broker with real fills

**Assessment:** Comprehensive and well-organized. The 7-service design provides clear separation of concerns. The capability-loud approach (`require_capability` before each call) prevents silent failures. The v3-parity methods with deprecation warnings enable gradual migration.

### 6.4 Strategy Framework (`strategy/`)

The framework is split into **`core/`** (framework code — never edited by users) and **`extensions/`** (user-owned, auto-discovered). `strategy/__init__.py` re-exports the public surface and `all_strategies` / `all_scanners`.

| Module | Classes | Purpose |
|--------|---------|---------|
| `core/protocols.py` | `Strategy` protocol | on_start/on_stop/on_event callbacks (`@runtime_checkable`) |
| `core/engine.py` | `ReactiveStrategyEngine` | RxPY-driven strategy execution + **Signal→`PlaceOrderCommand` bridge** (CQRS) |
| `core/scanner.py` | `ScannerEngine` | Scanner run/top with history/evaluate/matches; depends on the `IndicatorComputer` protocol |
| `core/ensemble.py` | `StrategyEnsemble`, `StrategyEntry` | Run multiple strategies, aggregate signals |
| `core/buy_and_hold.py` | `BuyAndHoldStrategy` | Reference strategy implementation |
| `extensions/` | `all_strategies`, `all_scanners` | Auto-discovery: `isinstance` against `Strategy` / `ScannerDefinition` over each sub-package's `__all__` |
| `extensions/strategies/sma_cross.py` | `SmaCrossStrategy` | Example user strategy proving discovery |
| `extensions/scanners/momentum.py` | momentum `ScannerDefinition` | Example user scanner proving discovery |

**Assessment:** Clean strategy framework. The `Strategy` protocol with `on_start/on_stop/on_event` is the standard event-driven pattern. `ReactiveStrategyEngine` drives strategies via the reactive bus and — for CQRS — converts any returned `Signal` into a `PlaceOrderCommand` published to the bus, so strategies never touch the execution layer directly. Extension isolation is enforced structurally: user code lives only in `extensions/` and is picked up by discovery, so `core/` stays untouched.

### 6.5 Runtime (`runtime/`)

| Module | Classes | Purpose |
|--------|---------|---------|
| `startup.py` | `RuntimeContext`, `boot()` | Composition root, broker boot, session creation |
| `live.py` | `build_broker_from_env()`, master download | Live broker construction from env vars |
| `health.py` | `HealthCheck`, `ComponentHealth`, `AggregateHealthCheck` | Health check system |
| `metrics.py` | `MetricsRegistry`, `_Counter`, `_Gauge`, `_Histogram` | In-process metrics |
| `calendar.py` | NSE trading calendar | Market hours |
| `market_feed.py` | `MarketFeed` | Tick bridge from broker WS to reactive bus |
| `master_lifecycle.py` | `InstrumentRefreshScheduler` | Daily instrument master refresh |
| `session_states.py` | Session state machine | State transitions |

**Assessment:** Well-organized composition root. `boot()` wires everything: broker → bus → engine → session → services. The `build_broker_from_env()` reads credentials from environment variables.

### 6.6 Analytics (`analytics/`)

| Module | Purpose |
|--------|---------|
| `engine.py` | AnalyticsEngine — indicator/report evaluation |
| `indicators.py` | SMA, EMA, RSI, Bollinger Bands, ATR, MACD, etc. |
| `options.py` | Option Greeks, implied volatility |
| `futures.py` | Futures roll, term structure analysis |
| `fundamentals.py` | Fundamental data processors |
| `breadth.py` | Market breadth indicators |
| `feature_pipeline.py` | Feature extraction pipeline |
| `functions.py` | Standalone analytics functions |
| `orderflow.py`, `volume_profile.py` | Order-flow and volume-profile analysis |
| `probability.py`, `ranking.py`, `sector.py` | Probability, ranking, sector analytics |
| `volatility.py`, `warmup.py` | Volatility analytics, warmup handling |
| `walk_forward.py` | Walk-forward analysis |

**Assessment:** Good coverage of standard technical indicators. The lazy numpy import keeps the analytics optional.

### 6.7 Datalake (`datalake/`)

| Module | Classes | Purpose |
|--------|---------|---------|
| `catalog.py` | `DataCatalog` | duckdb-backed bar storage (list_tables, has_bars, query_bars, write_bars) |
| `quality.py` | Data quality checks | Validate bar data integrity |
| `source_selection.py` | `DataSourceKind` | Source priority selection |
| `corporate_actions.py` | `CorporateActionStore` | Split/dividend adjustments |
| `mcp_server.py` | MCP server | External data access |
| `data_engine.py` | Data ingestion engine | Backfill/aggregation pipeline |
| `parquet_catalog.py` | Parquet-backed catalog | Columnar bar storage |

**Assessment:** Pragmatic duckdb-backed storage. The `DataCatalog` provides a clean API for bar persistence.

### 6.8 Replay (`replay/`)

| Module | Classes | Purpose |
|--------|---------|---------|
| `engine.py` | `ReplayEngine` | Historical data replay |
| `backtest.py` | `BacktestEngine`, `FakeClock` | Backtesting with simulated time |
| `optimization.py` | `grid_search`, `OptimizationResult` | Parameter grid search |
| `walk_forward.py` | `walk_forward` | Walk-forward optimization |

**Assessment:** Clean separation of replay (historical data) and backtest (strategy evaluation). The `FakeClock` enables deterministic time in backtests.

### 6.9 Interface (`interface/`)

| Module | Classes | Purpose |
|--------|---------|---------|
| `cli.py` | `_build_parser()`, `run_cli()` | Main CLI with argparse |
| `fastapi_app.py` | `HealthApp`, `_SystemClock` | FastAPI health endpoint |
| `check_connection.py` | `_check()`, `main()` | Connectivity probe |
| `tui.py` | `diagnose()`, `render_status()` | Terminal diagnostics |

**Assessment:** Minimal interface layer. The CLI provides connectivity checks and diagnostics.

---

## 7. Class Diagrams & Relationships

### 7.1 Domain Core

```
┌──────────────────────────────────────────────────────────────┐
│                       Value Objects                          │
├─────────────────┬─────────────────┬──────────────────────────┤
│  InstrumentId   │  OrderId        │  Price                   │
│  (exchange,     │  (value: str)   │  (value: Decimal)        │
│   underlying,   │                 │   + comparison ops       │
│   expiry,       │  AccountId      │   + subtraction          │
│   strike, right)│  (value: str)   │                          │
│  + parse()      │                 │  Quantity                │
│  + factories    │  CorrelationId  │  (value: Decimal)        │
│                 │  (value: UUID)  │   + arithmetic ops       │
│                 │                 │   + comparison ops       │
│                 │  ProviderMeta   │                          │
│                 │  (provider,     │  Money                   │
│                 │   native_id,    │  (amount: Decimal,       │
│                 │   raw: dict)    │   currency: str)         │
│                 │                 │   + arithmetic ops       │
└─────────────────┴─────────────────┴──────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│                    Instrument Hierarchy                       │
├──────────────────────────────────────────────────────────────┤
│  Instrument (abstract)                                       │
│   ├── instrument_id: InstrumentId                            │
│   ├── symbol, exchange, asset_class, currency                │
│   ├── expiry, strike, option_type, contract_size             │
│   └── meta: InstrumentMeta                                   │
│                                                              │
│   ├── Equity    .of(exchange, symbol)                        │
│   ├── Index     .of(exchange, symbol)                        │
│   ├── Future    .of(exchange, underlying, expiry)            │
│   ├── Option    .of(exchange, underlying, expiry, strike, r) │
│   ├── Currency  .of(exchange, symbol)                        │
│   └── Commodity .of(exchange, symbol)                        │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│                    Execution Objects                           │
├──────────────────────────────────────────────────────────────┤
│  OrderRequest → Order (via FillSource)                        │
│                                                              │
│  Order                                                   │
│   ├── order_id, instrument, side, type, qty, price         │
│   ├── status: OrderStatus, time_in_force                    │
│   ├── filled_quantity, correlation_id                        │
│   └── transition_to(new_status) → Order                     │
│                                                              │
│  OrderState: NEW → PENDING → ACK → PARTIALLY_FILLED → FILLED│
│                              ↓              ↓                │
│                          CANCELLED      REJECTED             │
│                                                              │
│  Fill → Position → PortfolioSnapshot                         │
└──────────────────────────────────────────────────────────────┘
```

### 7.2 Broker Adapter Pattern

```
┌──────────────────────────────────────────────────────────────┐
│              BrokerAdapter (Protocol, domain)                 │
│                                                               │
│  connect() / close()                                          │
│  submit_order(request) → OrderId                              │
│  cancel_order(id) → Order                                     │
│  modify_order(id, request) → Order                            │
│  get_order(id) → Order                                        │
│  get_quote(instrument) → Quote                                │
│  ltp(instrument) → Price                                      │
│  history(instrument, tf, start, end) → HistoricalSeries       │
│  get_option_chain(underlying) → OptionChain                   │
│  stream_backend() → object                                    │
│  market_stream_backend() → object                             │
│  ...                                                          │
└──────────┬──────────────────────────────────┬────────────────┘
           │ runtime_checkable                 │
    ┌──────▼───────┐              ┌───────────▼────────┐
    │  DhanBroker  │              │   UpstoxBroker      │
    │              │              │                     │
    │ ┌──────────┐ │              │ ┌────────────────┐  │
│ │DhanApiClient││            │ │UpstoxApiClient │  │
│ │(26 methods)││             │ │(22 methods)    │  │
    │ └──────────┘ │              │ └────────────────┘  │
    │ ┌──────────┐ │              │ ┌────────────────┐  │
    │ │Registry  │ │              │ │Registry        │  │
    │ └──────────┘ │              │ └────────────────┘  │
    │ ┌──────────┐ │              │ ┌────────────────┐  │
    │ │WS Streams│ │              │ │WS Streams     │  │
    │ └──────────┘ │              │ └────────────────┘  │
    └──────────────┘              └─────────────────────┘
           │                              │
    ┌──────▼───────┐              ┌───────────▼────────┐
    │ExtensionAdapter│             │  ExtensionAdapter  │
    │(super/forever/│              │(forever/slice)     │
    │ slice/edis)   │              │ no super, no edis  │
    └──────────────┘              └─────────────────────┘
```

### 7.3 Execution Pipeline

```
┌──────────────────────────────────────────────────────────────┐
│                   TradingSession                              │
│                                                               │
│  trade.submit(request)                                        │
│       ↓                                                       │
│  TradeService.submit(request)                                 │
│       ↓  (capability check + order gate)                      │
│  ExecutionEngine.submit(request)                              │
│       ↓                                                       │
│  ┌─────────────────────────────────────────────────────┐     │
│  │  Pipeline:                                           │     │
│  │  1. IdempotencyGuard.check_and_reserve(cid)          │     │
│  │  2. Kill switch gate                                 │     │
│  │  3. RiskManager.check(request)                       │     │
│  │  4. FillSource.submit(request) → (Order, Fill?)     │     │
│  │  5. OrderManager.on_order_created(order)             │     │
│  │  6. PositionManager.on_fill(fill)                    │     │
│  │  7. Bus.publish(OrderPlaced / OrderFilled)           │     │
│  └─────────────────────────────────────────────────────┘     │
│                                                               │
│  FillSource implementations:                                  │
│  ├── SimulatedFillSource  (backtest — fill at request price) │
│  ├── PaperFillSource      (paper — fill at LTP or nominal)   │
│  ├── BrokerFillSource     (live — delegate to broker)        │
│  └── ReplayFillSource     (replay — historical fills)        │
└──────────────────────────────────────────────────────────────┘
```

### 7.4 Reactive Data Flow

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Broker WS  │────▶│ MarketFeed  │────▶│ReactiveBus  │
│  (quotes)   │     │ (tick bridge)│   │ (Subject)   │
└─────────────┘     └─────────────┘     └──────┬──────┘
                                                │
                                    ┌───────────┼───────────┐
                                    │           │           │
                                    ▼           ▼           ▼
                              of_type(     of_type(    of_type(
                              Quote)       OrderRequest) Fill)
                                    │           │           │
                                    ▼           ▼           ▼
                              StreamService  Execution   Position
                              subscribers    Engine      Manager
```

---

## 8. Order Flow — End to End

### 8.1 Paper Trading (Simplest Path)

```
User Code:
  session = TradingSession.paper()
  req = OrderRequest(instrument=Equity.of("NSE", "RELIANCE"),
                     side=OrderSide.BUY, order_type=OrderType.LIMIT,
                     quantity=Quantity(10), price=Price(2500.00))
  receipt = session.trade.submit(req)

Flow:
  1. TradeService.submit(req)
     → capability check (supports_limit_order: True for Paper)
     → order gate check (live_orders_enabled: True)
  2. ExecutionEngine.submit(req)
     → IdempotencyGuard: skip (no correlation_id)
     → Kill switch: not set
     → RiskManager: pass (no limits configured)
  3. PaperFillSource.submit(req)
     → _make_order(req, FILLED)
     → fill_price = request.price or cache LTP or Decimal("1.0")
     → return (Order[FILLED], Fill)
  4. OrderManager.on_order_created(order)
  5. PositionManager.on_fill(fill)
  6. Bus.publish(OrderPlaced) → Bus.publish(OrderFilled)
  7. Return OrderReceipt(order_id, FILLED, "submitted")
```

### 8.2 Live Trading (Dhan)

```
User Code:
  session = TradingSession.live(BrokerId.DHAN, confirm=True)
  receipt = session.trade.submit(req)

Flow:
  1. TradingSession.live()
     → build_broker_from_env("DHAN")
        → reads DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN from env
        → creates DhanBroker with DhanApiClient
     → broker.connect() — loads instrument master
     → ExecutionEngine(BrokerFillSource(broker))
     → MarketFeed(broker, bus)
     → InstrumentRefreshScheduler (daily master refresh)
  2. TradeService.submit(req)
     → capability check (supports_limit_order: True for Dhan)
  3. ExecutionEngine.submit(req)
     → RiskManager: pass
  4. BrokerFillSource.submit(req)
     → submission_boundary_crossed = True
     → broker.submit_order(req)
        → DhanBroker.submit_order(req)
           → DhanApiClient.submit_order(req)
              → ProviderHttpClient.request(POST /orders)
                 → RateLimit → Retry → CircuitBreaker
                 → HTTP 200 → parse response → return OrderId
        → return OrderId
     → _make_order(req, ACK)
     → return (Order[ACK], None)  — fill comes via WebSocket
  5. OrderManager.on_order_created(order)
  6. Bus.publish(OrderPlaced)
  7. Return OrderReceipt(order_id, ACK, "submitted")

  Later (via WebSocket):
  8. DhanOrderStreamBackend receives fill notification
  9. Parses → Fill → Bus.publish(OrderFilled)
  10. PositionManager.on_fill(fill) (via reactive pipeline)
```

---

## 9. Market Data Flow — End to End

### 9.1 REST Quote

```
session.market.quote(instrument)
  → MarketService.quote(instrument)
     → broker.get_quote(instrument)
        → DhanBroker.get_quote(instrument)
           → DhanApiClient.quote(instrument)
              → ProviderHttpClient.request(GET /market-quote)
                 → RateLimit → Retry → HTTP 200
              → Parse JSON → Quote
           → return Quote
```

### 9.2 WebSocket Stream

```
session.stream.subscribe_quotes(on_quote)
  → StreamService.subscribe_quotes(on_quote)
     → bus.of_type(Quote).subscribe(on_quote)

session.market_feed.subscribe([inst1, inst2], depth="20")
  → MarketFeed.subscribe(instruments, depth)
     → broker.market_stream_backend()
        → DhanMarketDataStreamBackend
     → backend.subscribe(instruments)
        → WebSocket.connect(ws_url)
        → Send subscribe frame
        → Daemon loop: receive → parse → bus.publish(Quote)

  On tick received:
     → DhanMarketDataStreamBackend._on_data(frame)
        → tick_parser.parse(frame) → Quote
        → bus.publish(Quote)
        → ReactiveBus._subject.on_next(Quote)
        → StreamService subscriber receives Quote
        → on_quote(quote) called
```

---

## 10. Component Organization & Boundaries

### 10.1 Package Boundaries (Enforced)

| Boundary | Rule | Enforcement |
|----------|------|-------------|
| `domain` → stdlib + rx only | No internal deps | `pyproject.toml`: `dependencies = ["rx>=3.2,<4"]` (all 3 packages aligned) |
| `brokers` → domain + stdlib | Never imports `tradex_trading` | CI grep verified |
| `trading` → domain + brokers + stdlib | Composition root | `runtime/startup.py` |

### 10.2 Protocol Boundaries

| Protocol | Defined In | Implemented In | Consumed In |
|----------|-----------|---------------|-------------|
| `BrokerAdapter` | domain/protocols.py | brokers/dhan/adapter.py, brokers/upstox/adapter.py, brokers/paper/adapter.py | trading/sdk/session.py |
| `ExtensionAdapter` | domain/protocols.py | brokers/dhan/adapter.py, brokers/upstox/adapter.py | trading/sdk/session.py |
| `SessionFacade` | domain/protocols.py | trading/sdk/session.py | domain/strategy.py |
| `FillSource` | trading/execution/fill_sources.py | trading/execution/fill_sources.py (4 impls) | trading/execution/engine.py |
| `TokenLifecyclePort` | brokers/common/token_lifecycle.py | brokers/dhan/adapter.py, brokers/upstox/adapter.py | brokers/common/token_lifecycle.py |

### 10.3 Event Flow Boundaries

```
Strategy Layer → PlaceOrderCommand → ReactiveBus → ExecutionEngine
Broker Layer   → Quote/Depth       → ReactiveBus → StreamService
Engine Layer   → OrderPlaced/Filled → ReactiveBus → Analytics/Strategy
```

All inter-layer communication flows through the `ReactiveBus`. No direct imports between trading submodules (strategy never imports execution directly).

---

## 11. Principal Engineer Assessment

### 11.1 Architecture Strengths

**1. Strict Dependency Boundaries (A+)**
The three-package layout with enforced import boundaries is textbook clean architecture. Domain owns protocols, brokers implement them, trading consumes them. No circular dependencies.

**2. Immutability by Default (A)**
Every domain object is a frozen dataclass. Orders transition via `transition_to()` returning a new instance. This eliminates a whole class of bugs in concurrent trading systems.

**3. Decimal for Financial Values (A+)**
No floating-point anywhere in the financial path. `Price`, `Quantity`, `Money` all use `Decimal` with validation in `__post_init__`. Cross-currency arithmetic is rejected.

**4. Reactive Architecture (A-)**
RxPY provides a clean event-driven backbone. The `ReactiveBus` with `of_type()` and `share()` is well-designed. The execution pipeline as RxPY operators is elegant.

**5. Capability-Loud Design (A)**
Fail-closed `BrokerCapabilities` with `require_capability()` gates prevents silent failures. Each service checks capabilities before calling the broker.

**6. FillSource Abstraction (A+)**
The `FillSource` protocol cleanly separates execution modes. Swapping from Paper to Live is a one-line change. The `submission_boundary_crossed` flag enables correct idempotency handling.

**7. Two-Mode Token Manager (B+)**
Supports both v4 port mode and v3 mint mode. Cross-process locking with fcntl/msvcrt is correct. Atomic writes prevent partial JSON on crash.

### 11.2 Architecture Concerns

**1. `DurableTokenManager` Complexity (B-)**
739 lines for a token manager is high. The dual-mode design (port + mint) adds significant complexity. Consider splitting into `PortTokenManager` and `MintTokenManager` in v5.

**2. `TradingSession` Size (B)**
482 lines for the session module. The 7 services have been extracted to `sdk/services/` (done in v4). Remaining facade is reasonable.

**3. Missing mypy Configuration (C)**
258 type errors because mypy is not configured for the v4 layout. The `pyproject.toml` has `[tool.mypy]` sections but no `mypy_path` or per-subproject config.

**4. Error Handling in Pipeline (B)**
The `_process_request` error handler creates an `Order` with `None` for instrument/side/type/quantity — these should not be None in a real Order. The frozen dataclass should reject this.

**5. Test Environment Dependency (B → Resolved)**
The 3 env-dependent trading tests documented in the original audit are now mocked/isolated — 0 failures as of 2026-08-07.

### 11.3 Code Quality Metrics

| Metric | Value | Assessment |
|--------|-------|------------|
| Average module size | ~171 lines/file | **Good** — focused modules |
| Largest module | 1114 lines (interface/fastapi_app.py) | **Acceptable** — HTTP app surface |
| Largest single class | 739 lines (DurableTokenManager) | **High** — consider splitting |
| Test-to-source ratio | 159 test files / 168 source files | **Good** — 0.95:1 |
| Test pass rate | 2,543 / 2,545 = 99.9% | **Excellent** (2 skipped) |
| Frozen dataclasses | ~95% of domain objects | **Excellent** — immutability |
| Type annotations | Comprehensive | **Good** — but mypy not configured |
| Docstrings | Present on all public APIs | **Good** — with FDS references |

---

## 12. Quant Engineer Assessment

### 12.1 Numerical Correctness

**Price/Quantity Arithmetic (A+)**
All financial calculations use `Decimal`. The `Price` and `Quantity` value objects enforce:
- Non-negative prices
- Finite values (NaN/Infinity rejected)
- Type-safe arithmetic (`Quantity * Price → Money`)
- Cross-currency rejection in `Money`

**P&L Calculation (A)**
`Position.total_pnl = realized_pnl + unrealized_pnl` with currency preservation. `PortfolioSnapshot.total_value` sums position market values + account equity.

**Slippage Model (B)**
`SimulatedFillSource` accepts an optional `slippage_model` with `.apply(price, side, quantity)`. The interface is clean but the default is no slippage — realistic backtests need a slippage model.

### 12.2 Data Integrity

**Order State Machine (A+)**
The `_LEGAL_TRANSITIONS` dict enforces valid transitions. `Order.transition_to()` raises `SessionStateError` for illegal transitions. Terminal states (FILLED, CANCELLED, REJECTED) have no outgoing transitions.

**Idempotency (A)**
`MemoryIdempotencyGuard` with reservation + release prevents duplicate submissions. The `submission_boundary_crossed` flag in `BrokerFillSource` ensures the idempotency key is not released if the order may have reached the broker.

**Reconciliation (A-)**
`ReconciliationEngine` compares local vs broker state for drift detection. The `reconcile()` method is side-effect free — it returns `DriftItem` list without modifying state.

### 12.3 Latency Considerations

**Synchronous Order Path (B+)**
The order submission path is synchronous (no async/await), which is correct for low-latency: no event loop overhead, no coroutine scheduling. The HTTP transport uses stdlib `urllib` which is blocking but predictable.

**WebSocket Stream (A-)**
Market data streams use WebSocket with a daemon receive loop. The `WsReconnectManager` handles disconnections with exponential backoff. The tick-to-bus path is: receive → parse → publish, minimal overhead.

**Rate Limiting (A)**
Per-path token bucket rate limiting prevents broker throttling. The `MultiBucketRateLimiter` supports different limits for different API endpoints.

### 12.4 Missing for Production Quant Use

1. **No latency measurement** — no timing instrumentation on the order path
2. **No fill quality analysis** — no comparison of fill price vs arrival price
3. **No market impact model** — no estimation of order impact on price
4. **No order book simulation** — paper broker fills at LTP, not through a simulated order book
5. **No transaction cost analysis** — `fees.py` exists but is not integrated into backtest P&L
6. **No survivorship bias protection** — datalake doesn't track delisted instruments

---

## 13. Test Coverage Analysis

### 13.1 Test Distribution

| Package | Test Files | Passing | Skipped | Failed | Coverage Estimate |
|---------|-----------|---------|---------|--------|-------------------|
| domain | 15 | 343 | 0 | 0 | ~95% |
| brokers | 38 | 899 | 2 | 0 | ~90% |
| trading | 106 | 1,301 | 0 | 0 | ~85% |
| **Total** | **159** | **2,543** | **2** | **0** | **~88%** |

### 13.2 Test Categories

| Category | Files | Focus |
|----------|-------|-------|
| Unit tests | ~120 | Individual classes, methods |
| Integration tests | ~20 | Multi-component interactions |
| Parity tests | ~5 | Cross-provider consistency |
| Contract tests | ~5 | Protocol conformance |
| Regression tests | ~10 | Reactive bus, session lifecycle |
| Smoke tests | ~2 | Live broker connectivity |

### 13.3 Test Strengths

- **Parity tests** verify all brokers satisfy `BrokerAdapter` protocol
- **Cross-provider tests** ensure order lifecycle works identically on Paper
- **Reactive regression tests** verify bus delivery contracts
- **Session lifecycle tests** verify NEW → READY → STOPPED transitions
- **Kill switch tests** verify order pipeline blocking

### 13.4 Test Gaps

- **No performance tests** — no latency benchmarks
- **No stress tests** — no high-volume order submission tests
- **No WebSocket resilience tests** — no disconnection/reconnection tests
- **Limited live broker tests** — smoke tests only, no CI integration with live brokers

---

## 14. Known Issues & Failures

### 14.1 Failing Tests — Resolved (0)

The 4 failures documented in the 2026-08-06 audit are all fixed as of 2026-08-07:

**1. `test_retry_exhausted_error_is_runtime_error`** (brokers) — test expectation corrected to `BrokerUnavailableError`; the code was already correct.

**2-4. `test_check_without_session`, `test_main_default_args`, `test_main_single_broker`** (trading) — env-dependent failures resolved (broker construction now isolated/mocked); all 3 pass.

**Current state:** 0 failures, 2 skipped (brokers).

### 14.2 Warnings (1,761)

- **DeprecationWarning: `InstrumentType`** — expected, from the deprecated enum
- **DeprecationWarning: `utcnow()`/`utcfromtimestamp()`** — from RxPY and matplotlib, not our code
- **DeprecationWarning: `close()` → `stop()`** — from our own deprecation, expected
- **DeprecationWarning: `scan()` → `run()`** — from our own deprecation, expected
- **NumPy generic timedelta** — from matplotlib, not our code

### 14.3 Security Concerns

**`.env.local` with real credentials** — the file contains live API tokens, secrets, PINs, and TOTP secrets. Status as of 2026-08-07:
1. ✅ Added to `.gitignore` (verified: `git check-ignore .env.local` → ignored)
2. ✅ Never committed (working tree clean, no secrets in history)
3. ⚠️ Use a secrets manager in production (still open)

### 14.4 Operational Gaps

1. ⚠️ **No `mypy.ini` for v4** — mypy not configured for the v4 layout (open)
2. ⚠️ **No CI pipeline** — no GitHub Actions or equivalent (open)
3. ✅ **`.gitignore` present** — runtime state, caches, and `.env.local` ignored (verified)
4. ✅ **`README.md` present** — usage documentation exists
5. ✅ **Version pinning** — `rx>=3.2,<4` aligned across all 3 pyprojects (2026-08-07)

---

## 15. Recommendations

### 15.1 Immediate (This Week)

| Priority | Action | Effort | Impact | Status (2026-08-07) |
|----------|--------|--------|--------|---------------------|
| **P0** | Add `.env.local` to `.gitignore` | 1 min | Security | ✅ Done — verified ignored |
| **P0** | Fix 4 failing tests | 15 min | CI green | ✅ Done — 0 failures |
| **P1** | Add `mypy.ini` for v4 layout | 30 min | Type safety | ⚠️ Open |
| **P1** | Create `README.md` with usage examples | 1 hour | Onboarding | ✅ Done — exists |
| **P2** | Add `.gitignore` for v4 | 10 min | Clean repo | ✅ Done — exists |

### 15.2 Short Term (This Sprint)

| Priority | Action | Effort | Impact |
|----------|--------|--------|--------|
| **P1** | Split `DurableTokenManager` into Port/Mint variants | 4 hours | Maintainability |
| **P2** | Extract session services to separate modules | 2 hours | Readability |
| **P2** | Add slippage model to `SimulatedFillSource` | 2 hours | Backtest realism |
| **P2** | Integrate `fees.py` into backtest P&L | 2 hours | Accurate P&L |
| **P3** | Add latency instrumentation to order path | 3 hours | Performance visibility |

### 15.3 Medium Term (Next Quarter)

| Priority | Action | Effort | Impact |
|----------|--------|--------|--------|
| **P2** | Add order book simulation to Paper broker | 1 day | Realistic testing |
| **P2** | Add WebSocket resilience tests | 4 hours | Reliability |
| **P3** | Add performance benchmarks | 4 hours | Regression detection |
| **P3** | Add survivorship bias protection to datalake | 2 days | Data integrity |
| **P3** | Migrate to async/await for I/O-bound paths | 2 days | Throughput |

### 15.4 What NOT to Change

| Item | Reason |
|------|--------|
| Three-package boundary | Works perfectly, don't touch |
| Frozen dataclass pattern | Correct for financial domain |
| Decimal for financial values | Non-negotiable |
| Reactive bus architecture | Clean, well-designed |
| Capability-loud design | Prevents silent failures |
| FillSource abstraction | Best seam in the codebase |
| Order state machine | Correct, comprehensive |

---

## Appendix A: v3 → v4 Migration Summary

| Area | v3 | v4 | Change |
|------|-----|-----|--------|
| Source files | 78 | 168 | +115% (new modules: reactive, events, capabilities, wire, extensions) |
| Lines of code | 15,828 | 28,714 | +81% (broker adapters + trading services expanded; domain kernel lean) |
| Test files | 51 | 159 | +212% |
| Tests | ~200+ | 2,543 | +1,170% |
| Packages | 1 monolith | 3 packages | Clean boundaries |
| EventBus | Imperative pub/sub | RxPY reactive streams | Event-driven |
| Token management | Per-broker | Shared DurableTokenManager | Cross-process safe |
| Error handling | Generic exceptions | 10 typed SDK errors | Capability-loud |
| Broker protocol | Implicit | `runtime_checkable` Protocol | Contract-enforced |

## Appendix B: Quick Start

```bash
# Set up paths
export PYTHONPATH=domain/src:brokers/src:trading/src

# Run all tests
python -m pytest domain/tests brokers/tests trading/tests -q

# Compile check
python -m compileall -q domain/src brokers/src trading/src

# Lint
ruff check domain/src brokers/src trading/src

# Paper trading
python -c "
from tradex_trading.sdk.session import TradingSession
from tradex_domain.execution import OrderRequest
from tradex_domain.enums import OrderSide, OrderType
from tradex_domain.value_objects import Quantity, Price
from tradex_domain.instruments import Equity

session = TradingSession.paper()
req = OrderRequest(
    instrument=Equity.of('NSE', 'RELIANCE'),
    side=OrderSide.BUY,
    order_type=OrderType.LIMIT,
    quantity=Quantity('10'),
    price=Price('2500.00'),
)
receipt = session.trade.submit(req)
print(f'Order: {receipt.order_id}, Status: {receipt.status}')
session.stop()
"
```

---

*Report generated 2026-08-06, reconciled 2026-08-07. Based on full code-level review of 168 source files across 3 packages.*