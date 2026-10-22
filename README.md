# TradeX v4

A broker-agnostic algorithmic trading platform for Indian markets (NSE, BSE, NFO,
MCX, CDS) with live adapters for **Dhan** and **Upstox**, plus a fully in-memory
**Paper** broker for simulation and testing.

The platform is built on a reactive (RxPY) spine with a 14-member `uv`
workspace and an immutability-first domain model. Each package may import only
the packages its declared boundary allows; that graph is enforced by
`tests/test_import_boundaries.py` and the import-linter contracts in the root
`pyproject.toml`.

---

## Architecture

A `uv` workspace of 14 installable packages. `tradex_domain` is the shared
kernel; no package may import upward past its declared boundary, enforced by
`tests/test_import_boundaries.py` and the import-linter contracts in the root
`pyproject.toml`.

| Package | Import path | Responsibility |
|---|---|---|
| `domain/` | `tradex_domain` | Enums, value objects, instruments, protocols, capabilities, events, paths |
| `brokers/` | `tradex_brokers` | Dhan, Upstox and Paper adapters: auth, tokens, rate limits, streams |
| `config/` | `tradex_config` | `AppConfig`, environment loading, secret-file checks |
| `observability/` | `tradex_observability` | Metrics registry |
| `analytics/` | `tradex_analytics` | Indicators, reports, order-flow profiles |
| `reactive/` | `tradex_reactive` | RxPY bus |
| `execution/` | `tradex_execution` | Execution engine, OMS, idempotency, event store |
| `strategy/` | `tradex_strategy` | Strategy runtime, artifacts, extensions |
| `replay/` | `tradex_replay` | Backtest engine, walk-forward, Monte Carlo |
| `application/` | `tradex_application` | Application command handlers |
| `market_data/` | `tradex_market_data` | Datalake, parquet storage, history fetch, window loading |
| `interfaces/` | `tradex_interfaces` | FastAPI/WS routes and the `tradex` CLI |
| `runtime/` | `tradex_runtime` | Composition root (`boot`), `TradingSession`, feed supervision |
| `services/duckdb-analytics/` | `duck_analytics` | Read-only DuckDB service over the parquet datalake |

### Key design decisions

- **Frozen dataclasses everywhere** — immutable domain objects; no accidental mutation in event streams.
- **`Decimal` for all money** — no floating-point drift in prices, quantities, or P&L.
- **Capability-loud adapters** — `BrokerCapabilities` defaults to closed; unsupported features raise typed `SDKError`s instead of failing silently.
- **`FillSource` execution seam** — swap Simulated / Paper / Broker without touching the engine.
- **One path to the venue** — `ExecutionEngine.submit()` is the only door (AC-22). A strategy sizes its signal, adds protective legs, and hands the request to the engine; the bus carries market data in and observations out, never an order.

---

## Setup

Python **3.12+** and [`uv`](https://docs.astral.sh/uv/) are required.

```bash
uv sync --frozen
```

`uv sync` installs every workspace member editable from `uv.lock`; no
`PYTHONPATH` setup is needed.

Live credentials are read from the environment (e.g. `DHAN_CLIENT_ID`,
`DHAN_ACCESS_TOKEN`, `UPSTOX_API_KEY`, ...). Keep real tokens in a local,
git-ignored `.env.local` and load them explicitly — they are never loaded
implicitly.

---

## Quick start — paper trading

No credentials required:

```python
from decimal import Decimal

from tradex_config import AppConfig, RiskConfig
from tradex_domain.enums import OrderSide, OrderType
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_runtime import TradingSession

config = AppConfig(
    risk=RiskConfig(
        max_order_value=Decimal("100000"),
        max_position_value=Decimal("500000"),
        max_orders_per_minute=10,
        max_daily_loss_amt=Decimal("5000"),
        max_drawdown_pct=Decimal("5"),
    )
)
session = TradingSession.paper(config=config)
try:
    request = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("2500.00")),
    )
    receipt = session.engine.submit(request)
    print(f"Order: {receipt.order_id}, Status: {receipt.status}")
finally:
    session.stop()
```

`TradingSession.paper()` returns a READY session backed by `PaperBroker`.
`session.engine` is the single order door. Boot refuses to start until every
required risk limit is named or explicitly set to `UNBOUNDED`; the snippet
names all five. A marketable limit order fills immediately; outside the
simulated session it stays `ACK` until filled.

### Live trading

Live mode is entered through `tradex serve`; the explicit CLI invocation is
the confirmation gate. It requires credentials, an API key and a complete risk
policy in the environment, plus the tape the session must stream:

```bash
export DHAN_CLIENT_ID=...
export DHAN_ACCESS_TOKEN=...
export TRADEX_SERVE_API_KEY=...
export TRADEX_FEED_INSTRUMENTS=NSE:RELIANCE,NSE:INFY
export TRADEX_RISK_MAX_ORDER_VALUE=100000
export TRADEX_RISK_MAX_POSITION_VALUE=500000
export TRADEX_RISK_MAX_ORDERS_PER_MINUTE=10
export TRADEX_RISK_MAX_DAILY_LOSS=5000
export TRADEX_RISK_MAX_DRAWDOWN_PCT=5

tradex serve --broker DHAN --api-key "$TRADEX_SERVE_API_KEY"
```

A limit left unset refuses the boot with `MissingRiskLimitError`; a live
broker on a non-loopback host is refused as well.

### Session surface

`TradingSession` exposes the composed components directly.

| Member | What it is |
|---|---|
| `session.engine` | The single `ExecutionEngine`; `submit(OrderRequest)` is the only order door |
| `session.cache` | The OMS projection (orders, positions, cash) |
| `session.broker` | The active `BrokerAdapter` |
| `session.bus` | The reactive bus |
| `session.strategy_engine` | The strategy runtime, when one was wired |
| `session.market_feed` | The feed handle, when one was cold-started |
| `session.feed_supervisor` | Feed liveness supervisor |
| `session.state` | `NEW` → `READY` → `STOPPED` |
| `session.capabilities` | Broker capability declaration |

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
uv sync --frozen

# Full suite (also what CI runs)
uv run --no-sync python -m pytest -c pyproject.toml -q -p no:cacheprovider

# Every deploy-blocking check: install, lint, types, imports, tests, image
bash scripts/predeploy_gate.sh
```

### Benchmarks

Stdlib-only micro-benchmarks (order path latency, bus throughput, tick decode)
live in `benchmarks/`:

```bash
uv run --no-sync python benchmarks/bench_order_path.py
```


