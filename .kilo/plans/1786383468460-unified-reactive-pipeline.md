# Unified Reactive Execution Pipeline (Backtest == Live)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the two execution code paths into one. Today `BacktestEngine.run()` carries its own private `cash`/`positions`/`equity_curve` loop (backtest.py:179-567); live/replay/paper already share the reactive pipeline (`ReactiveStrategyEngine` → bus `PlaceOrderCommand` → `ExecutionEngine` → `PositionManager` → `FeeCalculator`/`RiskManager`). Make backtest drive the **same** `ExecutionEngine` + `PositionManager` that live uses, so fills/risk/fees/positions/corporate-actions are computed by one code path in every mode. Keep the system reactive/event-driven. Preserve the public `BacktestResult` return type.

**Architecture:** Backtest becomes a *bus driver*, not a calculator. `run()` composes a `ReactiveBus` + `ReactiveStrategyEngine` (next_open) + `ExecutionEngine` (with `SimulatedFillSource` + `PositionManager` + shared `RiskManager`/`FeeCalculator`/`SlippageModel`) + a new `CashLedger`, publishes historical `Candle`/`Quote`/`Fill` events onto the bus (exactly as `ReplayEngine.replay(bus)` already does), then derives `BacktestResult` from `PositionManager`/`CashLedger` state. Corporate actions are applied by `BacktestEngine` calling `PositionManager.on_corporate_action()` at the correct ex-date as it streams candles (orchestrated model — user-confirmed; no new domain events).

**Tech Stack:** Python 3.11+, RxPY (existing `ReactiveBus`), stdlib `decimal`. No new dependencies.

## Current state (verified)
- `ReactiveStrategyEngine` (strategy/core/engine.py) subscribes to `Candle`/`Quote`/`Depth`/`Fill`; in `next_open` mode defers signals and publishes `PlaceOrderCommand` priced at the next candle's OPEN (`_flush_pending`, engine.py:108). This is the fill-price source for backtest parity.
- `ExecutionEngine` consumes `PlaceOrderCommand` (engine.py:352) → risk → `FillSource.submit` → `PositionManager.on_fill` → `FeeCalculator` → `OrderFilled` on bus. Shares `RiskManager`, `FeeCalculator`, `SlippageModel` with backtest already.
- `PositionManager` (position_manager.py) already has `on_fill`, `on_fee`, `on_corporate_action` (splits/dividends via `position_math`) — the live-side accounting backtest must now reuse.
- `ReplayEngine.replay(bus)` (engine.py:130) already publishes events to a bus and is the proven template for "historical data → bus → pipeline".
- `BacktestEngine.run()` is the ONLY non-unified path: it collects signals, then loops privately doing risk/slippage/CA/fees/position math and building its own `equity_curve`.
- `BacktestResult` (fields: `total_return`, `sharpe`, `max_drawdown`, `num_trades`, `trades`, `total_fees`, `equity_curve`, `num_rejected`) is consumed by `optimization.py`, `walk_forward.py`, and 7 parity test files. **Must remain unchanged.**

## Global Constraints
- Domain layer stays pure (no imports from `trading`/`brokers`) — existing `tests/test_import_boundaries.py` enforces this. New `CashLedger` lives in `trading/.../execution/` (it is infrastructure, not domain).
- `BacktestResult` public fields/semantics unchanged. Callers (`optimization`, `walk_forward`, parity tests) must keep passing unmodified.
- Determinism preserved: backtest stays a pure function of (data, config, strategy). Single-threaded bus replay (as `ReplayEngine` already does) is deterministic; `ReactiveStrategyEngine` correlation ids are seeded per-instance (engine.py:85) so ids reproduce.
- Parity tests in `trading/tests/parity/` are the acceptance gate and must keep passing (exact fill-price + realized-PnL equality across modes).

---

### Task 1: Add `CashLedger` (backtest cash account)

**Why:** Live `PositionManager` tracks positions+PnL but not account cash. Backtest needs cash for `equity_curve` and fee debits. Orchestrated model: a small ledger fed by `OrderFilled` events.

**Files:**
- Create: `trading/src/tradex_trading/execution/cash_ledger.py`
- Test: `trading/tests/execution/test_cash_ledger.py`

**Interfaces:**
- Consumes: `Fill` events (subscribe on the bus), `Money`/`Price`/`Quantity` from domain
- Produces: `cash: Decimal`, `total_fees: Decimal`, `realized_pnl: Decimal`, `equity(curve_point: Decimal) -> Decimal`

- [ ] **Step 1: Write failing test**

```python
def test_buy_debits_cash_and_sell_credits():
    from tradex_domain import Fill, OrderSide, OrderType
    from tradex_domain.execution import OrderRequest
    from tradex_domain.value_objects import OrderId, Price, Quantity
    from tradex_trading.execution.cash_ledger import CashLedger
    led = CashLedger(Decimal("100000"))
    led.on_fill(Fill(order_id=OrderId("1"), instrument=None, side=OrderSide.BUY,
                    quantity=Quantity(1), price=Price(10), timestamp=None))
    assert led.cash == Decimal("99990")
    led.on_fee(Decimal("1"))
    assert led.cash == Decimal("99989")
    led.on_fill(Fill(order_id=OrderId("2"), instrument=None, side=OrderSide.SELL,
                    quantity=Quantity(1), price=Price(12), timestamp=None))
    assert led.cash == Decimal("100001")  # +12, no fee
```

- [ ] **Step 2: Run, expect FAIL** (`cash_ledger` import error)

- [ ] **Step 3: Implement**

```python
"""Backtest cash ledger — fed by OrderFilled events (orchestrated model).

Pure Decimal accounting. BUY debits (price*qty + fee), SELL credits
(price*qty - fee). `equity(mark)` = cash + sum of position marks supplied by
the caller (BacktestEngine snapshots PositionManager at each bar).
"""
from __future__ import annotations
from decimal import Decimal

from tradex_domain.value_objects import Money, Price, Quantity


class CashLedger:
    def __init__(self, initial: Decimal | float | str = Decimal("100000")) -> None:
        self._cash = Decimal(str(initial))
        self._fees = Decimal("0")

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def total_fees(self) -> Decimal:
        return self._fees

    def on_fill(self, side: object, quantity: Quantity, price: Price) -> None:
        notional = quantity.value * price.value
        if side.value == "BUY":
            self._cash -= notional
        else:
            self._cash += notional

    def on_fee(self, fee: Decimal | Money) -> None:
        amount = fee.amount if isinstance(fee, Money) else Decimal(str(fee))
        self._cash -= amount
        self._fees += amount

    def equity(self, marked_positions: Decimal) -> Decimal:
        return self._cash + marked_positions
```

- [ ] **Step 4: Run test, expect PASS**

- [ ] **Step 5: Commit** `cash_ledger.py` + test.

---

### Task 2: Refactor `BacktestEngine.run()` to drive the unified pipeline

**Why:** Replace the private loop with bus + `ReactiveStrategyEngine` + `ExecutionEngine` + `PositionManager` + `CashLedger`. This is the core unification.

**Files:**
- Modify: `trading/src/tradex_trading/replay/backtest.py` (`run()` body + `__init__` wiring; keep `BacktestResult` fields/semantics)
- Test: extend `trading/tests/parity/test_golden_mode_parity.py` + `test_corporate_action_parity.py` (add assertions that backtest now routes through `ExecutionEngine` — e.g. spy on `PositionManager.on_fill` call count == num_trades)

**Interfaces:**
- Reuses: `ReactiveBus`, `ReactiveStrategyEngine`, `ExecutionEngine`, `SimulatedFillSource`, `PositionManager`, `TradingCache`, `CashLedger`, `CorporateActionStore`, `RiskManager`, `FeeCalculator`, `SlippageModel`
- Produces: `BacktestResult` (unchanged shape)

- [ ] **Step 1: Write characterization test (must stay green after refactor)**

```python
def test_backtest_routes_through_execution_engine():
    from tradex_trading.execution.position_manager import PositionManager
    from tradex_trading.replay.backtest import BacktestEngine
    spy_hits = []
    real = PositionManager.on_fill
    PositionManager.on_fill = lambda self, f: (spy_hits.append(1) or real(self, f))
    try:
        res = BacktestEngine().run(_TimedBuySell(INSTRUMENT), _chained_candles())
        assert res.num_trades == 2
        assert len(spy_hits) == 2  # fills went through PositionManager, not a private loop
    finally:
        PositionManager.on_fill = real
```

- [ ] **Step 2: Run, expect FAIL** (current impl never calls `PositionManager.on_fill`)

- [ ] **Step 3: Rewrite `run()` to orchestrate the pipeline**

```python
def run(self, strategy, data):
    from tradex_domain import Candle, Fill, Quote
    from tradex_trading.reactive.bus import ReactiveBus
    from tradex_trading.strategy.core.engine import ReactiveStrategyEngine
    from tradex_trading.execution.engine import ExecutionEngine
    from tradex_trading.execution.fill_sources import SimulatedFillSource
    from tradex_trading.execution.trading_cache import TradingCache
    from tradex_trading.execution.position_manager import PositionManager
    from tradex_trading.execution.cash_ledger import CashLedger

    bus = ReactiveBus()
    cache = TradingCache()
    position_manager = PositionManager(cache)
    fill_source = self._fill_source or SimulatedFillSource(cache=cache)
    engine = ExecutionEngine(
        bus, fill_source, risk_manager=self._risk_manager,
        cache=cache, fee_calculator=self._fee_calculator,
    )
    ledger = CashLedger(self._initial_capital)
    strategy_engine = ReactiveStrategyEngine(bus, fill_reference="next_open")
    strategy_engine.register(strategy)

    # Cash ledger subscribes to fills (orchestrated cash tracking).
    bus.of_type(Fill).subscribe(lambda f: (
        ledger.on_fill(f.fill.side, f.fill.quantity, f.fill.price),
        ledger.on_fee(engine._fee_calculator.calculate(f.fill).amount)
        if engine._fee_calculator is not None else None,
    ))

    # Risk manager binds to the SAME positions the engine fills (parity).
    if self._risk_manager is not None:
        if not getattr(self._risk_manager, "positions_provider_bound", False):
            self._risk_manager.set_positions_provider(
                lambda: list(cache.all_positions()))
        self._risk_manager.reset_rate_window()

    # Corporate actions: apply at ex-date as we stream (orchestrated).
    if self._corporate_actions is not None:
        self._wire_corporate_actions(bus, position_manager, strategy_engine, data)

    equity_curve = [float(self._initial_capital)]
    for event in data:
        if isinstance(event, Candle):
            bus.publish(event)  # strategy defers→next-open PlaceOrderCommand→engine
            # MTM snapshot after the bar: cash + Σ qty*close
            mark = Decimal("0")
            for pos in cache.all_positions():
                mark += pos.quantity.value * event.ohlc.close.value
            equity_curve.append(float(ledger.equity(mark)))
        elif isinstance(event, Quote):
            bus.publish(event)
        elif isinstance(event, Fill):
            bus.publish(event)

    engine.shutdown()
    strategy_engine.dispose_all()

    # Build BacktestResult from pipeline state (unchanged fields).
    positions = cache.all_positions()
    realized = sum((p.realized_pnl.amount for p in positions), Decimal("0"))
    # total_return / sharpe / max_drawdown via existing analytics helpers.
    ...
    return BacktestResult(
        total_return=..., sharpe=..., max_drawdown=...,
        num_trades=len([o for o in cache.all_orders() if o.status == FILLED]),
        trades=[...], total_fees=float(ledger.total_fees),
        equity_curve=equity_curve, num_rejected=engine._risk._rejected_count,
    )
```

Notes:
- `_wire_corporate_actions` iterates `self._corporate_actions.due_before(ts)` per candle and calls `position_manager.on_corporate_action(...)` — same store/method backtest already used, now applied to the shared `PositionManager`.
- `num_rejected` requires `RiskManager` to expose a rejected counter (add `self._rejected = 0` + increment in `check()` when it returns False; read-only property). This is a small additive field, backward-compatible.
- `trades` list (Signals) is already collected by `strategy.signals` — keep surfacing it.
- Keep `FakeClock`, `BacktestResult` dataclass, `_action_identity`, `_parse_ex_date` unchanged.

- [ ] **Step 4: Run characterization test, expect PASS**

- [ ] **Step 5: Run full parity suite, expect PASS** (exact fill-price + PnL equality must hold — the pipeline IS the same one replay/live use, so this is expected)

- [ ] **Step 6: Commit**

---

### Task 3: Add `RiskManager.rejected_count` (small additive field)

**Why:** `BacktestResult.num_rejected` currently comes from the private loop. The unified engine's risk decisions live in `RiskManager.check()`. Expose a counter so `run()` can read it without private-loop bookkeeping.

**Files:**
- Modify: `trading/src/tradex_trading/execution/engine.py` (`RiskManager.__init__` + `check`)
- Test: `trading/tests/execution/test_engine_risk_gaps.py` (extend)

- [ ] **Step 1: Add field + increment**

```python
def __init__(self, ...):
    ...
    self._rejected_count = 0

@property
def rejected_count(self) -> int:
    return self._rejected_count

def check(self, request, now=None):
    with self._lock:
        if not self._live_orders_enabled:
            self._rejected_count += 1
            return False
        ...  # existing gates; on any rejection path: self._rejected_count += 1; return False
        return True
```

- [ ] **Step 2: Test that a denied order increments `rejected_count`**

- [ ] **Step 3: Run + commit**

---

### Task 4: Wire corporate-action application into the unified backtest

**Why:** In the old loop, CAs were applied to the backtest's private `positions` dict before fill. Now they must apply to the shared `PositionManager` at the right ex-date during the candle stream.

**Files:**
- Modify: `trading/src/tradex_trading/replay/backtest.py` (`_wire_corporate_actions` helper, called from `run()`)
- Test: `trading/tests/parity/test_corporate_action_parity.py` (already asserts `result.equity_curve[-1]` and `pos.realized_pnl`; add assertion that `PositionManager.on_corporate_action` was invoked)

- [ ] **Step 1: Implement `_wire_corporate_actions`**

```python
def _wire_corporate_actions(self, bus, position_manager, strategy_engine, data):
    from tradex_domain import Candle
    from tradex_domain.value_objects import Price
    store = self._corporate_actions
    applied = set()
    def _apply_at(candle):
        ts = candle.timestamp
        for inst in store.instruments_due(ts):
            for action in store.actions_for(inst, ts):
                key = f"{inst.symbol}|{action.ex_date}|{action.action_type}|{action.amount}|{action.ratio}"
                if key in applied:
                    continue
                applied.add(key)
                position_manager.on_corporate_action(
                    action.instrument, action.action_type,
                    ratio=action.ratio, per_share=action.per_share)
    bus.of_type(Candle).subscribe(_apply_at)
```

(Adapt to the actual `CorporateActionStore` API — verify method names in `trading/src/tradex_trading/datalake/corporate_actions.py` before coding; the plan names are illustrative.)

- [ ] **Step 2: Run `test_corporate_action_parity.py`, expect PASS** (equity_curve values unchanged: split ladder still 96000, dividend still 102000)

- [ ] **Step 3: Commit**

---

## Unified Flow (target architecture)

```
historical data (Candle/Quote/Fill)
        │
        ▼  BacktestEngine.run()  ── publishes onto ──┐
        │                                            ▼
        │                                   ReactiveBus (single bus)
        │                                            │
        │                                   ReactiveStrategyEngine
        │                                   (next_open: defer → PlaceOrderCommand
        │                                    priced at next candle OPEN)
        │                                            │
        │                                            ▼
        │                                   ExecutionEngine  ◄─── SAME engine
        │                                   risk → FillSource → PositionManager   live uses
        │                                   → FeeCalculator → OrderFilled          (replay/
        │                                            │                            paper too)
        │                                            ▼
        │                                   CashLedger (on_fill/on_fee)
        │                                   PositionManager.on_corporate_action
        │                                            │
        └────────────── reads cache + ledger ───────┘
                        to build BacktestResult
```

One pipeline: `bus → strategy → ExecutionEngine → PositionManager → CashLedger`, used identically by backtest, replay, paper, live. Difference between modes is only the **FillSource** (`SimulatedFillSource` vs `BrokerFillSource`) and the **event source** (historical replay vs live WS).

## Validation (verification-before-completion)

Run, read output, THEN claim done:

1. `python -m pytest trading/tests/parity/ -q` → 0 failures (exact mode-parity gate).
2. `python -m pytest trading/tests/execution/ -q` → 0 failures (engine + cash ledger + risk counter).
3. `python -m pytest trading/src/tradex_trading/replay/ -q` → 0 failures (backtest + optimization + walk_forward still return `BacktestResult`).
4. `python -m pytest tests/test_import_boundaries.py -q` → pass (domain still pure; `CashLedger` is in `trading/`).
5. `python -m pytest domain/tests brokers/tests trading/tests --import-mode=importlib -q` → confirm no NEW failures vs the pre-change baseline (the 19 pre-existing session-lifecycle failures must remain unchanged in count).

## Risks / Open Questions
- **`CorporateActionStore` API:** exact method names (`instruments_due`/`actions_for`) are illustrative — verify against `datalake/corporate_actions.py` during Task 4. If the store only exposes a bulk `apply` against a positions dict, wrap it to call `PositionManager.on_corporate_action` per due action instead.
- **MTM mark price:** backtest marks open positions at each bar's `close`; live marks at quote LTP. Parity tests assert fill-price + realized-PnL equality, not equity-curve identity, so this divergence is acceptable and already tolerated today.
- **`num_rejected` counter** is additive to `RiskManager` — no behavior change for live (counter just increments; nothing reads it except backtest result).
- **`SimulatedFillSource` requires a positive `request.price`** — the strategy engine passes next-open price, so backtest fills identically to replay/live. Confirmed by existing `test_exact_fill_price_equals_backtest_next_open`.
- **Determinism:** bus replay is single-threaded; `ReactiveStrategyEngine` correlation ids are per-instance seeded (engine.py:85) → reproducible across runs. Same guarantee `ReplayEngine` already provides.

## Explicitly NOT doing
- No new domain events (`CorporateAction`/`MarkToMarket` on the bus) — orchestrated model chosen.
- No change to live/replay/paper paths (they already use the unified pipeline).
- No change to `BacktestResult` shape or `ReactiveStrategyEngine`/`ExecutionEngine` public contracts.
- No deletion of `ReplayEngine` (it remains the thin historical-event publisher; backtest now composes the same pieces).
