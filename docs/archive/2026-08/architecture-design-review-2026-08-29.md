# TradeX v4 — Architecture & Flows Design Review

**Date:** 2026-08-29
**Scope:** Full repo (`domain/`, `brokers/`, `trading/`, plus `frontend/`, `services/`, `docs/`)
**Lens:** What the system *must* be for a real-money Indian-markets platform vs what it *is*
**Companion:** `docs/design/state.md` is the living design doc; this review is the baseline.

---

## 1. What the design must be (and mostly is)

These are non-negotiable for a real-money Indian platform. Each row says "design must" and "implementation does":

| Invariant | Must | Actual |
|---|---|---|
| **One order path** | Every fill mode shares one idempotency→risk→fill→OMS sequence. | ✅ `ExecutionEngine._run_pipeline` is the only path. `submit()` and `PlaceOrderCommand` both call it. |
| **Money is Decimal** | No float drift in prices, qty, P&L, fees. | ✅ `value_objects.Price`, `Quantity`, `Money` are Decimal. Fees deduct via `Decimal` math. |
| **Frozen domain** | No accidental mutation in event streams. | ✅ Every domain type is `@dataclass(frozen=True, slots=True)`. |
| **Capability-loud adapters** | Unsupported features raise typed `CapabilityNotSupportedError`. | ✅ `BrokerCapabilities` (defaults all `False`) + `require_capability` gate. |
| **Composition root** | Every wiring point lives in one function. | ✅ `runtime/startup.boot()` is the only construction site. `TradingSession.paper()`/`live()` are thin wrappers. |
| **One resilience stack** | Every broker HTTP call passes the same rate-limit → breaker → retry pipeline. | ✅ `ProviderHttpClient` → `ResiliencePipeline`. CI-gated. |
| **Fail-closed live gate** | A live broker constructed without `confirm=True` cannot trade. | ✅ `TradingSession.live()` requires `confirm=True`. `BaseBroker._allow_order_operations` is the *adapter-level* hardware gate. |
| **Parity evidence** | backtest ≡ replay ≡ paper ≡ live on a recorded tape. | ✅ CI parity gate (`.github/workflows/parity.yml`); backtest and reactive paper/live share the same slippage + fee models. |
| **Capability drift fails CI** | A broker that claims a capability it doesn't implement is a hard failure. | ✅ Tested. |
| **Single-writer live** | Two live processes on the same account must not coexist. | ✅ `SingleWriterLock` (pid file with stale-PID auto-clear). |

The bones are right. The next 1000 lines of design work are not about adding things — they are about *removing* accidental complexity and *hardening* the soft tissue.

---

## 2. Design gaps, ranked

### 🔴 Critical (would block live deployment)

#### G1. The reactive spine is a single-threaded, in-process bottleneck
**File:** `trading/src/tradex_trading/reactive/bus.py`, `thread_safe_bus.py`
**What the design must be:** A tick→order path with predictable latency, decoupled from scanner evaluation, recoverable across crashes, and observable.

**What it is:** A single `rx.subject.Subject` (FIFO, no partitioning) wrapped in an `RLock`. Every publish is synchronous and drains the queue inline. The bounded `deque(maxlen=10_000)` is the *only* durability — crash = loss of causal history, no replay.

**Why this is wrong for live:**
1. **Head-of-line blocking.** A slow `ScannerEngine` subscriber on `Quote` events sits in the same drain as the order pipeline.
2. **Reentrancy on RLock is a smell.** A subscriber that re-publishes from a worker thread deadlocks.
3. **No replayability.** Reconciliation has no way to ask "what did we think happened 5 minutes ago?"

**The shape of the fix:**
- Split the bus by **criticality** (`OrderPipeline`, `MarketData`, `Diagnostics` Subjects).
- Make `OrderPipeline` synchronous and partition-locked; make `MarketData` a true RxPY observable with `observe_on(thread_pool_scheduler)` for slow consumers.
- Replace the in-memory `deque` with a **durable append-only event log** (SQLite, you already have it for orders).
- Wire the existing `BoundedReactiveBus` back-pressure hooks (`max_queue_size`, `on_backpressure`) — they're currently no-ops.

#### G2. No margin / buying-power / per-strategy-allocation gates
**File:** `trading/src/tradex_trading/execution/engine.py` (`RiskManager`)
**What the design must be:** A live-trading risk layer that fails *closed* on margin, lets multiple strategies share a risk budget, and distinguishes "exit" from "entry" reliably.

**What it is:** `RiskManager.check()` enforces (1) master live gate, (2) `max_order_value`, (3) `max_position_value`, (4) `max_orders_per_minute`, (5) `max_daily_loss_amt` and `max_drawdown_pct`. That's it.

**What's missing for live:**
- **No margin/buying-power check.** A ₹50L position against ₹10L available margin is approved today.
- **`max_daily_loss_amt` only denies new exposure** via `_increases_exposure`; the partial-close / cover-short edge cases need tests.
- **No per-strategy allocation.** Every strategy on the same account draws from one shared risk envelope.
- **`reject_unknown_market_value`** is an opt-in boolean. **Invert the default: live brokers must opt out, paper keeps the legacy skip.**

**The shape of the fix:**
```python
@dataclass(frozen=True, slots=True)
class RiskBudget:
    strategy_id: str
    max_order_value: Decimal | None = None
    max_position_value: Decimal | None = None
    max_daily_loss_amt: Decimal | None = None
    max_drawdown_pct: Decimal | None = None

class RiskManager:
    def __init__(self, *, budgets: Mapping[str, RiskBudget], ...): ...
    def check(self, request: OrderRequest, strategy_id: str, ...) -> bool: ...
```
Margin check delegates to a `MarginProvider` protocol (Dhan and Upstox expose `fund_limits`).

#### G3. `BrokerFillSource` is not a true `FillSource`
**File:** `trading/src/tradex_trading/execution/fill_sources.py:171-217`
Live mode returns `(order, None)` always. Fills arrive via a *separate* `LiveFillBridge` → `OrderFilled` → `_apply_fill` path. The order spine has *two* live paths coupled by an applied-fill fingerprint set — a load-bearing comment at `engine.py:812` documents the invariant. Write a test that exercises the race.

### 🟠 High (correctness or maintainability at scale)

- **G4.** `BaseBroker` has ~30 one-line pass-throughs. They're correct (lifecycle gate, mutation gate, capability check) but every new operation is shotgun-surgery. Refactor to a generated wall.
- **G5.** `ReactiveStrategyEngine._pending` is a linear list with a per-bar scan. Make it `dict[InstrumentId, list]`; add `pending_max_age` to cancel stale orders; add a "last bar of dataset" test.
- **G6.** `AnalyticsEngine` is a hand-rolled `if/elif` of 91 branches. Promote to `IndicatorRegistry`; co-locate goldens with specs.
- **G7.** `ScannerEngine._history` is snapshot-only. Make it a stream consumer.
- **G8.** The SDK service layer (`trade.py`, `portfolio.py`, etc.) is a thin pass-through. Either remove it or give it a real job.
- **G9.** `services/duckdb-analytics` re-implements parts of `ScannerEngine` with no shared contract. Either keep it strictly research (move out of repo) or wire it to the same `IndicatorRegistry`.

### 🟡 Medium (organisation / DX / scale)

- **G10.** `trading/` is 25.6k LOC across 10 modules. Split into `trading_core`, `trading_datalake`, `trading_strategy`, `trading_runtime`, `trading_interface`, `trading_sdk` once the team is past 2–3 people.
- **G11.** `analytics/` is a flat directory of 30 modules. Sub-folders by family.
- **G12.** `replay/` mixes backtest driver, walk-forward, optimization, and synthetic-tick generation. Split.
- **G13.** Frontend has 11 `if (!resp.ok) throw new Error(...)` repetitions; backend has no typed-error contract. Add typed errors to routes.

### 🟢 Low (nice to have)

- **G14.** `MetricsRegistry` is in-process and never exported.
- **G15.** Keep `docs/ARCHITECTURE.md`; update after G10 lands.
- **G16.** `tests/test_import_boundaries.py` is a real design tool — keep it, update it when G10 lands.
- **G17.** `pyproject.toml` files are mostly empty (today's code review flagged this — CI runs `uv sync --frozen` and gets nothing). This is a release blocker.

---

## 3. Cross-cutting recommendations

### R1. Adopt the "explicit event log" as the spine's primary storage
Replace the in-memory `deque(maxlen=10_000)` in `ThreadSafeReactiveBus` with a SQLite append-only event log. Three concrete benefits:
1. **Replayability.** On boot, replay the last N events into a fresh `Subject`.
2. **Post-mortem.** A `reconcile` failure becomes "diff the last 1000 events against the broker book."
3. **Live-stream recovery.** A WS reconnect no longer needs to ask the broker for the last hour of fills.

You already have `SQLiteIdempotencyGuard` and `SQLiteOrderStore`; add `SQLEventLog` next to them.

### R2. Make the bus partitioned
Three Subjects, one per criticality:
- `OrderPipeline` — `OrderPlaced`, `OrderFilled`, `OrderCancelled`, `OrderModified`, `OrderRejected`, `PlaceOrderCommand`, `ErrorOccurred`. Synchronous, partition-locked, no back-pressure ever.
- `MarketData` — `Quote`, `Depth`, `Candle`, `CandleReceived`. RxPY observable with `observe_on(thread_pool_scheduler, max_concurrent=4)`; slow consumers drop with `ErrorOccurred`.
- `Diagnostics` — `ScannerResult`, log events, metrics. Best-effort, lossy, exporter.

A subscriber declares which partition it wants via `bus.of_type_in(...)` (new API). One lock per partition.

### R3. Promote the "indicator" protocol to a first-class contract
```python
@runtime_checkable
class IndicatorSpec(Protocol):
    name: str
    inputs: tuple[str, ...]
    params: dict[str, ParamSpec]
    outputs: tuple[str, ...]
    compute: Callable[[HistoricalSeries, dict], IndicatorResult]

@runtime_checkable
class IndicatorRegistry(Protocol):
    def get(self, name: str) -> IndicatorSpec: ...
    def all(self) -> tuple[IndicatorSpec, ...]: ...
```
`ScannerEngine`, `AnalyticsEngine`, `services/duckdb-analytics`, and the FastAPI route all consume the *same* registry; parity test proves identical outputs at the registry level.

### R4. Make the design review process own a "design diff" file
A single living doc that lists invariants, known gaps with severity, and proposed-but-deferred redesigns. Update it after every architectural commit.

---

## 4. What is genuinely well-designed (protect these)

1. **Three-package boundary with CI enforcement.** `tests/test_import_boundaries.py` is a real design tool.
2. **One composition root with rollback.** `boot()` is the only wiring point; the live path is the only branch that needs rollback.
3. **Idempotency with reservation + release.** `check_and_reserve` / `record_result` / `release` is the right primitive.
4. **Next-open fill semantics.** `ReactiveStrategyEngine._flush_pending` matching `BacktestEngine` is the difference between "our backtest is a lie" and "our backtest is the system."
5. **Capability-loud adapters.** The `Literal["supports_*"]` type at `require_capability` is static-typed.
6. **Capability drift CI gate.** A broker that claims a capability it doesn't implement is a hard CI failure.
7. **The `FillSource` seam.** One fill-source family (Simulated, Paper, Broker, Replay).
8. **The two order gates on purpose.** `BaseBroker._allow_order_operations` (hardware) and `session._make_order_gate` (policy) guard different failure modes.
9. **The explicit pass-through wall.** The ~30 one-liners in `BaseBroker` pay for themselves the first time you add a per-method log line or capability check.
10. **The CI parity gate.** Says "backtest ≡ paper ≡ live."

---

## 5. Sequencing — what to fix first

A real-money deployment has a different ordering than a design-purity ordering.

**Sprint 1 (live safety):**
- G2 (margin check, per-strategy budget, invert `reject_unknown_market_value` default)
- G1 partial — wire existing `BoundedReactiveBus` hooks; add `bus.messages.dropped` counter
- G3 — test for the live-fill race between `_run_pipeline` and `_apply_fill`

**Sprint 2 (durability):**
- R1 — replace the in-memory `deque` with a SQLite event log; replay on boot
- G5 — `_pending` per-instrument dict, `pending_max_age`, last-bar-of-dataset test
- G17 — fix the `pyproject.toml` / `uv.lock` so CI actually runs (prerequisite for everything else)

**Sprint 3 (correctness at scale):**
- G4 — generated pass-through wall in `BaseBroker`
- G6 — promote `IndicatorRegistry` to a first-class contract
- G8 — either remove the SDK service layer or give it a real job

**Sprint 4 (design hygiene):**
- G10 — package split (only if team size justifies it)
- G11 — analytics sub-folders
- G12 — `replay/` split

**Sprint 5 (observability):**
- R2 — partitioned bus
- G14 — Prometheus exporter (or drop the registry)
- R3 — promote the indicator protocol

**Backlog:** R4 (living design doc — *this file* + `docs/design/state.md`), G7, G9, G13, G15, G16.

---

## 6. Final word

The system is genuinely well-built at the design level. The composition root, the order pipeline, the capability table, the fill-source seam, and the test discipline are real engineering wins. The gaps are *tissue* not *bone*.

What's encouraging is that the *next 500 lines of design work* are not about adding capability — they are about *removing accidental complexity* (the SDK service layer, the hand-rolled registry, the flat folder layout) and *hardening the soft tissue* (the bus, the risk layer, the durable event log). That's the kind of work a mature codebase does next.

The bones don't need rebuilding. The spine needs partitioning, the log needs persistence, the risk layer needs margin, the indicators need a registry, and the SDK needs to be either a wall or a service.
