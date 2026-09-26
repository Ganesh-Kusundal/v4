# TradeX v4 — Proposed Target: Architecture, Organization, Flows & Integration

**Status:** Proposal (supersedes the package section of `trading-predeploy-plan.md`)
**Date:** 2026-09-26
**Basis:** current-tree review, `graphify` knowledge graph (16,253 nodes @ `709274c`),
`RUNTIME_FLOW_AUDIT_sections_1_7_9.md`, and the existing `target-architecture-design.md`.

This is a **superset** of the existing predeploy plan, not a replacement for it. The
plan's "16 → 12 packages" and layered dependency direction are correct and are kept
verbatim below. What this adds is the thing the design document promised and nobody
built — **the ports** — plus two ordering corrections that outrank all structural work.

---

## 0. The one-paragraph version

The core is already good: a value-object kernel (`Price`, `Quantity`, `Order*` — the
graph's god nodes) and a real reactive bus carry the system, and the strategy correctly
never touches the broker. What is missing is the **contract layer between packages**, so
"package boundary" currently means "a folder and a `pyproject.toml`" rather than "a
narrow interface." Introduce the ports first, then let the 17 packages collapse toward
12 real ones. But do that **after** the money path is provably safe — the red suite, the
CI gate that has never run, and the dual position-writer are the actual risks, and they
are cheaper to fix than any restructure.

---

## 1. Diagnosis (what the evidence actually says)

| # | Finding | Evidence | Severity |
|---|---|---|---|
| 1 | Test suite is **red**; CI has effectively never run a test | 7 failures in `tests/runtime_audit/`; `quality-gate.yml` halts at ruff (step 4) | **Critical** |
| 2 | **Two writers to position state**; `MarkToMarketService` bypasses `PositionAccountant`'s lock | `mark_to_market.py:98`; flow audit §0.3; `M-1` in deep-review | **Critical** |
| 3 | **No `Candle` is ever published on the bus** → bar-driven strategies are dead in live/paper | flow audit §0.1: 0 of 30 `.publish(` sites publish `Candle` | **Critical** |
| 4 | 17 packages, but 6 are ceremony: `persistence` 19 LOC, `operations` 41, `application` 100, `trading` is 154 shims | LOC census; `trading/src` = 2,659 LOC of which 1,673 is shims | Important |
| 5 | Design's core **ports do not exist** (`MarketDataPort`, `ExecutionCommandPort`, `RiskPort`) | grep: not found under those names | Important |
| 6 | `runtime → trading` cycle, allow-listed; the guard is blind to `import_module` shims | `runtime/session.py:7`; `test_import_boundaries.py:123` walks only `ast.Import` | Important |
| 7 | Undeclared deps after the split: `interfaces→brokers`, `reactive→rx`, `market_data→pandas/pyarrow` | works transitively, breaks standalone install | Minor |
| 8 | 26 of 42 mutating HTTP ops unauthenticated; SIGTERM bypasses teardown | deep-review findings; design §8 | Important |

Findings 1–3 are **behavioural**. 4–8 are **structural**. This ordering drives the plan.

---

## 2. Proposed organization: 12 packages

Kept from the predeploy plan. The added column is *why it earns its boundary*.

```
                          tradex_domain                 pure kernel, zero internal imports
                                ▲
        ┌───────────┬───────────┼───────────┬────────────┐
   brokers      execution    market_data    strategy    analytics
   venue I/O    money path   parquet lake   signals     indicators
        │           │             │            │            │
        └───────────┴───── replay ─────────────┴────────────┘
                                ▲
                         tradex_runtime            THE ONLY composition root
                                ▲
                                │
                         tradex_interfaces           imports domain + runtime ONLY
```

| Package | Verdict | Earns its boundary because |
|---|---|---|
| `domain` | KEEP | Pure kernel. 430 tests, mypy-clean. Zero internal imports. |
| `brokers` | KEEP | Venue I/O + auth lifecycle. Distinct failure modes (401 retry, TOTP cooldown). |
| `execution` | KEEP | The money path. One owner of orders/positions/cash. |
| `market_data` | KEEP | Owns the lake; only package allowed to open Parquet files. |
| `strategy` | KEEP | Signal producers. Cannot import broker or execution internals. |
| `analytics` | KEEP | Pure compute; the parity surface. |
| `replay` | KEEP | Backtest/walk-forward; reuses the *same* engine as live. |
| `reactive` | KEEP | The bus primitive — the one cross-cutting seam. |
| `config` | KEEP | `AppConfig` contract. |
| `observability` | KEEP | Cross-cutting metrics/audit. Folding saves nothing. |
| `runtime` | KEEP + ABSORB | Composition root. Absorbs `TradingSession` from `trading/`. |
| `interfaces` | KEEP, NARROW | HTTP/WS/CLI. Fan-in 11 → 2. |
| ~~`persistence`~~ | **DELETE** | 19 LOC, pure re-export of `tradex_execution.sqlite_store`. |
| ~~`operations`~~ | **DELETE** | 41 LOC, a dict of markdown paths. Not code. |
| ~~`research`~~ | **DELETE pkg**, salvage `pit.py` | 0 production importers; PIT validators move to test-support. |
| ~~`application`~~ | **DELETE** | 100 LOC, 1 caller, mostly `return engine.x(request)`. |
| ~~`trading/`~~ | **DELETE src** (tests/scripts MOVE) | 154 shims + 5 real files. |

### The 5 enforceable dependency rules

1. `domain` imports nothing internal.
2. Exactly one composition root (`runtime`).
3. `interfaces` imports only `domain` + `runtime`.
4. No cross-package private reads.
5. **Protocols are complete** — if the engine calls it, the Protocol declares it.

Rule 5 is new, and it is the one that was silently violated.

---

## 3. The missing contract: the ports

This is the core addition. The design document defines these; the code does not have
them. Introduce them **before** moving code, so each package boundary is a real interface
boundary rather than a folder.

```python
# tradex_domain/ports.py — pure kernel, no internal imports

class MarketDataPort(Protocol):
    def subscribe(self, request: MarketDataSubscription) -> Subscription: ...
    def history(self, request: HistoryRequest) -> HistorySnapshot: ...
    def health(self, instrument: InstrumentId) -> FeedHealth: ...

class ExecutionCommandPort(Protocol):
    def submit(self, command: PlaceOrderCommand) -> CommandReceipt: ...
    def modify(self, command: ModifyOrderCommand) -> CommandReceipt: ...
    def cancel(self, command: CancelOrderCommand) -> CommandReceipt: ...

class RiskPort(Protocol):
    def evaluate(self, request: RiskRequest) -> RiskDecision: ...
    def on_market_state(self, state: MarketState) -> None: ...
    def on_position(self, position: Position) -> None: ...
```

Because the ports live in `domain`, every consumer depends on the *interface*, never on
`tradex_market_data` or `tradex_execution` directly. That is what makes rule 3 mechanically
enforceable rather than aspirational.

---

## 4. Flows

### 4.1 Boot (fail-closed composition)

```
tradex → tradex_interfaces.cli:main
  → boot(AppConfig)                                  runtime/startup.py
       0. safety gates   (mode ∈ {paper,backtest,replay,live};
                          live ⇒ live_enabled + persistence.path)
       1. broker         (live: build_broker_from_env; else transport-less adapter)
       2. ReactiveBus    (live: ThreadSafeReactiveBus)
       3. fill source    (paper|replay=Simulated, backtest=Simulated, live=Broker)
       4. durability     (opt-in SQLite: idempotency guard, order store, event store)
       5. engine         (risk, feed-supervisor, cash, fees, metrics)
       6. strategies     (registered, discovered scanners bound)
  ⇒ TradingSession READY          (any failure ⇒ no session)
  ⇒ /health/ready 200;  `serve` refuses to bind unless READY
```

### 4.2 Live order (target — see `target-order-flow.html`)

```
Feed ─tick→ Bus ─publish Candle(bar close)→ Strategy ─on_bar→ Signal
                                                    │
                                    PlaceOrderCommand│ (bus)
                                                    ▼
Bus ─submit→ Execution ─reserve+check→ Risk ─approved→ Execution
                                     ├─ append intent (durable first)
                                     ├─ place_order via BrokerAdapter (venue-first)
                                     │     └─ fill ack  |  UNKNOWN → reconcile, never blind retry
                                     ├─ apply_fill → position, cash, fees
                                     │     └─ ONE mutator: PositionAccountant, under its lock
                                     └─ publish Fill/OrderUpdated → Bus → Strategy
```

Two things change vs today:
- **`Candle` is published on the bus** (finding 3). Bar-driven strategies work live/paper.
- **One position writer** (finding 2). `MarkToMarketService` goes through
  `PositionAccountant`, or becomes a pure computation the accountant applies.

### 4.3 Shutdown (reachable and ordered)

```
SIGTERM → stop intake → set kill switch → stop strategies
        → drain engine → close broker → flush + close idempotency guard
        → close event store → release writer lock
```

Wired into `TradingSession.stop()` via `ShutdownCoordinator` (currently stranded-correct).

### 4.4 Backtest / replay (unchanged intent)

Same engine, same risk/fill/position/cash/fee semantics as live — this parity is the
design's core promise and must not be broken by the restructure.

---

## 5. Integration boundaries

| Boundary | Seam | Rule |
|---|---|---|
| Venue | `BrokerAdapter` | venue truth; adapters own transport, never policy |
| Lake | `ParquetMarketProvider` | the only reader; all history routes through it |
| Execution mode | `FillSource` | Paper / Simulated / Broker / Replay, swappable |
| HTTP | FastAPI / WS routes | transport mapping only; no risk, no persistence, no policy |
| CLI | `tradex_interfaces.cli:main` | the `tradex` console script lives here only |

Remove the one dynamic `import_module` in `chart.py`.

---

## 6. Sequenced plan (safety outranks structure)

```
TRACK 1 — Make it safe and observable  (blocks everything)
  T1.1  Fix the 7 failing tests; make `pytest` the CI gate that can actually fail
  T1.2  Remove `continue-on-error` from the mypy step; move pytest before the halt
  T1.3  Dual position writer → one (MarkToMarket through PositionAccountant)
  T1.4  Auth on mutating HTTP ops; loopback-only live bind
  T1.5  ShutdownCoordinator reachable; SIGTERM teardown works
  T1.6  Recon golden from a recorded broker book

TRACK 2 — Complete the reactive core
  T2.1  Publish typed `Candle` on the bus in live and paper
  T2.2  One BarAggregator; remove the per-client duplicate in stream.py
  T2.3  Backtest/replay emit the same events as live (parity)

TRACK 3 — Earn the structure
  T3.1  Introduce the three ports in `domain`; make consumers depend on them
  T3.2  Make the 5 dependency rules mechanically enforced (AST gate reads import_module)
  T3.3  Absorb `TradingSession` into `runtime`; delete the 154 shims; delete `trading/src`
  T3.4  Delete persistence / operations / application; salvage research `pit.py`
  T3.5  Narrow `interfaces` to domain + runtime
  T3.6  Regenerate CLAUDE.md / README for the 12-package reality

TRACK 4 — Then, optionally
  T4.1  Production authentication (the deferred design in specs/)
  T4.2  Research boundary + versioned manifests
```

**Why this order.** Tracks 1–2 are behavioural: a strategy that can't run on bars and a
position book with two writers are *live-money* risks. Tracks 3–4 are structural: they
make the codebase easier to change, and a codebase you don't trust yet is not worth
restructuring. The predeploy plan put the extraction first; this proposal reverses that,
because the extraction is now ~97% done anyway — the remaining work is mechanical — while
the safety work is not done at all.

---

## 7. What is explicitly *not* changing

- `domain` stays pure and dependency-free.
- CQRS holds: strategies emit signals and never touch a broker.
- One OMS spine: `apply_fill` remains the only money authority.
- Parquet is the store; there is no database to introduce.
- The `ReactiveBus` remains the cross-component channel.
- Backtest, paper, and live keep identical risk/fill/fee semantics.
