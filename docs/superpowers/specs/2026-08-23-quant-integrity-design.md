# Design — Quant Integrity Repair (C1-C3, H1-H7, M1-M8)

- **Date:** 2026-08-23
- **Status:** Approved
- **Origin:** Expert quant review rounds 1-2 of `v4/` (domain, brokers, trading — execution, replay, analytics, runtime, market_feed)
- **Method:** Superpowers brainstorming → this spec → writing-plans → sliced implementation
- **Lens:** Ponytail full (lazy-minimal). Short diffs, reuse existing helpers, delete where possible. No new abstraction unless a finding forces a seam.

---

## 1. Goal & Success Criteria

Close every second-pass finding with the smallest code that makes quant results match reality. No behavior change beyond correctness.

**Done means:**

1. Live `Fill.price` equals the broker's average traded price (not the reference price) — `sdk/live_fill_bridge.py:136-164` C1 closed.
2. Walk-forward report is statistically valid: `_aggregate` stitches one OOS equity curve and computes return/Sharpe/MaxDD on it, and `sharpe_frequency` no longer inflates D1 backtests 19× — C2+C3 closed.
3. Fee math matches broker contract notes to the paisa: GST base includes SEBI where regulations say it does, per-order brokerage cap envelopes partials, and delivery STT docs reflect post-Oct-2024 both-sides rule — H1+H2 closed.
4. OMS integrity: fill-after-cancel no longer leaves order=CANCELLED + position+delta in opposite directions; same-instrument concurrent fills are atomic; overfills clamp — H3-H5 closed.
5. Runtime safety: stale feed emits typed `StaleFeed`; limit orders cannot fill through their limit — H6-H7 closed.
6. Medium findings M1-M8 fixed or explicitly documented as wont-fix with a `ponytail:` note + benchmark that would trigger a fix.
7. Full suite green after every wave: `python -m pytest domain/tests brokers/tests trading/tests -q` + `python -m compileall -q domain/src brokers/src trading/src` + `ruff check domain/src brokers/src trading/src`.

---

## 2. Non-Goals (ponytail cuts)

- No new package, DI container, or framework.
- No order-book simulator to replace `replay/synthetic_ticks.py` reference-grade ticks.
- No market-data quality engine (anomaly detection, corporate-action auto-ingest).
- No fee engine rewrite for derivatives/FX/commodities beyond documenting current equity-intraday scope.
- No migration of `CashLedger` to Decimal-only reporting beyond H2's per-order envelope (float report docs carry caveat).

---

## 3. Architecture — Three Waves

One commit per slice inside a wave; waves are sequential, slices loose within a wave.

```
W1 critical  C1, C2, C3         (each a one-to-two-file fix, independent)
W2 high      H1, H2, H3, H4, H5, H6, H7  (H3 needs H4's lock; keep together)
W3 medium    M1, M2, M3, M4, M5, M6, M7, M8 (doc/lock/carry fixes, independent)
```

**W1 — Critical (quant correctness)**

| Slice | Finding | Files |
|---|---|---|
| C1 | Live fill price = traded avg | `trading/src/tradex_trading/sdk/live_fill_bridge.py:136-164`, `brokers/src/tradex_brokers/dhan/_orders.py` + `upstox/_orders.py` (row shape) |
| C2 | WF stitched OOS curve | `trading/src/tradex_trading/replay/walk_forward.py:302-317` + `analytics/reports.py` reuse |
| C3 | Sharpe frequency inference | `trading/src/tradex_trading/replay/backtest.py:114,418`, `domain/timeframe.py` |

**W2 — High (OMS & cost integrity)**

| Slice | Finding | Files |
|---|---|---|
| H1 | Fee GST base + SEBI doc + STT/stamp note | `trading/src/tradex_trading/execution/fees.py:123-210` |
| H2 | Brokerage cap per-order envelope | `trading/src/tradex_trading/execution/fees.py` + new `execution/fee_envelope.py` (only if needed — ponytail: try tracking in ExecutionEngine first) |
| H3 | Fill-after-cancel FSM | `domain/src/tradex_domain/execution.py:39`, `trading/src/tradex_trading/execution/order_manager.py:58-60`, `trading/src/tradex_trading/execution/engine.py:640-652` |
| H4 | Position RMW per-instrument lock | `trading/src/tradex_trading/execution/position_manager.py:44-47`, `trading/src/tradex_trading/execution/trading_cache.py` reuse |
| H5 | Overfill clamp | `trading/src/tradex_trading/execution/order_manager.py:44-51` |
| H6 | Feed staleness | `trading/src/tradex_trading/runtime/market_feed.py:280-286`, `tradex_domain/events.py` (StaleFeed) |
| H7 | Limit-through guard | `trading/src/tradex_trading/execution/fill_model.py:29-58` |

**W3 — Medium (docs, locks, carry)**

| Slice | Finding | Files |
|---|---|---|
| M1 | BONUS in adjust_series | `trading/src/tradex_trading/datalake/corporate_actions.py:129` |
| M2 | WF purge/warmup | `trading/src/tradex_trading/replay/walk_forward.py:132-148` |
| M3 | Recording-only bridge note | `trading/src/tradex_trading/replay/backtest.py:327-344` docs |
| M4 | FeedRegistry lock | `trading/src/tradex_trading/runtime/market_feed.py:300-389` |
| M5 | MarketFeed atomic subscribe | `trading/src/tradex_trading/runtime/market_feed.py:188-202` |
| M6 | Trade-book cache | `trading/src/tradex_trading/sdk/live_fill_bridge.py:81-104` |
| M7 | Missing-candle MTM note | `trading/src/tradex_trading/replay/backtest.py:508-514` |
| M8 | Long-only flag docs | `trading/src/tradex_trading/strategy/extensions/strategies/*.py` |

---

## 4. Detailed Design

### C1 — Live fills use traded average, not reference price

**Root cause:** `LiveFillBridge._on_order` (line 161) builds `Fill(price=order.price)` — the order's limit/reference price stamped by `ReactiveStrategyEngine` for MARKET orders. Broker stream row's own `averagePrice`/`avgTradedPrice` field is ignored.

**Fix (ponytail: higher rung):** In `_on_order`, read `order.average_price` / `order.avg_price` / broker row field directly (Dhan `avgPrice`, Upstox `averagePrice`). Pseudocode:

```python
traded = getattr(order, "average_price", None) or getattr(order, "avg_traded_price", None)
price = traded if traded is not None and traded.value > 0 else order.price
```

Fallback to `order.price` preserves paper/no-field behavior. If adapters do not surface the field yet, expose it from their order-mapping (`dhan/_orders.py:map_order`, `upstox/_orders.py`) as `avg_price: Price | None` on `domain.execution.Order` (add optional field, no migration). When `position_projection_owned` is True the bridge is bypassed — keep existing path.

**Test:** Feed a stream row with `filled_qty=5, averagePrice=100.5` where `order.price=100` (reference). Assert published `Fill.price=100.5` and `Position.avg_price` reflects 100.5.

### C2 — Walk-forward stitched OOS curve

**Fix:** Replace `_aggregate`'s mean-of-returns with true stitching. For each step, take `out_of_sample_result.equity_curve` (already Decimal→float, but keep float for report parity), normalize each segment by its first value, chain multiplicatively:

```python
stitched = [1.0]
for seg in segments:  # each seg is oos equity_curve
    base = seg[0] or 1.0
    norm = [x/base for x in seg[1:]]  # skip duplicate junction
    stitched.extend(v*stitched[-1] for v in norm)
aggregate_oos_return = (stitched[-1]-1.0)
aggregate_oos_sharpe = sharpe_ratio(stitched_returns, frequency=oos_frequency)
aggregate_oos_maxdd = max_drawdown(stitched)
```

Add `aggregate_oos_maxdd` to `WalkForwardReport`. Keep `aggregate_oos_return/sharpe` names for compat.

### C3 — Sharpe frequency inference

**Fix:** In `BacktestEngine.run`, if `sharpe_frequency` is still default `"1m"` but `data[0]` carries `.timeframe`, infer: `Timeframe.D1→"daily"`, `M1→"1m"`, etc. If inference conflicts with explicit caller value, explicit wins. If no timeframe available, raise `ValueError("sharpe_frequency required for non-M1 data — pass daily/1m/5m")` rather than silently inflating.

### H1 — Fee parity docs + code

- Document SEBI default as statutory ₹20/crore (`0.0002%`) and keep `_SEBI_RATE` canonical; constructor docstring says custom `sebi_charge_pct` is "percent, not fraction" and example shows legacy `0.0001` → `0.0001%` = ₹10/crore (clarify not statutory). Do not change numeric defaults beyond docs — preserve backward compat.
- Fix GST base: canonical includes SEBI; legacy docstring drops "structurally identical" claim and notes base difference; add comment that true NSE GST base is (brokerage+exchange+SEBI) — our legacy path is intentionally divergent for custom rates and flagged for removal.
- Add module-level note: delivery STT both-sides since Oct 2024, our `equity_delivery` models pre-Oct-2024 sell-only (conservative). Stamp duty note: equity stamp buy-side only, we charge both sides (conservative).

### H2 — Per-order brokerage cap

**Ponytail-first try:** Without a new file, track `OrderId → Decimal brokerage_accrued` in `ExecutionEngine._brokerage_accrued` and cap at `cap - accrued` per `on_fee` call. Only if that pollutes engine do we extract `execution/fee_envelope.py: OrderFeeEnvelope(can_allocate(fill) -> Money)`.

### H3 — Fill-after-cancel

Allow `CANCELLED → FILLED` (and `→ PARTIALLY_FILLED`) in `_LEGAL_TRANSITIONS` (domain) or make `PositionManager.on_fill` succeed while `OrderManager.on_order_filled` uses `apply_unknown`-like force-write for this single edge. Prefer FSM allow-list — one line, every caller benefits, guards the exception that currently leaves order=CANCELLED + position drift.

### H4 — Position RMW lock

Add `self._instrument_locks: dict[str, threading.RLock]` in `PositionManager`; `on_fill` wraps `get→apply→update` under `with self._instrument_lock(instrument.instrument_id):`. Reuse `TradingCache` read-write lock pattern (no new lock primitive). # ponytail: per-instrument locks, single global if contention never shows.

### H5 — Overfill clamp

In `OrderManager.on_order_filled`, compute `remaining = order.quantity.value - order.filled_quantity.value`; `delta = min(fill.quantity.value, remaining)`; if `delta < fill.quantity.value` log warning and emit `DriftItem` via reconciler. Position books clamped delta.

### H6 — Feed staleness

New `domain/events.py: StaleFeed(instrument, age_seconds, last_timestamp)` event. `MarketFeed._on_quote` stamps `self._last_tick[inst]=now`; a `threading.Timer` (or existing metrics tick) checks max age > `stale_after = max(2 * bar_freq, 30s)` and publishes `StaleFeed`. Strategies can subscribe; execution engine can optionally increment `feed.stale` counter.

### H7 — Limit-through guard

In `FillModel.resolve_fill_price`, after slippage: if `request.order_type == LIMIT` and slippage moved price through the limit (`BUY: slipped > limit`, `SELL: slipped < limit`), clamp to limit (`# ponytail: limit is hard, slippage cannot worsen beyond it`). Keep MARKET path unchanged.

### M1-M8

- M1: `adjust_series` fetches both SPLIT and BONUS (`get_typed(..., action_type=None)` filter or two calls, multiply both).
- M2: Add `purge_bars=5` embargo between train/test in `run_walk_forward` — skip first N test bars from OOS scoring; document warmup carry via existing `FakeClock` + position seeding hook (leave as doc if code change too invasive: `ponytail: carry warmup by seeding strategy._closes from train tail when test_window < slow_period`).
- M3: Docstring in `backtest.py:327-344` clarifies recording-only vs returning fill timing.
- M4: `FeedRegistry.subscribe/unsubscribe` wrap count mutation in `threading.Lock`.
- M5: `MarketFeed.subscribe` adds to wanted set only after broker call succeeds (try/except with rollback).
- M6: `TradeBookFillIdResolver` caches last `trade_book()` result for 2s TTL (single `time.monotonic()` check).
- M7: Note in `backtest.py:508-514` that instruments without candles contribute 0 MTM (intentional, document).
- M8: Strategy doc notes long-only gate is caller responsibility; add `allow_short` flag doc.

---

## 5. Interfaces

New/changed signatures (all additions are optional/backward-compatible):

```python
# domain/execution.py
@dataclass(frozen=True, slots=True)
class Order:
    avg_price: Price | None = None  # broker average traded price, when reported
    # ... existing fields unchanged

# domain/events.py
@dataclass(frozen=True, slots=True)
class StaleFeed:
    instrument: Instrument
    age_seconds: float
    last_timestamp: datetime | None

# trading/execution/position_manager.py
class PositionManager:
    def on_fill(self, fill: Fill) -> Position: ...  # now per-instrument locked
```

No other public API change.

---

## 6. Testing Strategy

- **Unit (existing suites):** `domain/tests/test_order_transitions.py` extended for CANCELLED→FILLED, `trading/tests/execution/test_order_manager.py` overfill, `trading/tests/replay/test_walk_forward.py` stitched curve, `trading/tests/replay/test_backtest_real_pnl.py` Sharpe inference, `trading/tests/execution/test_fees_validation.py` GST-base note, `trading/tests/runtime/test_market_feed.py` StaleFeed.
- **Parity:** `tests/parity/test_execution_cost_parity.py` extended to assert per-order cap; `tests/parity/test_live_tape_parity.py` asserts traded price.
- **Integration:** `full_stack` smoke still single-command.

---

## 7. Rollout & Verification

Each wave:
```bash
python -m compileall -q domain/src brokers/src trading/src
ruff check domain/src brokers/src trading/src
python -m pytest domain/tests brokers/tests trading/tests -q
```

Per-slice gate before commit. Final gate: `grep -rn 'ponytail:' trading/src/brokers/src/domain/src` reviewed for ceilings noted.

---

## 8. Risks

- Adding `Order.avg_price` touches mappers (`dhan/_orders.py`, `upstox/_orders.py`) — keep field optional, no migration.
- Walk-forward new fields (`aggregate_oos_maxdd`) are additive; old code reading report ignores them.
- Per-instrument lock is heavier than single global but lower contention — fallback to global if profiling shows no win: `# ponytail: global lock if per-instrument proves unnecessary`.
