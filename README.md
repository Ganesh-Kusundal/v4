# TradeX v4

A broker-agnostic algorithmic trading platform for Indian markets (NSE, BSE, NFO,
MCX, CDS) with live adapters for **Dhan** and **Upstox**, plus a fully in-memory
**Paper** broker for simulation and testing.

The platform is built on a reactive (RxPY) spine with a strict three-package
dependency boundary and an immutability-first domain model.

---

## Architecture

```
tradex-domain    ← shared kernel (zero internal deps: stdlib + rx only)
    ↑
tradex-brokers   ← broker adapters (Dhan, Upstox, Paper) + resilience infra
    ↑
tradex-trading   ← execution engine, SDK session, strategy, analytics, replay
```

| Package       | Import path       | Responsibility |
|---------------|-------------------|----------------|
| `domain/`     | `tradex_domain`   | Enums, value objects, instruments, market/execution objects, protocols, capabilities, events |
| `brokers/`    | `tradex_brokers`  | Broker adapters, auth/token lifecycle, rate limiting, circuit breaker, retry, WebSocket streams |
| `trading/`    | `tradex_trading`  | Reactive execution engine, OMS, SDK `TradingSession`, strategy framework, analytics, backtest/replay, datalake |

Boundaries are enforced: `domain` never imports internal packages, `brokers`
never imports `tradex_trading`, and all inter-component communication flows
through the reactive bus.

### Key design decisions

- **Frozen dataclasses everywhere** — immutable domain objects; no accidental mutation in event streams.
- **`Decimal` for all money** — no floating-point drift in prices, quantities, or P&L.
- **Capability-loud adapters** — `BrokerCapabilities` defaults to closed; unsupported features raise typed `SDKError`s instead of failing silently.
- **`FillSource` execution seam** — swap Simulated / Paper / Broker / Replay without touching the engine.
- **CQRS order flow** — strategies publish `PlaceOrderCommand`; the engine processes.

---

## Setup

Python **3.12+** is required.

### Option A — editable installs (recommended)

```bash
pip install -e domain -e brokers -e trading
```

### Option B — PYTHONPATH

```bash
export PYTHONPATH=v4/domain/src:v4/brokers/src:v4/trading/src
```

Optional capability extras (from `trading/`): `analytics` (numpy), `datalake`
(duckdb), `api` (fastapi + uvicorn), `full`. From `brokers/`: `dhan`, `ws`, `full`.

Live credentials are read from the environment (e.g. `DHAN_CLIENT_ID`,
`DHAN_ACCESS_TOKEN`, `UPSTOX_API_KEY`, ...). Keep real tokens in a local,
git-ignored `.env.local` and load them explicitly — they are never loaded
implicitly.

---

## Quick start — paper trading

No credentials required:

```python
from tradex_trading.sdk.session import TradingSession
from tradex_domain.execution import OrderRequest
from tradex_domain.enums import OrderSide, OrderType
from tradex_domain.value_objects import Quantity, Price
from tradex_domain.instruments import Equity

session = TradingSession.paper()
req = OrderRequest(
    instrument=Equity.of("NSE", "RELIANCE"),
    side=OrderSide.BUY,
    order_type=OrderType.LIMIT,
    quantity=Quantity("10"),
    price=Price("2500.00"),
)
receipt = session.trade.submit(req)
print(f"Order: {receipt.order_id}, Status: {receipt.status}")
session.stop()
```

### Live trading

Live sessions require an explicit safety confirmation:

```python
from tradex_domain import BrokerId

session = TradingSession.live(BrokerId.DHAN, confirm=True)
quote = session.market.quote(session.equity("NSE", "RELIANCE"))
session.stop()
```

### The seven session services

`session.market`, `session.trade`, `session.portfolio`, `session.stream`,
`session.scanner`, `session.analytics`, `session.extension`.

### Standard interface

Every session exposes the same broker-agnostic surface: market data on
`session.market`, portfolio on `session.portfolio` (also reachable as
`session.account`), and orders on `session.trade`.

**Market data — `session.market`**

```python
reliance = session.equity("NSE", "RELIANCE")

quote = session.market.quote(reliance)          # full quote (LTP, OHLC, depth, volume)
ltp = session.market.ltp(reliance)              # just the last traded price
depth = session.market.depth(reliance)          # sorted bid/ask book
hits = session.market.search("RELIANCE")       # instrument search
```

Market **depth is NSE-only**: requesting it on any other exchange (BSE, MCX,
IDX, …) raises a typed `CapabilityNotSupportedError` instead of streaming an
empty book — `quote`/`ltp` still work on every exchange.

Option chains and futures chains are **capability-gated**: they work on live
brokers (Dhan/Upstox) that declare `supports_option_chain` /
`supports_future_chain`, and raise a typed `CapabilityNotSupportedError` on
brokers that don't (e.g. Paper):

```python
nifty = session.index("IDX", "NIFTY")

chain = session.market.option_chain(nifty)      # live only — expiries -> strike pairs
futures = session.market.future_chain(nifty)    # live only — futures on the underlying
```

**History** — the canonical form takes a timeframe plus an explicit window:

```python
from datetime import UTC, datetime, timedelta
from tradex_domain.enums import Timeframe

end = datetime.now(UTC)
start = end - timedelta(days=5)
bars = session.market.history(reliance, Timeframe.M5, start, end)
```

Or the convenience form — `interval` + `lookback_days` computes the window
ending now (any valid `Timeframe` value works: `"5m"`, `"15m"`, `"1h"`,
`"1d"`, …):

```python
bars = session.market.history(reliance, interval="5m", lookback_days=5)
```

With no window at all, `history` defaults to a 30-day lookback.

**Portfolio — `session.portfolio` and the `session.account` alias**

`session.account` is an alias of `session.portfolio`, so either reads the
same surface:

```python
session.portfolio.positions()   # open positions (cache-first — paper fills appear here)
session.portfolio.holdings()    # holdings (broker-delegated)
session.portfolio.account()     # account snapshot (balance, margin)
session.portfolio.funds()       # fund limits / available cash
session.portfolio.portfolio()   # positions + account snapshot

session.account.positions()     # identical via the alias
session.account.funds()
session.account.holdings()
```

`positions()` reads the session's OMS cache — the same cache the execution
engine fills — so paper fills show up immediately, and the served HTTP API
(`GET /positions`) reflects them. `session.portfolio.account()` remains the
canonical account-snapshot call.

### Datalake-backed backtesting — `ParquetBacktestLoader`

Backtest strategies directly against the local parquet datalake (`data/ohlcv`,
~500 Nifty symbols) — fully offline, no broker needed:

```python
from tradex_domain import Timeframe
from tradex_trading.datalake.backtest_loader import ParquetBacktestLoader
from tradex_trading.strategy.extensions.strategies.multi_symbol_sma_cross import (
    MultiSymbolSmaCross,
)

loader = ParquetBacktestLoader()
# Portfolio strategy — one instance trades every instrument, one engine pass:
result = loader.run(
    MultiSymbolSmaCross(fast=5, slow=20),
    universe="nifty100",
    timeframe=Timeframe.D1,
    max_workers=4,          # parallel per-symbol reads
)
print(result.total_return, result.sharpe, result.num_trades)

# Or assemble the raw candle list for a custom BacktestEngine:
candles = loader.load(universe="nifty100", timeframe=Timeframe.D1)
```

Backtest/replay sessions also expose it as `session.backtest`. The CLI script
does per-symbol runs, portfolio mode, grid-search optimization, and
walk-forward OOS validation:

```bash
python trading/scripts/backtest_datalake.py --universe nifty100 \
    --strategy multi_symbol --timeframe 1d --months 2

python trading/scripts/backtest_datalake.py --universe nifty50 \
    --strategy sma_cross --optimize --walk-forward
```

### HTTP API — `tradex serve`

Expose the session over HTTP (FastAPI + uvicorn):

```bash
tradex serve                 # paper session on 127.0.0.1:8080
tradex serve --port 9090 --broker UPSTOX   # live broker (needs credentials)
tradex serve --api-key sk-…  # gate write routes behind X-API-Key
```

Options: `--host`, `--port`, `--broker PAPER|DHAN|UPSTOX`, `--api-key`,
`--workers N`, `--reload`.

**Auth** — the `X-API-Key` header is required on **write routes only**
(`POST/PUT/DELETE /orders/*`). When `--api-key` is set, requests without it
(or with a wrong value) get `403`. All `GET` endpoints (`/health`, `/positions`,
`/orders`, `/quotes`, `/option-chain`, …) are public, so readiness probes and
read-only dashboards need no key. With no `--api-key`, writes are unauthenticated.

**Readiness** — `/health/live` (process up) and `/health/ready` (session
READY). `serve` refuses to bind unless the session is READY, and `/health/ready`
returns `503` if the session is not READY — a Kubernetes-style probe contract.

**Scaling note** — `serve` is session-bound (an in-memory `TradingSession`
cannot be pickled across processes). `--workers N` and `--reload` therefore run
through an importable factory: each uvicorn worker/reload process rebuilds its
own session from the environment. State (orders, positions, streams) is per
process, and every `--reload` restart resets it — scale out horizontally by
running N instances on separate ports behind a reverse proxy. `--workers` /
`--reload` are **paper-only**: a live broker re-authenticates per process
(interactive TOTP, cooldowns), so live scaling means separate `tradex serve`
instances. If both are given, uvicorn ignores `--workers` (reload wins).

---

## Development

```bash
export PYTHONPATH=v4/domain/src:v4/brokers/src:v4/trading/src

# Run all tests
python -m pytest domain/tests brokers/tests trading/tests -q

# Compile check
python -m compileall -q domain/src brokers/src trading/src

# Lint
ruff check domain/src brokers/src trading/src

# Type-check (per package)
python -m mypy --config-file domain/pyproject.toml  domain/src
python -m mypy --config-file brokers/pyproject.toml brokers/src
python -m mypy --config-file trading/pyproject.toml trading/src
```

### Benchmarks

Stdlib-only micro-benchmarks (order path latency, bus throughput, tick decode)
live in `benchmarks/`:

```bash
python benchmarks/bench_order_path.py
```

---

## Documentation

- `docs/DEEP_REVIEW.md` — full architectural review, class diagrams, order/market-data flow, and assessments.
- `V3_TO_V4_COMPLETION_PLAN.md` — migration and completion history.
