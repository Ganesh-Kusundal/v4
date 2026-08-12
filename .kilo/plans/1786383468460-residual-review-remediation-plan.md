# Plan: Ponytail-filtered remediation of residual review findings (TradeXV4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the genuinely-real residual bugs from the prior review passes using the smallest correct diffs, with regression tests proving each fix. Skip everything already fixed or harmless.

**Architecture:** Domain layer (`tradex_domain`) is DDD-pure with no upward imports. `InstrumentId` is a frozen value object that currently carries **no asset-class discriminator** — its `equity/index/currency/commodity` classmethods all produce the identical bare `(exchange, underlying)` object. Provider keys are derived in `wire.py` via `_tag_from_id`, which hardcodes `"EQ"` for anything that is not FUT/OPT. The fixes below are confined to the domain layer (value object + wire tag derivation + one Instrument factory call) plus regression tests; no broker/trading layer changes are required because those branches key off `instrument.asset_class`, not the ID tag.

**Tech Stack:** Python 3.11+, pytest, stdlib `dataclasses`/`enum`. No new dependencies.

## Reconciliation — what the prior passes flagged vs. what the current code actually does

The deep-audit "Critical" list was read against a stale tree. Verified against current source:

| Prior claim | Current state | Action |
|---|---|---|
| `fill_sources.py:214` order id discarded | `BrokerFillSource.submit` captures `broker_order_id` (fill_sources.py:224-225) | DONE — skip |
| `fees.py:216` SELL fee sign | `total_cost` correctly negates: `-trade_value - fees` (fees.py:216) | DONE — skip |
| `engine.py` kill-switch TOCTOU / unbounded `_applied_fills` | kill-switch checked in both paths; `_applied_fills` dedup present | PARTIAL — see Task 3 |
| `instruments.py:75/137/151` equity id for index/currency/commodity | All factories produce identical bare id; `index()` exists and is equivalent | SEE Task 1 (cosmetic; real gap is wire tag) |
| `session.py:117/123` state machine | `start()` rejects non-NEW; `stop()` idempotent | DONE — skip |
| `streaming.py` `__all__` dup / super skip | No dup; skip documented & intentional | DONE — skip |
| Duplicated submit logic in `engine.py` | `_process_request_impl` vs `_submit_impl` duplicate ~80 lines | SEE Task 4 (ponytail: low priority, optional) |

**Net: Tasks 1, 2, 3 carry real correctness value; Task 4 (engine de-dup) included per user decision.**

## Global Constraints

- Domain layer must not import from `brokers` or `trading`. (enforced by `tests/test_import_boundaries.py`)
- `InstrumentId` is frozen + `slots=True`; preserve `__eq__`/`__hash__`/`__str__` stability so existing registered keys stay resolvable.
- `wire.py` key format is `f"{exchange}_{tag}|{underlying}{suffix}"` — changing the tag for non-equity instruments changes generated provider keys; must remain bidirectional with `reverse_instrument_key` and must not break `register_bulk` collision checks.
- Every fix ships with a regression test in the existing `domain/tests/` tree.

---

### Task 1: Make `InstrumentId` convey asset class (root-cause fix for the index/currency/commodity id bug)

**Why:** `instruments.py:75,137,151` call `InstrumentId.equity(...)` for `Index`/`Currency`/`Commodity`. Although `equity()` and `index()` are currently identical, the *intent* is wrong and the wire layer cannot distinguish asset classes for key derivation (see Task 2). The lazy fix is to give `InstrumentId` a real discriminator so the factories are no longer equivalent no-ops.

**Files:**
- Modify: `domain/src/tradex_domain/value_objects.py:21-85` (InstrumentId)
- Test: `domain/tests/test_instrument_id.py` (create)

**Interfaces:**
- Consumes: `AssetClass` from `tradex_domain.enums`
- Produces: `InstrumentId` with new `asset_class: AssetClass` field; updated `equity/index/currency/commodity` classmethods set it; `_tag_from_id` (wire.py) will read it.

- [ ] **Step 1: Write the failing test**

```python
from tradex_domain.value_objects import InstrumentId
from tradex_domain.enums import AssetClass

def test_factories_set_asset_class():
    assert InstrumentId.equity("NSE", "TCS").asset_class is AssetClass.EQUITY
    assert InstrumentId.index("NSE", "NIFTY").asset_class is AssetClass.INDEX
    assert InstrumentId.currency("NSE", "USDINR").asset_class is AssetClass.CURRENCY
    assert InstrumentId.commodity("MCX", "GOLD").asset_class is AssetClass.COMMODITY

def test_equity_and_index_ids_differ():
    # Same exchange+underlying must NOT be equal across asset classes once
    # the discriminator exists.
    assert InstrumentId.equity("NSE", "TCS") != InstrumentId.index("NSE", "TCS")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd domain && python -m pytest tests/test_instrument_id.py -v`
Expected: FAIL (`InstrumentId` has no `asset_class` attribute / equality still true)

- [ ] **Step 3: Add the discriminator to `InstrumentId`**

```python
from tradex_domain.enums import AssetClass, ExchangeId

@dataclass(frozen=True, slots=True)
class InstrumentId:
    exchange: str
    underlying: str
    expiry: date | None = None
    strike: Decimal | None = None
    right: str | None = None
    asset_class: AssetClass = AssetClass.EQUITY
    ...
```

- [ ] **Step 4: Make the classmethods set the correct asset class**

```python
@classmethod
def equity(cls, exchange, symbol):
    return cls(exchange=exchange, underlying=symbol, asset_class=AssetClass.EQUITY)

@classmethod
def index(cls, exchange, symbol):
    return cls(exchange=exchange, underlying=symbol, asset_class=AssetClass.INDEX)

@classmethod
def currency(cls, exchange, symbol):
    return cls(exchange=exchange, underlying=symbol, asset_class=AssetClass.CURRENCY)

@classmethod
def commodity(cls, exchange, symbol):
    return cls(exchange=exchange, underlying=symbol, asset_class=AssetClass.COMMODITY)

@classmethod
def future(cls, exchange, underlying, expiry):
    return cls(exchange=exchange, underlying=underlying, expiry=expiry,
               right="FUT", asset_class=AssetClass.FUTURE)

@classmethod
def option(cls, exchange, underlying, expiry, strike, right):
    return cls(exchange=exchange, underlying=underlying, expiry=expiry,
               strike=strike if isinstance(strike, Decimal) else Decimal(str(strike)),
               right=right, asset_class=AssetClass.OPTION)
```

- [ ] **Step 5: Fix the call sites in `instruments.py` to use the correct factory**

`instruments.py:75` → `InstrumentId.index(exchange, symbol)`
`instruments.py:137` → `InstrumentId.currency(exchange, symbol)`
`instruments.py:151` → `InstrumentId.commodity(exchange, symbol)`

(The `Equity.of` at line 61 already uses `.equity` — correct.)

- [ ] **Step 6: Update `__eq__`/`__hash__` already cover the new field automatically** (they compare all slots). Confirm `parse()` back-fills `asset_class` from `right`/shape (FUT→FUTURE, CE/PE→OPTION) and defaults to EQUITY otherwise:

```python
# at end of parse(), before return:
if right == "FUT":
    asset_class = AssetClass.FUTURE
elif right in {"CE", "PE"}:
    asset_class = AssetClass.OPTION
else:
    asset_class = AssetClass.EQUITY
return cls(exchange=exchange, underlying=underlying, expiry=expiry,
           strike=strike, right=right, asset_class=asset_class)
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd domain && python -m pytest tests/test_instrument_id.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add domain/src/tradex_domain/value_objects.py domain/src/tradex_domain/instruments.py domain/tests/test_instrument_id.py
git commit -m "domain: give InstrumentId a real asset-class discriminator"
```

---

### Task 2: Derive wire tag from `InstrumentId.asset_class` instead of hardcoding `"EQ"`

**Why:** `wire.py:_tag_from_id` (lines 140-145) returns `"EQ"` for everything that isn't FUT/OPT, so an `Index` registered with a provider key like `NSE:INDEX_NIFTY` gets reverse-mapped to a bare `NSE_EQ|NIFTY` key — wrong tag, potential collision with an actual equity `NIFTY`. Ponytail rule: fix once where all callers route through (`_tag_from_id`), not per-caller.

**Files:**
- Modify: `domain/src/tradex_domain/wire.py:139-145` (`_tag_from_id`)
- Test: `domain/tests/test_registry_key_semantics.py` (extend existing — already pins key semantics)

**Interfaces:**
- Consumes: `InstrumentId.asset_class` (from Task 1)
- Produces: correct tag string per asset class via `_TAG_BY_ASSET_CLASS` (already defined at wire.py:30-38)

- [ ] **Step 1: Write the failing test** (append to `domain/tests/test_registry_key_semantics.py`)

```python
from tradex_domain.value_objects import InstrumentId
from tradex_domain.enums import AssetClass

def test_index_key_uses_idx_tag():
    iid = InstrumentId.index("NSE", "NIFTY")
    assert InstrumentId.instrument_key(iid) == "NSE_IDX|NIFTY"

def test_currency_key_uses_cur_tag():
    iid = InstrumentId.currency("NSE", "USDINR")
    assert InstrumentId.instrument_key(iid) == "NSE_CUR|USDINR"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd domain && python -m pytest tests/test_registry_key_semantics.py::test_index_key_uses_idx_tag -v`
Expected: FAIL (key is `NSE_EQ|NIFTY`)

- [ ] **Step 3: Rewrite `_tag_from_id` to use the discriminator**

```python
@staticmethod
def _tag_from_id(instrument_id: InstrumentId) -> str:
    tag = _TAG_BY_ASSET_CLASS.get(str(instrument_id.asset_class.value))
    if tag is not None:
        return tag
    # Fallback for any asset class without an explicit mapping.
    return "EQ"
```

- [ ] **Step 4: Run full registry test suite + the new tests**

Run: `cd domain && python -m pytest tests/test_registry_key_semantics.py -v`
Expected: PASS (existing OPTION/FUTURE assertions unaffected — they still resolve via right/asset_class)

- [ ] **Step 5: Run the whole domain + broker suite to catch key-shape regressions**

Run: `python -m pytest domain/tests brokers/tests -q` (from repo root, using the repo's test runner)
Expected: 0 failures. If `dhan`/`upstox` master-load tests registered indices with an `EQ` tag expectation, they must be updated to `IDX` — grep for hardcoded `"EQ|"` expectations and fix.

- [ ] **Step 6: Commit**

```bash
git add domain/src/tradex_domain/wire.py domain/tests/test_registry_key_semantics.py
git commit -m "wire: derive provider-key tag from InstrumentId.asset_class"
```

---

### Task 3: Close the kill-switch TOCTOU in the reactive pipeline (ponytail: minimal, one guard)

**Why:** In `engine.py:_process_request_impl`, the idempotency guard reserves the correlation id (line 423) *before* the kill-switch check (line 432). If the kill-switch trips between the two, the reserved cid is never released → that cid is permanently poisoned (future retries raise `RuntimeError` "already reserved"). The lazy fix: check the kill-switch *before* reserving, OR release the reservation on the kill-switch early-return. One-line-ish.

**Files:**
- Modify: `trading/src/tradex_trading/execution/engine.py:419-433`
- Test: `trading/tests/execution/test_engine_killswitch.py` (create)

**Interfaces:**
- Consumes: `self._kill_switch`, `self._guard`
- Produces: no leaked reservation when kill-switch is active

- [ ] **Step 1: Write the failing test**

```python
from tradex_domain.execution import OrderRequest
from tradex_domain.value_objects import CorrelationId, Price, Quantity
from tradex_trading.execution.engine import ExecutionEngine, MemoryIdempotencyGuard

def test_kill_switch_does_not_leak_reservation():
    guard = MemoryIdempotencyGuard()
    engine = ExecutionEngine(bus=_fake_bus(), fill_source=_noop_fill(), guard=guard)
    engine.trip_kill_switch()
    cid = CorrelationId("00000000-0000-0000-0000-000000000001")
    req = OrderRequest(..., correlation_id=cid)
    engine._process_request(req)
    # After kill-switch early-return, the cid must be free to reserve again.
    assert guard.check_and_reserve(cid) is None  # not already reserved
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest trading/tests/execution/test_engine_killswitch.py -v`
Expected: FAIL (raises RuntimeError "already reserved")

- [ ] **Step 3: Reorder the guard — check kill-switch first**

```python
# 1. Kill switch (cheap, must precede any reservation)
if self._kill_switch.is_set():
    return
# 2. Idempotency check
if self._guard is not None:
    cid = request.correlation_id
    if cid is not None:
        dup = self._guard.check_and_reserve(cid)
        if dup is not None:
            log.info("Idempotency replay for correlation %s", cid)
            if self._metrics is not None:
                self._metrics.counter("orders.idempotency_replay").inc()
            return
```

(Move the block at engine.py:431-433 above engine.py:419-429.)

- [ ] **Step 4: Run test + engine suite**

Run: `python -m pytest trading/tests/execution -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/execution/engine.py trading/tests/execution/test_engine_killswitch.py
git commit -m "engine: check kill-switch before reserving idempotency cid"
```

---

### Task 4: De-duplicate `engine.py` submit paths (confirmed in scope)

**Why:** `_process_request_impl` (engine.py:412) and `_submit_impl` (engine.py:604) repeat ~80 lines of identical idempotency→risk→fill→OMS logic. The user opted to clean this up. The two paths differ in exactly three places; a single shared `_run_pipeline(request, *, sync)` collapses both.

**Asymmetry map (must preserve behavior):**
1. Return type: reactive path returns `None`; sync path returns `OrderReceipt`.
2. Idempotency replay: reactive early-returns nothing; sync returns `dup.result`.
3. Kill-switch early return: reactive returns nothing; sync returns a `rejected` `OrderReceipt(order_id="rejected", message="kill_switch_active")`.
4. Fill-submission failure: reactive re-raises `OrderSubmissionUnknownError` (the bus `on_error` will publish it); sync re-raises too. **Identical** — safe to share.
5. Non-boundary fill failure: reactive returns nothing; sync returns a `rejected` `OrderReceipt`.

Pattern: `_run_pipeline` returns `OrderReceipt | None`. Both public methods call it; the sync wrapper returns the receipt (or builds the kill-switch/idempotency-replay receipts before calling), the reactive wrapper ignores the return value.

**Files:**
- Modify: `trading/src/tradex_trading/execution/engine.py` (replace `_process_request_impl` + `_submit_impl` with `_run_pipeline` + thin adapters)
- Test: `trading/tests/execution/test_engine_dedup.py` (create) — proves both paths still produce identical OMS/bus effects.

**Interfaces:**
- Consumes: `self._guard`, `self._kill_switch`, `self._risk`, `self._fill`, `self._order_manager`, `self._position_manager`, `self._apply_fee`, `_make_order`
- Produces: `_run_pipeline(request, sync: bool) -> OrderReceipt | None`; `_process_request_impl` and `_submit_impl` become 3-line adapters.

- [ ] **Step 1: Write the failing/characterization test**

```python
from tradex_domain.execution import OrderRequest, Fill, Order
from tradex_domain.value_objects import CorrelationId, Price, Quantity, OrderId
from tradex_domain.enums import OrderStatus
from tradex_trading.execution.engine import ExecutionEngine

def test_sync_and_reactive_produce_same_oms(_engine, _request):
    # drive both paths, assert identical cache state + bus events
    _engine._run_pipeline(_request, sync=True)
    o1 = _engine.cache.all_orders()
    # reset
    _engine2 = make_fresh()
    _engine2._process_request(_request)
    o2 = _engine2.cache.all_orders()
    assert [o.order_id for o in o1] == [o.order_id for o in o2]
    assert _engine.cache.all_positions() == _engine2.cache.all_positions()
```

- [ ] **Step 2: Run to confirm it passes against current (pre-refactor) code** (characterization — must stay green after refactor)

Run: `python -m pytest trading/tests/execution/test_engine_dedup.py -v`
Expected: PASS (current duplicated code already behaves consistently)

- [ ] **Step 3: Extract `_run_pipeline`**

```python
def _run_pipeline(self, request, *, sync: bool) -> "OrderReceipt | None":
    """Single idempotency→risk→fill→OMS sequence shared by the reactive
    and synchronous submit paths. ``sync`` controls the return shape:
    None (reactive, fires-and-forgets on the bus) or an OrderReceipt.
    """
    # Kill switch (checked before reserving idempotency cid — see Task 3)
    if self._kill_switch.is_set():
        if sync:
            return OrderReceipt(order_id=OrderId(value="rejected"),
                                status=OrderStatus.REJECTED,
                                message="kill_switch_active")
        return None
    # Idempotency check
    cid = request.correlation_id
    if self._guard is not None and cid is not None:
        dup = self._guard.check_and_reserve(cid)
        if dup is not None:
            log.info("Idempotency replay for correlation %s", cid)
            if self._metrics is not None:
                self._metrics.counter("orders.idempotency_replay").inc()
            return dup.result if sync else None
    # Risk check
    if self._risk is not None and not self._risk.check(request):
        log.warning("Risk check failed for order")
        order = self._make_order(request, OrderStatus.REJECTED)
        self._order_manager.on_order_created(order)
        self._bus.publish(OrderRejected(order=order, reason="risk_check_failed"))
        if self._metrics is not None:
            self._metrics.counter("orders.rejected").inc()
            self._metrics.counter("risk.rejected").inc()
        return OrderReceipt(order_id=order.order_id, status=OrderStatus.REJECTED,
                            message="risk_check_failed") if sync else None
    # Fill
    try:
        order, fill = self._fill.submit(request)
    except Exception as exc:
        boundary_crossed = getattr(self._fill, "submission_boundary_crossed", False)
        if boundary_crossed:
            from tradex_domain.errors import OrderSubmissionUnknownError
            raise OrderSubmissionUnknownError(
                f"Order submission failed after crossing broker boundary: {exc}"
            ) from exc
        if self._guard is not None and cid is not None:
            self._guard.release(cid)
        order = self._make_order(request, OrderStatus.REJECTED)
        self._order_manager.on_order_created(order)
        self._bus.publish(OrderRejected(order=order, reason=str(exc)))
        if self._metrics is not None:
            self._metrics.counter("orders.rejected").inc()
        return OrderReceipt(order_id=order.order_id, status=OrderStatus.REJECTED,
                            message=str(exc)) if sync else None
    # OMS update
    self._order_manager.on_order_created(order)
    self._bus.publish(OrderPlaced(order=order))
    if fill is not None:
        if not getattr(self._fill, "position_projection_owned", False):
            self._position_manager.on_fill(fill)
            self._apply_fee(fill)
        self._order_manager.on_order_filled(order, fill)
        self._bus.publish(OrderFilled(fill=fill))
        if self._guard is not None and cid is not None:
            self._guard.record_result(cid, order.order_id)
        if self._metrics is not None:
            self._metrics.counter("orders.submitted").inc()
            self._metrics.counter("orders.filled").inc()
    elif self._metrics is not None:
        self._metrics.counter("orders.submitted").inc()
    if not sync:
        return None
    return OrderReceipt(order_id=order.order_id, status=order.status, message="submitted")
```

- [ ] **Step 4: Replace the two impl methods with thin adapters**

```python
def _process_request_impl(self, request):
    self._run_pipeline(request, sync=False)

def _submit_impl(self, request):
    return self._run_pipeline(request, sync=True)
```

(Delete the ~80 duplicated lines from each.)

- [ ] **Step 5: Run full engine + integration suite**

Run: `python -m pytest trading/tests/execution trading/tests/integration -q`
Expected: 0 failures. The characterization test (Step 1) must stay green — proving no behavior change.

- [ ] **Step 6: Commit**

```bash
git add trading/src/tradex_trading/execution/engine.py trading/tests/execution/test_engine_dedup.py
git commit -m "engine: collapse duplicated submit paths into _run_pipeline"
```

---

## Verification (superpowers: verification-before-completion)

Run, read output, THEN claim done:

1. `cd domain && python -m pytest tests/ -q` → expect 0 failures (covers Task 1 + 2).
2. `python -m pytest trading/tests/execution -q` → expect 0 failures (Task 3).
3. `python -m pytest tests/test_import_boundaries.py -q` → expect pass (domain stays pure).
4. `python -m pytest brokers/tests -q` → expect 0 failures (key-shape regression from Task 2).
5. If the repo defines a lint/typecheck command (e.g. `ruff`/`mypy` in pyproject.toml), run it and confirm 0 errors.

## Risks / open questions

- **Key-shape change (Task 2):** Any persisted/cached registry state or broker master-load test that assumed `NSE_EQ|NIFTY` for an index will now see `NSE_IDX|NIFTY`. Must grep broker tests for hardcoded `"EQ|"` and fix. This is the main blast-radius item — call out if any test relies on the old shape.
- **`InstrumentId` equality change (Task 1):** Making equity≠index for same (exchange, underlying) could surprise any code that used bare ids as fungible keys. Grep for `InstrumentId(` constructions without asset class in non-domain layers before merging.
- Task 4 is intentionally out of scope unless requested.

## What we explicitly are NOT doing

- Not re-fixing `fill_sources` order-id, `fees` SELL sign, `session` state machine, `streaming` `__all__/super` — already correct in current tree.
- Not adding new dependencies, factories, or abstractions (ponytail: no YAGNI violations).
- Not touching broker/trading market-data branches that key off `instrument.asset_class` (unaffected by the discriminator).
- Task 4 does **not** change pipeline behavior — only folds the two identical sequences into one helper; the characterization test guards against drift.
