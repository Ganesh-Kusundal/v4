# v4 Feature Parity Matrix

Tracks implementation status of every feature (F1–F28), non-functional requirement (N1–N14), parity assertion (P1–P4), and engineering assertion (E1–E4) in the v4 platform.

**Status key:** ✅ Done | 🟡 Partial | ❌ Not started

---

## Functional Features (F1–F28)

| ID | Feature | Phase | Status | Notes |
|----|---------|-------|--------|-------|
| F1 | Enum types (OrderSide, OrderType, OrderStatus, Timeframe, etc.) | 1 | ✅ | `enums.py` — 10 StrEnum types |
| F2 | Error hierarchy (SDKError + 9 typed errors) | 1 | ✅ | `errors.py` — full hierarchy |
| F3 | Value objects (InstrumentId, OrderId, Price, Quantity, Money) | 1 | ✅ | `value_objects.py` — frozen dataclasses |
| F4 | Instrument hierarchy (Instrument → Equity, Future, Option, etc.) | 1 | ✅ | `instruments.py` — 6 concrete types + InstrumentMeta |
| F5 | Market objects (Quote, Depth, Candle, HistoricalSeries) | 1 | ✅ | `market.py` |
| F6 | Options (OptionChain, Expiry, OptionPair) | 1 | ✅ | `options.py` |
| F7 | SDK MarketService (quote, ltp, depth, history) | 6 | ✅ | `sdk/session.py` |
| F8 | SDK TradeService (submit, cancel) | 6 | ✅ | `sdk/session.py` |
| F9 | SDK PortfolioService (positions, account, portfolio) | 6 | ✅ | `sdk/session.py` |
| F10 | Strategy protocol + ReactiveStrategyEngine | 10 | ✅ | `strategy/core/protocols.py`, `strategy/core/engine.py` (re-exported from `strategy/`) |
| F11 | ScannerEngine | 10 | ✅ | `strategy/core/scanner.py` (depends on `IndicatorComputer` protocol, not concrete engine) |
| F12 | ExecutionEngine (reactive order spine) | 5 | ✅ | `execution/engine.py` — RxPY pipeline |
| F13 | OMS (OrderManager, PositionManager, TradingCache) | 5 | ✅ | `execution/order_manager.py`, `position_manager.py`, `trading_cache.py` |
| F14 | SDK StreamService (reactive subscriptions) | 6 | ✅ | `sdk/streaming.py` |
| F15 | Analytics engine + indicators (SMA, EMA, RSI) | 10 | ✅ | `analytics/engine.py`, `analytics/indicators.py` |
| F16 | Datalake (DataCatalog, quality, source selection) | 10 | ✅ | `datalake/catalog.py`, `quality.py`, `source_selection.py` |
| F17 | Replay + Backtest engines | 10 | ✅ | `replay/engine.py`, `replay/backtest.py` |
| F18 | Paper broker adapter | 4 | ✅ | `paper/adapter.py` — full BrokerAdapter |
| F19 | Dhan broker adapter | 8 | ✅ | `dhan/adapter.py` — full adapter, protocol conformance (432 lines) |
| F20 | Upstox broker adapter | 9 | ✅ | `upstox/adapter.py` — full adapter, protocol conformance (442 lines) |
| F21 | CLI interface | 11 | ✅ | `interface/cli.py` |
| F22 | Config schema + loaders (YAML, env) | 7 | ✅ | `config/schema.py`, `env.py`, `loader.py` |
| F23 | Serialization (to_dict/from_dict) | 1 | ✅ | `serialization.py` — generic machinery |
| F24 | Fee calculator + PricingService | 5 | ✅ | `execution/fees.py` |
| F25 | Reconciliation engine | 5 | ✅ | `execution/reconciliation.py` |
| F26 | Runtime boot (composition root) | 7 | ✅ | `runtime/startup.py` — `boot()` |
| F27 | HTTP health API + TUI diagnostics | 11 | ✅ | `interface/fastapi_app.py`, `interface/tui.py` |
| F28 | Corporate actions + MCP server | 10 | ✅ | `datalake/corporate_actions.py`, `datalake/mcp_server.py` |
| F29 | Datalake-backed backtesting | 11 | ✅ | `datalake/backtest_loader.py` (`ParquetBacktestLoader`), `datalake/market_provider.py`, `session.backtest`, `scripts/backtest_datalake.py` (per-symbol + portfolio modes, grid-search + walk-forward) |

---

## Non-Functional Requirements (N1–N14)

| ID | Requirement | Phase | Status | Notes |
|----|-------------|-------|--------|-------|
| N1 | Domain has ZERO internal deps (stdlib + rx only) | 1 | ✅ | `pyproject.toml` — only `rx>=3.2,<4` (all 3 packages aligned) |
| N2 | Auth module (TOTP/OAuth) | 2 | ✅ | `common/auth.py` |
| N3 | Token lifecycle (DurableTokenManager, broadcast, refresh) | 2 | ✅ | `common/token_lifecycle.py` |
| N4 | Rate limiting (per-broker tables) | 2 | ✅ | `common/rate_limit.py` — TokenBucketRateLimiter |
| N5 | Circuit breaker (3-state) | 2 | ✅ | `common/circuit_breaker.py` |
| N6 | Retry (safe-vs-write classification) | 2 | ✅ | `common/retry.py` |
| N7 | Resilience pipeline (rate_limit → retry → circuit_breaker) | 2 | ✅ | `common/resilience.py` |
| N8 | Transport + ProviderHttpClient | 2 | ✅ | `common/transport.py`, `common/provider_client.py` |
| N9 | Trading calendar (NSE) | 7 | ✅ | `runtime/calendar.py` |
| N10 | Health checks | 7 | ✅ | `runtime/health.py` |
| N11 | Metrics registry | 7 | ✅ | `runtime/metrics.py` |
| N12 | Adapter protocols (BrokerAdapter, ExtensionAdapter) | 1 | ✅ | `protocols.py` — `@runtime_checkable` |
| N13 | Capability-loud design (fail-closed) | 1 | ✅ | `capabilities.py` — `require_capability()` |
| N14 | Wire mapping (InstrumentRegistry, normalize) | 1 | ✅ | `wire.py` |

---

## Parity Assertions (P1–P4)

| ID | Assertion | Status | Test |
|----|-----------|--------|------|
| P1 | All brokers satisfy `BrokerAdapter` protocol | ✅ | `tests/parity/test_cross_provider.py::TestProtocolConformance` |
| P2 | Capability matrix is truthful (fail-closed) | ✅ | `tests/parity/test_cross_provider.py::TestCapabilityMatrix` |
| P3 | Order lifecycle works identically on Paper | ✅ | `tests/parity/test_cross_provider.py::TestOrderLifecycleParity` |
| P4 | BrokerFactory discovers all brokers | ✅ | `tests/parity/test_cross_provider.py::TestBrokerFactoryParity` |

---

## Engineering Assertions (E1–E4)

| ID | Assertion | Status | Test |
|----|-----------|--------|------|
| E1 | ReactiveBus delivers typed events correctly | ✅ | `tests/reactive/test_reactive_regression.py::TestReactiveBusContract` |
| E2 | Kill switch blocks order pipeline | ✅ | `tests/contracts/test_session_smoke.py::TestKillSwitchSmoke` |
| E3 | Session lifecycle (NEW → READY → STOPPED) | ✅ | `tests/contracts/test_session_smoke.py::TestSessionLifecycle` |
| E4 | 3 repos compile independently | ✅ | Verified via `compileall` across all 3 repos |

---

## Architecture Assertions

| Assertion | Status | Evidence |
|-----------|--------|----------|
| `tradex-domain` imports ONLY stdlib + rx | ✅ | `domain/pyproject.toml` |
| `tradex-brokers` NEVER imports `tradex_trading` | ✅ | CI grep verified |
| `tradex-trading` imports both domain + brokers | ✅ | Composition root in `runtime/startup.py` |
| `BrokerAdapter` protocol lives in domain | ✅ | `domain/src/tradex_domain/protocols.py` |
| `BrokerFactory` lives in brokers | ✅ | `brokers/src/tradex_brokers/registry.py` |
| All market data flows through RxPY Observable | ✅ | `ReactiveBus.of_type()` + `share()` |
| EventBus is fully reactive | ✅ | No imperative pub/sub — all via Subject |
| BrokerFactory enables plug-and-play | ✅ | `register()` / `create()` / `available()` |
| v3 is completely untouched | ✅ | v3 lives in a sibling workspace; this checkout is the v4 tree |

---

## Integration Gate

```bash
# Domain
cd domain && python -m compileall -q src && pytest -q && ruff check src

# Brokers
cd brokers && python -m compileall -q src && pytest -q && ruff check src

# Trading
cd trading && python -m compileall -q src && pytest -q && ruff check src
```

**Result:** ALL 3 repos GREEN ✅ — 2,543 passing, 0 failures (2 skipped), ruff clean (verified 2026-08-07)

---

## Bug Fix Audit (post-completion)

| Bug | File | Fix | Status |
|-----|------|-----|--------|
| IdempotencyGuard `check_and_mark` → `check_and_reserve` | engine.py, sqlite_store.py | Method name + condition logic corrected | ✅ Fixed |
| BrokerFillSource `place_order` → `submit_order` | fill_sources.py | Dispatch to correct adapter method | ✅ Fixed |
| DurableTokenManager deadlock | token_lifecycle.py | `Lock` → `RLock` | ✅ Fixed |
| OrderManager always sets FILLED | order_manager.py | Partial fill → `PARTIALLY_FILLED` | ✅ Fixed |
| boot() missing safety gates | startup.py, schema.py | Mode validation, live gates, `live_enabled` | ✅ Fixed |
| PaperFillSource quantity-as-price | fill_sources.py | Nominal `Decimal("1.0")` paper price | ✅ Fixed |
| `distinct_until_changed` key ignored | reactive/operators.py | Pass `key` to RxPY operator | ✅ Fixed |
| SimulatedFillSource dead code | fill_sources.py | Simplified fill_price logic | ✅ Fixed |
| RiskManager thread safety | engine.py | Added `threading.Lock` | ✅ Fixed |
| kill_switch thread safety | engine.py | `bool` → `threading.Event` | ✅ Fixed |
| ReactiveBus.dispose() resource leak | bus.py | `subscribe()` tracks disposables | ✅ Fixed |
| Missing `trip_kill_switch`/`reconcile` | engine.py | Ported from v3 | ✅ Fixed |
| Health check hardcoded mode | health.py | Read from `session.mode` | ✅ Fixed |
| Missing exports in `__init__.py` | execution/__init__.py | Added Idempotency* classes | ✅ Fixed |

---

## Completion Audit (v3 vs v4)

| Area | v3 modules | v4 modules | Status |
|------|-----------|-----------|--------|
| Domain | 8 files | 15 files | ✅ v4 adds events, protocols, capabilities, wire, lifecycle |
| Analytics | 15 files | 18 files | ✅ All v3 analytics ported + orderflow, probability, ranking, warmup |
| Execution | 8 files | 12 files | ✅ Match + IdempotencyGuard + trip_kill_switch + reconcile + slippage, submission_safety |
| Brokers common (infra) | 16 files | 21 files | ✅ All v3 infra ported + base, client_shared, streaming |
| Strategy | 4 files | core/ 5 + extensions/ 6 (+ init files) | ✅ Framework in `strategy/core/`, user code in `strategy/extensions/` (auto-discovered) |
| Replay | 2 files | 5 files | ✅ Match + optimization, walk_forward |
| Datalake | 5 files | 8 files | ✅ Match + data_engine, parquet_catalog |
| Runtime | 6 files | 9 files | ✅ All v3 runtime ported + safety gates |
| SDK | 2 files | 6 + services/ | ✅ Core services + services/ subpackage |
| Interface | 4 files | 5 files | ✅ Match (api → fastapi_app) |
| Config | 3 files | 4 files | ✅ Match + `live_enabled` gate |
| Reactive (new) | — | 9 files | ✅ RxPY backbone (bus, bounded_bus, thread_safe_bus, async_dispatch, message_log, backpressure, operators, subscription) |
| **Total source** | **102** | **168** | ✅ v4 has MORE modules (15 domain + 54 brokers + 99 trading) |
| **Tests** | — | **159 test files / 2,543 passing** | ✅ 0 failures (2 skipped) |
