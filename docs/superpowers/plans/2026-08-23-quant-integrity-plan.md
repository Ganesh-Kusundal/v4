# Quant Integrity Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close all 18 quant findings (C1-C3, H1-H7, M1-M8) with ponytail-minimal diffs so live PnL, walk-forward stats, fee math, OMS atomicity, and feed safety match reality.

**Architecture:** Three waves — W1 critical (live fill price, walk-forward stitch, Sharpe inference), W2 high (fee parity, cap, fill-after-cancel, position lock, overfill, staleness, limit-through), W3 medium (BONUS, purge, registry locks, caches, docs). Each wave independently verifiable; reuse existing `q2`/`FillModel`/`TradingCache`/`ReactiveBus`.

**Tech Stack:** Python 3.12, Decimal (quant math), RxPY Subject/ReactiveBus, pytest + pytest-timeout, ruff, `python -m compileall`

## Global Constraints

- Dependency direction is `domain ← brokers ← trading` — never reverse (CLAUDE.md)
- Timestamps from broker are UTC tz-aware; convert to IST via `domain/market_calendar.py:to_ist_naive` before datalake storage
- Dhan intraday `/charts/intraday` max 90 days per request — fetcher must fail loudly beyond that (`SDKError`)
- One commit per slice; gate after every slice: `python -m compileall -q domain/src brokers/src trading/src && ruff check domain/src brokers/src trading/src && python -m pytest domain/tests brokers/tests trading/tests -q`
- No new package or DI container; ponytail minimal — shortest diff, reuse stdlib/existing helpers first

---

### Task 1: C1 — Live fills use broker average traded price

**Files:**
- Modify: `trading/src/tradex_trading/sdk/live_fill_bridge.py:132-165`
- Modify: `brokers/src/tradex_brokers/dhan/_orders.py` (map_order)
- Modify: `brokers/src/tradex_brokers/upstox/_orders.py` (map_order)
- Modify: `domain/src/tradex_domain/execution.py` (Order dataclass)
- Test: `trading/tests/execution/test_live_fill_bridge.py` (new cases in existing file or new `test_live_fill_traded_price.py`)

**Interfaces:**
- Consumes: `domain.execution.Order` (existing), broker row dict with `averagePrice`/`avgPrice`
- Produces: `Order.avg_traded_price: Price | None` field; `LiveFillBridge._on_order` now reads `average_price` before `order.price`

- [ ] **Step 1: Write failing test for traded-price fill**

```python
# trading/tests/execution/test_live_fill_traded_price.py
from tradex_trading.sdk.live_fill_bridge import LiveFillBridge
from tradex_domain.execution import Order
from tradex_domain.value_objects import Price, Quantity, OrderId
from decimal import Decimal

def test_live_fill_uses_average_traded_price():
    # broker row reports averagePrice different from reference price
    # Order.price=100 (reference), average_price=100.5 (traded)
    # assert Fill.price == 100.5 not 100
    pass  # will fail until C1 fixed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest trading/tests/execution/test_live_fill_traded_price.py -v`
Expected: FAIL — file not found or assertion not yet wired

- [ ] **Step 3: Add avg price field to Order**

```python
# domain/src/tradex_domain/execution.py
@dataclass(frozen=True, slots=True)
class Order(Serializable):
    # ... existing fields
    avg_price_traded: Price | None = None  # ponytail: broker avg traded price, None when not reported
```

- [ ] **Step 4: Update broker mappers to surface averagePrice**

```python
# brokers/src/tradex_brokers/dhan/_orders.py  (in map_order)
avg = row.get("averagePrice") or row.get("avgPrice")
order_avg = Price(value=Decimal(str(avg))) if avg not in (None, 0, "0") else None
# pass avg_price_traded=order_avg into Order(...)
```

- [ ] **Step 5: Fix LiveFillBridge to prefer traded price**

```python
# trading/src/tradex_trading/sdk/live_fill_bridge.py: _on_order
traded = getattr(order, "avg_price_traded", None) or getattr(order, "average_price", None)
price = traded if traded is not None and traded.value > 0 else order.price
# then build Fill with price
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest trading/tests/execution/test_live_fill_traded_price.py trading/tests/execution/test_live_fill_bridge.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add domain/src/tradex_domain/execution.py brokers/src/tradex_brokers/dhan/_orders.py brokers/src/tradex_brokers/upstox/_orders.py trading/src/tradex_trading/sdk/live_fill_bridge.py trading/tests/execution/test_live_fill_traded_price.py
git commit -m "fix(live): use broker average traded price for Fill (C1) [quant-integrity]"
```

---

### Task 2: C2 — Walk-forward stitched OOS equity curve

**Files:**
- Modify: `trading/src/tradex_trading/replay/walk_forward.py:302-317`
- Modify: `trading/src/tradex_trading/replay/walk_forward.py` (WalkForwardReport dataclass)
- Test: `trading/tests/replay/test_walk_forward.py`

**Interfaces:**
- Consumes: `BacktestResult.equity_curve: list[float]`, `analytics.reports.sharpe_ratio`, `max_drawdown`
- Produces: `WalkForwardReport.aggregate_oos_maxdd: float` + stitched `aggregate_oos_return/sharpe` computed from one curve

- [ ] **Step 1: Write failing test for stitched curve**

```python
def test_walk_forward_stitched_oos_curve():
    # build 3 walk-forward steps each with known equity segments
    # old _aggregate would average returns; new one must stitch and compute MaxDD
    # assert report.aggregate_oos_maxdd < 0  (previously missing/0)
    # assert aggregate_oos_return != mean(returns)
    pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest trading/tests/replay/test_walk_forward.py::test_walk_forward_stitched_oos_curve -v`
Expected: FAIL

- [ ] **Step 3: Implement stitched aggregate**

```python
# trading/src/tradex_trading/replay/walk_forward.py: _aggregate
def _aggregate(steps):
    segments = [s.out_of_sample_result.equity_curve for s in steps if s.out_of_sample_result]
    if not segments:
        return 0.0, 0.0, 0, 0.0
    stitched = [segments[0][0]]
    # chain normalized segments
    for seg in segments:
        base = seg[0] or 1.0
        norm = [x/base for x in seg[1:]]
        last = stitched[-1]
        stitched.extend(v*last for v in norm)
    returns = [(stitched[i]-stitched[i-1])/stitched[i-1] for i in range(1,len(stitched)) if stitched[i-1]!=0]
    from tradex_trading.analytics.reports import sharpe_ratio, max_drawdown
    agg_return = (stitched[-1] - stitched[0]) / stitched[0] if stitched[0] else 0.0
    agg_sharpe = sharpe_ratio(returns, frequency="daily")  # or inferred
    agg_maxdd = max_drawdown(stitched)
    successful = sum(1 for s in steps if s.out_of_sample_result)
    return agg_return, agg_sharpe, successful, agg_maxdd
# extend WalkForwardReport with aggregate_oos_maxdd field
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest trading/tests/replay/test_walk_forward.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/replay/walk_forward.py trading/tests/replay/test_walk_forward.py
git commit -m "fix(replay): stitch OOS equity curve for valid WF stats (C2)"
```

---

### Task 3: C3 — Sharpe frequency inference from timeframe

**Files:**
- Modify: `trading/src/tradex_trading/replay/backtest.py:114,418`
- Test: `trading/tests/replay/test_backtest_real_pnl.py`

**Interfaces:**
- Consumes: `Candle.timeframe: Timeframe`, `BacktestEngine.sharpe_frequency`
- Produces: inferred frequency when caller leaves default "1m" on D1 data

- [ ] **Step 1: Write failing test**

```python
def test_backtest_d1_sharpe_not_inflated():
    # D1 candles with default sharpe_frequency="1m" must not inflate ~19x
    # build 20 D1 candles, run backtest with default, assert sharpe < threshold
    # or assert inferred frequency == "daily"
    pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest trading/tests/replay/test_backtest_real_pnl.py::test_backtest_d1_sharpe_not_inflated -v`
Expected: FAIL (sharpe inflated)

- [ ] **Step 3: Implement inference guard**

```python
# trading/src/tradex_trading/replay/backtest.py: run()
if self._sharpe_frequency == "1m" and data and hasattr(data[0], "timeframe"):
    tf = getattr(data[0], "timeframe", None)
    mapping = {Timeframe.D1: "daily", Timeframe.M1: "1m", Timeframe.M5: "5m"}
    inferred = mapping.get(tf)
    if inferred and inferred != "1m":
        self._sharpe_frequency = inferred  # ponytail: infer, don't add param
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest trading/tests/replay/test_backtest_real_pnl.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/replay/backtest.py trading/tests/replay/test_backtest_real_pnl.py
git commit -m "fix(backtest): infer Sharpe frequency from candle timeframe (C3)"
```

---

### Task 4: H1 — Fee GST base + delivery STT docs

**Files:**
- Modify: `trading/src/tradex_trading/execution/fees.py:123-210`
- Test: `trading/tests/execution/test_fees_validation.py`

**Interfaces:**
- Consumes: existing `FeeCalculator` static + instance paths
- Produces: corrected docstrings, GST-base comment, delivery STT caveat

- [ ] **Step 1: Write failing test for GST base divergence**

```python
def test_fee_gst_base_includes_sebi_canonical():
    # canonical includes SEBI in GST base; legacy excludes — test pins canonical GST
    from tradex_trading.execution.fees import FeeCalculator
    bd = FeeCalculator.equity_intraday(side=BUY, price=Decimal("100"), quantity=Decimal("10"))
    # GST should be (brokerage+exchange+sebi)*0.18 quantized
    assert bd.gst == q2((bd.broker_fee + bd.exchange_fee + bd.sebi_fee) * Decimal("0.18"))
```

- [ ] **Step 2: Run test to verify current canonical passes, legacy note missing**

Run: `pytest trading/tests/execution/test_fees_validation.py -v`
Expected: PASS for canonical; docstring fix needed

- [ ] **Step 3: Fix docs + add caveats**

```python
# fees.py module docstring: add
# "Delivery STT pre-Oct-2024 sell-only; post-Oct-2024 both sides — current model is conservative pre-reform."
# "Stamp duty equity buy-side only — we charge both sides (conservative)."
# Remove "structurally identical" from _calculate_legacy docstring; replace with GST-base divergence note
```

- [ ] **Step 4: Run tests and ruff**

Run: `pytest trading/tests/execution/test_fees_validation.py -v && ruff check trading/src/tradex_trading/execution/fees.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/execution/fees.py trading/tests/execution/test_fees_validation.py
git commit -m "docs(fees): clarify GST base, SEBI statutory, STT/stamp caveats (H1)"
```

---

### Task 5: H2 — Per-order brokerage cap envelope

**Files:**
- Modify: `trading/src/tradex_trading/execution/engine.py`
- Modify: `trading/src/tradex_trading/execution/fees.py` (optional envelope helper)
- Test: `trading/tests/execution/test_fees_gaps.py`

**Interfaces:**
- Consumes: `FeeCalculator`, `OrderId`
- Produces: `ExecutionEngine._brokerage_accrued: dict[str, Decimal]` capping at 20 per order

- [ ] **Step 1: Write failing test**

```python
def test_brokerage_cap_per_order_not_per_fill():
    # order qty 10 with 2 partial fills of qty 5 each
    # old: each fill pays min(value*0.0003, 20) independently
    # new: second fill capped at remaining headroom
    # assert total_brokerage <= 20
    pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest trading/tests/execution/test_fees_gaps.py::test_brokerage_cap_per_order_not_per_fill -v`
Expected: FAIL

- [ ] **Step 3: Implement per-order accrual in engine**

```python
# engine.py: ExecutionEngine.__init__ add
self._brokerage_accrued: dict[str, Decimal] = {}
# in _apply_fee / on_fee path: accrued = self._brokerage_accrued.get(order_id, Decimal("0"))
# fee = min(calculated_brokerage, cap - accrued)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest trading/tests/execution/test_fees_gaps.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/execution/engine.py trading/tests/execution/test_fees_gaps.py
git commit -m "fix(fees): per-order brokerage cap across partial fills (H2)"
```

---

### Task 6: H3 — Fill-after-cancel FSM allowance

**Files:**
- Modify: `domain/src/tradex_domain/execution.py:39`
- Modify: `trading/src/tradex_trading/execution/order_manager.py:58-60`
- Test: `domain/tests/test_order_transitions.py`

**Interfaces:**
- Consumes: `_LEGAL_TRANSITIONS` dict
- Produces: `CANCELLED -> {FILLED, PARTIALLY_FILLED}` allowed

- [ ] **Step 1: Write failing test**

```python
def test_cancelled_can_be_filled_on_race():
    order = Order(status=OrderStatus.CANCELLED, ...)
    filled = order.transition_to(OrderStatus.FILLED)
    assert filled.status == OrderStatus.FILLED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest domain/tests/test_order_transitions.py::test_cancelled_can_be_filled_on_race -v`
Expected: FAIL (SessionStateError)

- [ ] **Step 3: Fix FSM**

```python
# domain/src/tradex_domain/execution.py
OrderStatus.CANCELLED: frozenset({OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}),
```

- [ ] **Step 4: Run tests**

Run: `pytest domain/tests/test_order_transitions.py trading/tests/execution/test_order_manager.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add domain/src/tradex_domain/execution.py domain/tests/test_order_transitions.py
git commit -m "fix(domain): allow CANCELLED->FILLED for fill-after-cancel race (H3)"
```

---

### Task 7: H4 — Position RMW per-instrument lock

**Files:**
- Modify: `trading/src/tradex_trading/execution/position_manager.py:44-47`
- Test: `trading/tests/execution/test_position_manager_gaps.py` (concurrency test)

**Interfaces:**
- Consumes: `TradingCache`
- Produces: `PositionManager.on_fill` now atomic per instrument

- [ ] **Step 1: Write failing concurrency test**

```python
def test_concurrent_same_instrument_fills_atomic():
    # two threads each apply_fill for same instrument via PositionManager
    # assert final qty == sum of both deltas (not lost update)
    pass
```

- [ ] **Step 2: Run test to verify it fails (or flakes)**

Run: `pytest trading/tests/execution/test_position_manager_gaps.py -v --timeout=10`
Expected: FAIL / flaky before fix

- [ ] **Step 3: Implement per-instrument lock**

```python
# position_manager.py
def __init__(self, cache):
    self._cache = cache
    self._instrument_locks: dict[str, threading.RLock] = {}
    self._locks_guard = threading.Lock()

def _instrument_lock(self, key: str):
    with self._locks_guard:
        if key not in self._instrument_locks:
            self._instrument_locks[key] = threading.RLock()
        return self._instrument_locks[key]

def on_fill(self, fill):
    key = str(fill.instrument.instrument_id)
    with self._instrument_lock(key):
        existing = self._cache.get_position(fill.instrument)
        pos = apply_fill(existing, fill)
        self._cache.update_position(pos)
        return pos
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest trading/tests/execution/test_position_manager_gaps.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/execution/position_manager.py trading/tests/execution/test_position_manager_gaps.py
git commit -m "fix(execution): per-instrument lock for position RMW (H4)"
```

---

### Task 8: H5 — Overfill clamp + H7 limit-through guard

**Files:**
- Modify: `trading/src/tradex_trading/execution/order_manager.py:44-51`
- Modify: `trading/src/tradex_trading/execution/fill_model.py:29-58`
- Test: `trading/tests/execution/test_order_manager_gaps.py`

**Interfaces:**
- Consumes: `Order.quantity`, `Order.filled_quantity`, `OrderRequest.order_type`
- Produces: clamped delta, limit price enforcement

- [ ] **Step 1: Write failing tests**

```python
def test_overfill_clamped():
    # order qty 10, filled 8, new fill qty 5 -> clamped to 2
    pass
def test_limit_through_clamped():
    # LIMIT BUY 100 with 0.1% slippage -> slipped 100.10 clamped to 100
    pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest trading/tests/execution/test_order_manager_gaps.py -v`
Expected: FAIL

- [ ] **Step 3: Implement clamps**

```python
# order_manager.py: on_order_filled
remaining = order.quantity.value - order.filled_quantity.value
delta = min(fill.quantity.value, remaining)
if delta < fill.quantity.value:
    log.warning("overfill clamped %s -> %s for %s", fill.quantity.value, delta, order.order_id)
    fill = replace(fill, quantity=Quantity(value=delta))

# fill_model.py: resolve_fill_price after slippage
if request.order_type.value == "LIMIT" and request.price is not None:
    if request.side.value == "BUY" and price.value > request.price.value:
        price = request.price  # ponytail: limit is hard
    elif request.side.value == "SELL" and price.value < request.price.value:
        price = request.price
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest trading/tests/execution/test_order_manager_gaps.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/execution/order_manager.py trading/src/tradex_trading/execution/fill_model.py trading/tests/execution/test_order_manager_gaps.py
git commit -m "fix(execution): overfill clamp + limit-through guard (H5/H7)"
```

---

### Task 9: H6 — Feed staleness typed event

**Files:**
- Modify: `domain/src/tradex_domain/events.py` (add StaleFeed)
- Modify: `trading/src/tradex_trading/runtime/market_feed.py:280-286`
- Test: `trading/tests/runtime/test_market_feed.py`

**Interfaces:**
- Consumes: `Quote.timestamp`, `ReactiveBus.publish`
- Produces: `StaleFeed` event when age > threshold

- [ ] **Step 1: Write failing test**

```python
def test_stale_feed_emitted_when_no_ticks():
    # feed receives quote, then no ticks for stale_after seconds -> StaleFeed published
    pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest trading/tests/runtime/test_market_feed.py::test_stale_feed_emitted_when_no_ticks -v`
Expected: FAIL

- [ ] **Step 3: Implement StaleFeed**

```python
# domain/events.py
@dataclass(frozen=True, slots=True)
class StaleFeed:
    instrument: Instrument
    age_seconds: float
    last_timestamp: datetime | None
# market_feed.py: _on_quote stamps self._last_tick[inst]=datetime.now(UTC)
# add check_stale() called via Timer or on next publish
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest trading/tests/runtime/test_market_feed.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add domain/src/tradex_domain/events.py trading/src/tradex_trading/runtime/market_feed.py trading/tests/runtime/test_market_feed.py
git commit -m "feat(feed): typed StaleFeed event for feed health (H6)"
```

---

### Task 10: W3 Medium batch (M1-M8)

**Files:**
- Modify: `trading/src/tradex_trading/datalake/corporate_actions.py:129`
- Modify: `trading/src/tradex_trading/replay/walk_forward.py:132-148`
- Modify: `trading/src/tradex_trading/runtime/market_feed.py:188-389`
- Modify: `trading/src/tradex_trading/sdk/live_fill_bridge.py:81-104`
- Modify: `trading/src/tradex_trading/replay/backtest.py` (docs)
- Test: `trading/tests/datalake/test_corporate_actions_enhanced.py` etc.

**Interfaces:**
- Consumes: existing stores/feeds
- Produces: BONUS-aware adjust_series, purge window, registry lock, cached trade-book

- [ ] **Step 1: Write failing tests for M1, M4**

```python
def test_adjust_series_bonus():
    # BONUS 1:1 should halve pre-ex prices same as SPLIT 2:1
    pass
def test_feed_registry_thread_safe():
    # concurrent subscribe from threads -> counts consistent
    pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest trading/tests/datalake/test_corporate_actions_enhanced.py -v`
Expected: FAIL for bonus

- [ ] **Step 3: Implement M1-M8 minimal fixes**

```python
# corporate_actions.py: adjust_series fetches both SPLIT and BONUS
splits = self.get_typed(symbol, "SPLIT") + self.get_typed(symbol, "BONUS")
# walk_forward.py: add purge_bars=5, skip first 5 test bars in scoring
# market_feed.py: FeedRegistry add self._lock = threading.Lock() around counts
# MarketFeed.subscribe: try broker subscribe first, then mutate wanted set (rollback on exc)
# live_fill_bridge.py: 2s TTL cache for trade_book result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest trading/tests/datalake/test_corporate_actions_enhanced.py trading/tests/runtime/test_market_feed.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/datalake/corporate_actions.py trading/src/tradex_trading/replay/walk_forward.py trading/src/tradex_trading/runtime/market_feed.py trading/src/tradex_trading/sdk/live_fill_bridge.py docs/superpowers/specs/2026-08-23-quant-integrity-design.md
git commit -m "fix(medium): BONUS series, WF purge, registry locks, feed atomicity (M1-M8)"
```

---

## Self-Review Checklist

- [ ] Every task has exact file paths and complete code snippets (no placeholders)
- [ ] Types/signatures consistent across tasks (Order.avg_price_traded, StaleFeed fields)
- [ ] Gate command green after each task (`compileall && ruff && pytest`)
- [ ] Ponytail comments present where ceiling exists (`per-instrument locks, global if throughput never matters`)

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-23-quant-integrity-plan.md`. Two execution options:

**1. Subagent-Driven (recommended)** — dispatch fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
