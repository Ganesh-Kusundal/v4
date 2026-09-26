# Money-path correctness: review findings 1–7

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the 4 Critical and 3 Important defects an independent review found on the event-store money path, and harden the tests the review called decorative.

**Architecture:** Every fix keeps the existing spine (`apply_fill` / `PositionAccountant` / `CashLedger` / `OrderManager`) and the single-pass event fold. The recurring theme across findings 1, 4, 5, 6 is the same: state is derived in more than one pass, or a guard is checked after the fact. Each task makes the derivation single and ordered.

**Tech Stack:** Python 3.13, pytest 9.1.1, SQLite (WAL).

## Global Constraints

- **No commit unless the human asks.** Skip every commit step.
- **No second position or cash model** (`docs/target-architecture-design.md:15`) — fold through `apply_fill` / `CashLedger` / `PositionAccountant` / `OrderManager`.
- **Patch the authority module, never a shim** (`tradex_trading.*` string targets are order-dependent).
- **A green test is not evidence — prove it can fail.** Every task ends with a negative control: inject the defect, confirm red, restore, confirm green, `diff` to prove byte-identical restoration.
- **Never weaken an assertion to get green.** Complete the fixture instead.
- **Pytest:** `.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml`. The `-c pyproject.toml` is mandatory. Full suite ≈ 70s.
- **Fail closed.** Unknown state stops money movement; it never becomes zero or "sufficient".
- Reviewer's finding on test quality is part of scope: six named tests are weak and are hardened in Task 8.

---

### Task 1: A failed durable dispatch-intent write must not reach the broker

`engine.py:549` discards `_append(BrokerOrderRequested(request=request))`'s return value and calls `self._fill.submit(request)` regardless. In durable-required mode the kill switch trips, but the *current* order still crosses the broker boundary, and a crash immediately after leaves no durable record that an order may be live at the venue.

**Files:**
- Modify: `execution/src/tradex_execution/engine.py:549-551` (dispatch), and the risk-decision append at `:534`

**Interfaces:**
- Consumes: `_append(event) -> bool` (already returns False on a failed write).
- Produces: no new API. Contract: a failed intent write means the order never reaches the venue.

- [ ] **Step 1: Write the failing test**

Create `trading/tests/execution/test_dispatch_requires_durable_intent.py`:

```python
"""A dispatch intent that cannot be persisted must not reach the broker."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.execution.recovery import InMemoryEventStore
from tradex_trading.execution.trading_cache import TradingCache


def _bus() -> MagicMock:
    bus = MagicMock()
    bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()
    return bus


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )


def test_broker_is_not_called_when_the_intent_write_fails() -> None:
    store = InMemoryEventStore()
    fill_source = SimulatedFillSource()
    engine = ExecutionEngine(
        bus=_bus(),
        fill_source=fill_source,
        cache=TradingCache(),
        event_store=store,
        cash=Decimal("100000"),
        require_durable_events=True,
    )

    original = store.append
    state = {"failed": False}

    def flaky(event):
        if type(event).__name__ == "BrokerOrderRequested" and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("disk full")
        return original(event)

    store.append = flaky
    try:
        engine.submit(_request())
    except Exception:
        pass  # a rejection is fine; crossing the boundary is not

    assert state["failed"] is True, "the intent write never failed"
    # The venue must not have been called: a crash after this point would
    # leave an order live at the broker with no durable record of it.
    assert fill_source.submit_calls == 0, (
        "the order reached the broker despite a failed durable intent write"
    )
    assert engine.kill_switch is True
```

> `SimulatedFillSource` has no call counter. Add one in the same task:
> in `execution/src/tradex_execution/fill_sources.py`, initialise `self.submit_calls = 0` in
> `SimulatedFillSource.__init__` and increment it as the first statement of its `submit`.

- [ ] **Step 2: Run it and confirm it fails**

```bash
.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q trading/tests/execution/test_dispatch_requires_durable_intent.py
```

Expected: FAIL — the broker was called.

- [ ] **Step 3: Abort the pipeline when the intent cannot be persisted**

In `engine.py`, replace the dispatch block:

```python
        # 3. Fill — Wave C1: persist broker dispatch intent before the call.
        # A failed write means this order may never be recoverable, so it must
        # not cross the broker boundary: a crash after the venue accepted it
        # would leave a live order with no durable record.
        if not self._append(BrokerOrderRequested(request=request)):
            log.error(
                "Dispatch intent not durable for %s; order not sent to venue",
                request.correlation_id,
            )
            return None
        try:
            order, fill = self._fill.submit(request)
```

Apply the same guard to the `RiskDecision` approval append immediately above it: if that write fails, do not dispatch either.

- [ ] **Step 4: Run the test and the surrounding suites**

```bash
.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q trading/tests/execution trading/tests/runtime
```

Expected: all pass. If a test asserted "kill switch tripped but the broker was still called", that test encoded the old behavior — update it to the new contract rather than weakening the guard.

- [ ] **Step 5: Negative control**

Restore the old order (ignore the return value), confirm the test fails, restore, confirm green, and `diff` both files.

---

### Task 2: Distinct partial fills must not be discarded

`recovery.py` dedups on `fill_id` else `(order, side, qty, price)`. Two legitimate 5-share partials at the same price with no venue `fill_id` collapse into one: **cash 500 instead of 0, quantity 5 instead of 10** (reproduced).

**The discriminator is the order's total quantity.** Fills are accepted while the cumulative filled quantity stays within the order; the fill that would overshoot is the re-delivery. This was previously flagged as undecidable in code — it is not, provided the order quantity is known.

**Files:**
- Modify: `execution/src/tradex_execution/recovery.py` (`_fill_identity` consumers, the `seen` set in `fold_cash` and in `recover_trading_cache`)
- Test: `trading/tests/execution/test_partial_fill_recovery.py` (create)

**Interfaces:**
- Consumes: `Order.quantity` / `Order.filled_quantity` on carriers replayed from the log.
- Produces: `_is_duplicate_fill(seen, order_totals, fill) -> bool` in `recovery.py`.

- [ ] **Step 1: Write the failing test**

Create `trading/tests/execution/test_partial_fill_recovery.py`:

```python
"""Equal-lot partials are distinct fills, not duplicates.

Without a venue execution id, ``(order, side, qty, price)`` is identical for
two legitimate 5-share partials of one 10-share order. The order's total
quantity is what separates them: accept while the cumulative stays within the
order, reject the one that overshoots.
"""
from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import CashAccountInitialized, OrderFilled, OrderPlaced
from tradex_domain.execution import Fill, Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_trading.execution.recovery import (
    InMemoryEventStore,
    recover_trading_cache,
)
from tradex_trading.execution.trading_cache import TradingCache

INSTRUMENT = Equity.of("NSE", "RELIANCE")
ORDER_ID = OrderId("o-partial")


def _order() -> Order:
    return Order(
        order_id=ORDER_ID,
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.ACK,
    )


def _partial(qty: str = "5") -> OrderFilled:
    # fill_id=None: the venue supplied no execution id.
    return OrderFilled(
        fill=Fill(
            order_id=ORDER_ID,
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            quantity=Quantity(value=Decimal(qty)),
            price=Price(value=Decimal("100")),
            fill_id=None,
        ),
    )


def test_two_equal_partials_are_both_applied() -> None:
    store = InMemoryEventStore()
    store.append(CashAccountInitialized(amount=Decimal("1000")))
    store.append(OrderPlaced(order=_order()))
    store.append(_partial())
    store.append(_partial())

    cache = TradingCache()
    state = recover_trading_cache(store, cache)

    assert cache.get_position(INSTRUMENT).quantity.value == Decimal("10"), (
        "two 5-share partials of a 10-share order must total 10, not 5"
    )
    assert state.cash.cash == Decimal("0"), "1000 - 500 - 500 = 0"


def test_a_redelivered_partial_beyond_the_order_quantity_is_ignored() -> None:
    """The third 5-share fill overshoots the order, so it is a re-delivery."""
    store = InMemoryEventStore()
    store.append(CashAccountInitialized(amount=Decimal("1000")))
    store.append(OrderPlaced(order=_order()))
    store.append(_partial())
    store.append(_partial())
    store.append(_partial())  # would overshoot 10 -> duplicate

    cache = TradingCache()
    state = recover_trading_cache(store, cache)

    assert cache.get_position(INSTRUMENT).quantity.value == Decimal("10")
    assert state.cash.cash == Decimal("0")


def test_venue_fill_id_still_wins_as_identity() -> None:
    """A real execution id is authoritative and dedups exactly."""
    store = InMemoryEventStore()
    store.append(CashAccountInitialized(amount=Decimal("1000")))
    store.append(OrderPlaced(order=_order()))
    for _ in range(2):
        store.append(
            OrderFilled(
                fill=Fill(
                    order_id=ORDER_ID,
                    instrument=INSTRUMENT,
                    side=OrderSide.BUY,
                    quantity=Quantity(value=Decimal("5")),
                    price=Price(value=Decimal("100")),
                    fill_id="venue-1",
                ),
            ),
        )

    cache = TradingCache()
    state = recover_trading_cache(store, cache)

    assert cache.get_position(INSTRUMENT).quantity.value == Decimal("5")
    assert state.cash.cash == Decimal("500")
```

- [ ] **Step 2: Run it and confirm the first test fails**

```bash
.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q trading/tests/execution/test_partial_fill_recovery.py
```

Expected: `test_two_equal_partials_are_both_applied` FAILS (quantity 5, cash 500).

- [ ] **Step 3: Make dedup order-quantity aware**

In `recovery.py`, replace `_fill_identity` and add the discriminator:

```python
def _fill_identity(fill) -> str:
    """Stable identity for a fill, used to skip re-delivered broker fills."""
    return fill.fill_id or (
        f"{fill.order_id.value}|{fill.side.value}|"
        f"{fill.quantity.value}|{fill.price.value}"
    )


def _is_duplicate(
    key: str,
    seen: set[str],
    cumulative: dict[str, Decimal],
    order_totals: dict[str, Decimal],
    fill,
) -> bool:
    """True when *fill* is a re-delivery rather than a new partial.

    A venue ``fill_id`` is authoritative: the same id is the same execution.
    Without one, equal-lot fills of one order are only distinguishable by
    whether the order still has room. Accept while the cumulative filled
    quantity stays within the order; the fill that overshoots is the
    re-delivery. This is the only safe reading — treating every equal-lot fill
    as a duplicate silently loses real fills, and treating them all as new
    double-counts a re-delivery.
    """
    if key in seen:
        if fill.fill_id is not None:
            return True
    else:
        return False
    order_key = fill.order_id.value
    total = order_totals.get(order_key)
    if total is None:
        # No order record: fall back to the old conservative behavior.
        return True
    running = cumulative.get(order_key, Decimal("0"))
    return running + fill.quantity.value > total
```

Then in **both** `fold_cash` and `recover_trading_cache`, replace the bare `if key in seen: continue` with:

```python
        if _is_duplicate(key, seen, cumulative, order_totals, event.fill):
            continue
        seen.add(key)
        oid = event.fill.order_id.value
        cumulative[oid] = cumulative.get(oid, Decimal("0")) + event.fill.quantity.value
```

and build `order_totals` up front from any `OrderPlaced` / `OrderModified` carrier in the
log (first occurrence wins for the original quantity):

```python
    order_totals: dict[str, Decimal] = {}
    for event in events:
        order = getattr(event, "order", None)
        if order is not None:
            order_totals.setdefault(order.order_id.value, order.quantity.value)
```

**Both functions must use the same helper.** Separate implementations are how cash and
positions silently disagreed in the first place.

- [ ] **Step 4: Run the new tests and the parity/recovery suites**

```bash
.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q trading/tests/execution trading/tests/parity
```

Expected: all pass. `test_reconstruction_is_idempotent_under_duplicate_events` appends the *same event object* twice, which the `fill_id` path still dedups — it must stay green.

- [ ] **Step 5: Negative control**

Revert `_is_duplicate` to `key in seen`, confirm the partial test fails, restore, confirm green, `diff`.

---

### Task 3: Order recovery must be one ordered pass

`recover_trading_cache` installs carrier snapshots (including terminal `OrderCancelled`) and *then* re-applies every fill to that final snapshot. A 4-share partial followed by a cancel recovers as `PARTIALLY_FILLED` with 8 shares instead of `CANCELLED` with 4 (reviewer-reproduced).

**Files:**
- Modify: `execution/src/tradex_execution/recovery.py` (`recover_trading_cache`; `SessionRecovery` may stay for order-only use)
- Test: `trading/tests/execution/test_order_recovery_ordering.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""A cancelled order must recover as cancelled, not as re-filled."""
from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import (
    CashAccountInitialized, OrderCancelled, OrderFilled, OrderPlaced,
)
from tradex_domain.execution import Fill, Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_trading.execution.recovery import (
    InMemoryEventStore, recover_trading_cache,
)
from tradex_trading.execution.trading_cache import TradingCache

INSTRUMENT = Equity.of("NSE", "RELIANCE")
ORDER_ID = OrderId("o-cancel")


def test_partial_fill_then_cancel_recovers_as_cancelled() -> None:
    store = InMemoryEventStore()
    store.append(CashAccountInitialized(amount=Decimal("100000")))
    store.append(
        OrderPlaced(
            order=Order(
                order_id=ORDER_ID, instrument=INSTRUMENT, side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=Quantity(value=Decimal("10")),
                price=Price(value=Decimal("100")),
                time_in_force=TimeInForce.DAY, status=OrderStatus.ACK,
            ),
        ),
    )
    store.append(
        OrderFilled(
            fill=Fill(
                order_id=ORDER_ID, instrument=INSTRUMENT, side=OrderSide.BUY,
                quantity=Quantity(value=Decimal("4")),
                price=Price(value=Decimal("100")), fill_id="f1",
            ),
        ),
    )
    store.append(
        OrderCancelled(
            order=Order(
                order_id=ORDER_ID, instrument=INSTRUMENT, side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=Quantity(value=Decimal("10")),
                price=Price(value=Decimal("100")),
                time_in_force=TimeInForce.DAY,
                status=OrderStatus.CANCELLED,
                filled_quantity=Quantity(value=Decimal("4")),
            ),
        ),
    )

    cache = TradingCache()
    recover_trading_cache(store, cache)
    recovered = cache.get_order(ORDER_ID)

    assert recovered.status == OrderStatus.CANCELLED
    assert recovered.filled_quantity.value == Decimal("4"), (
        "the cancel already carries the filled quantity; re-applying the fill "
        "double-counts it"
    )
```

- [ ] **Step 2: Confirm it fails**, then **Step 3: rewrite `recover_trading_cache` as a single ordered pass**:

```python
    cache.clear()
    from tradex_execution.order_manager import OrderManager
    from tradex_execution.position_manager import PositionManager

    order_manager = OrderManager(cache)
    position_manager = PositionManager(cache)
    events = tuple(event_store.replay("orders"))
    order_totals = _order_totals(events)
    seen: set[str] = set()
    cumulative: dict[str, Decimal] = {}
    orders = 0

    for event in events:
        order = getattr(event, "order", None)
        if order is not None:
            # Carriers carry the post-transition state, including any fills
            # already applied. Installing them in stream order means the final
            # snapshot wins, which is what the venue reported.
            cache.update_order(order)
            orders += 1
            continue
        if not isinstance(event, OrderFilled):
            continue
        fill = event.fill
        if _is_duplicate(_fill_identity(fill), seen, cumulative, order_totals, fill):
            continue
        seen.add(_fill_identity(fill))
        oid = fill.order_id.value
        cumulative[oid] = cumulative.get(oid, Decimal("0")) + fill.quantity.value
        current = cache.get_order(oid)
        if current is None:
            current = _stub_order(fill)
            cache.update_order(current)
        # Only advance the order when the carrier stream has not already
        # carried it past this fill; otherwise the snapshot is authoritative.
        if current.status not in _TERMINAL_AFTER_FILL:
            order_manager.on_order_filled(current, fill)
        position_manager.on_fill(fill)
        if event.fee_amount:
            position_manager.on_fee(fill, Money(amount=event.fee_amount))
```

Extract `_stub_order(fill)` and `_order_totals(events)` as module helpers, and define
`_TERMINAL_AFTER_FILL = frozenset({OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.FILLED})`
so a fill never resurrects a terminal order. Return a `RecoveryResult` built from
`orders` and `len(events)` so `startup.py`'s existing log line keeps working.

- [ ] **Step 4: Run** `trading/tests/execution trading/tests/runtime trading/tests/sdk`.
- [ ] **Step 5: Negative control** — restore the two-pass version, confirm the test fails, restore, `diff`.

---

### Task 4: An unpriced order must not be cash-checked as zero

`risk.py:_incoming_exposure()` returns `0` when no mark is available. A MARKET BUY that *reduces* a short skips the fresh-mark gate (it only fires when absolute exposure increases), so the cash check compares `0` against cash and admits the order.

**Files:**
- Modify: `execution/src/tradex_execution/risk.py` (the cash-check block, ~`:405-431`)
- Test: `trading/tests/execution/test_risk_cash_unpriced.py` (create)

- [ ] **Step 1: Write the failing test** — bind a cash provider, register a short position, supply no price, submit a BUY that closes it, and assert it is denied with `unknown_market_value`:

```python
def test_buy_that_reduces_a_short_still_needs_a_price() -> None:
    """A zero notional is not a passed cash check."""
    rm = RiskManager()
    rm.fail_closed_cash = True
    rm.bind_cash_provider(lambda: Decimal("100000"))
    rm.set_positions_provider(lambda: [_short_position(Decimal("5"))])
    # No price provider bound -> no mark for the instrument.

    assert rm.check(_buy_request(quantity="5")) is False
    assert rm._last_deny_reason == "unknown_market_value"
```

Build `_short_position` with `quantity=Quantity(value=Decimal("-5"))` and zero P&L.

- [ ] **Step 2: Confirm it fails.**
- [ ] **Step 3: Fix** — inside the cash block, when `fail_closed_cash` is set and the incoming exposure could not be valued, deny rather than compare zero:

```python
                    else:
                        incoming = self._incoming_exposure(request)
                        if incoming == 0 and self._fail_closed_requires_price(request):
                            return self._deny("unknown_market_value", now=now)
                        if incoming > Decimal(str(cash)):
                            return self._deny("insufficient_cash", now=now)
```

with a small helper that returns True when the request has no resolvable price and the
position it trades is not already flat. Keep the existing `unknown_market_value` reason code
so the metric series does not fork.

- [ ] **Step 4: Run** `trading/tests/execution trading/tests/parity`.
- [ ] **Step 5: Negative control** — remove the new denial, confirm red, restore, `diff`.

---

### Task 5: Do not anchor a log that already has fills

If the opening anchor failed to persist but fills did, startup reads the broker's *current* balance and appends it as the anchor. The next fold starts from that already-net balance and applies every historical fill again. Separately, picking the latest anchor does not segment history: opening 1000 → BUY 1000 → corrected anchor 2500 folds to 1500 instead of 2500.

**Files:**
- Modify: `runtime/src/tradex_runtime/startup.py` (the cold-boot anchor branch)
- Modify: `execution/src/tradex_execution/recovery.py` (`_order_totals` sibling: `_cash_events`)
- Test: extend `trading/tests/execution/test_cash_anchor_integrity.py`

- [ ] **Step 1: Add the failing tests**

```python
def test_a_log_with_fills_but_no_anchor_is_not_anchored_from_broker_funds(
    monkeypatch, tmp_path,
) -> None:
    """Anchoring a fill-bearing log double-counts every historical fill.

    The broker's current balance is already net of those fills, so using it as
    the opening balance and then replaying the fills subtracts them twice.
    """
    # Boot once to create the store, write fills WITHOUT an anchor, then boot
    # again with a broker balance and assert the recovered cash is not the
    # broker figure minus the fills.
    ...

def test_a_corrected_anchor_discards_the_fills_before_it() -> None:
    """A re-anchoring restates the balance; earlier fills are already in it."""
    store = InMemoryEventStore()
    store.append(CashAccountInitialized(amount=Decimal("1000")))
    store.append(_buy_fill("1000"))          # 1000 - 1000 = 0
    store.append(CashAccountInitialized(amount=Decimal("2500")))  # deposit
    assert fold_cash(tuple(store.replay("orders"))).cash == Decimal("2500")
```

- [ ] **Step 2: Confirm both fail.**
- [ ] **Step 3: Segment the fold at the latest anchor** in `fold_cash`: find the index of the last `CashAccountInitialized` and apply only `OrderFilled` / `CashReconciled` events *after* it:

```python
    last_anchor = max(
        (i for i, e in enumerate(events) if isinstance(e, CashAccountInitialized)),
        default=None,
    )
    if last_anchor is None:
        raise CashStateUnknownError(...)
    opening = events[last_anchor].amount
    ledger = CashLedger(initial=opening, allow_negative=True)
    for event in events[last_anchor + 1:]:
        ...
```

- [ ] **Step 4: Refuse to anchor a fill-bearing log.** In `startup.py`, only anchor when the
  recovered log had no anchor **and** carried no fills; otherwise log an error and let the
  fail-closed cash gate keep denying:

```python
    if opening_cash is not None and engine.cash_anchor_missing:
        if engine.cache.all_orders():
            log.error(
                "Event log has fills but no opening balance; refusing to "
                "anchor from broker funds (it is already net of those fills). "
                "Cash stays unknown and BUYs are denied until an operator "
                "records a CashAccountInitialized."
            )
        else:
            engine.restore_cash(opening_cash, re_anchor=True)
```

- [ ] **Step 5: Also fix the silent anchor failure**: in `restore_cash`, only set
  `_cash_anchor_appended = True` when the append actually succeeded:

```python
        if re_anchor:
            if not self._cash_anchor_appended:
                self._cash_anchor_appended = self._append(
                    CashAccountInitialized(amount=cash),
                )
                return
        # A restore (not a re-anchor) proves the log already carries an anchor.
        self._cash_anchor_appended = True
```

- [ ] **Step 6: Run** `trading/tests/execution trading/tests/runtime`; **Step 7: negative control**.

---

### Task 6: Inbound dedup must not swallow a retry after a failed write

`engine.py:_apply_fill` records the fingerprint *before* appending. If the append fails, the venue's identical retry is skipped forever: the fill is never applied and never durable (reviewer-reproduced).

**Files:**
- Modify: `execution/src/tradex_execution/engine.py` (`_apply_fill`)
- Test: extend `trading/tests/execution/test_append_before_mutate.py`

- [ ] **Step 1: Add the failing test** — fail the first append, then replay the *same* fill and assert it is applied and durable:

```python
def test_a_retry_after_a_failed_write_is_still_applied() -> None:
    """Recording the dedup fingerprint before the write loses the fill forever."""
    # ... build engine with a failing-then-working store ...
    engine._apply_fill(OrderFilled(fill=fill))   # append fails
    engine._apply_fill(OrderFilled(fill=fill))   # venue retries
    assert cache.all_positions(), "the retry must be applied, not skipped"
```

- [ ] **Step 2: Confirm it fails.**
- [ ] **Step 3: Record the fingerprint only after a successful append.** `_record_applied_fill`
  currently both tests and inserts. Split it: keep the membership test, and insert **after**
  `_append` returns True (and after the mutations, which are already gated on it).
- [ ] **Step 4: Run** `trading/tests/execution`; **Step 5: negative control**.

---

### Task 7: `_apply_fill` must honor the recorded fee

`_apply_fill` recomputes the fee, so an `OrderFilled` carrying `fee_amount` is charged at today's rate rather than the recorded one. `ReplayFillSource` handles this correctly, but has no production caller.

**Files:**
- Modify: `execution/src/tradex_execution/engine.py` (`_apply_fill`)
- Test: `trading/tests/execution/test_recorded_fee_replay.py` (create)

- [ ] **Step 1: Write the failing test** — publish an `OrderFilled` carrying `fee_amount=Decimal("20")` into a bus with a `FeeCalculator` that would charge something else, and assert the applied fee is 20.
- [ ] **Step 2: Confirm it fails.**
- [ ] **Step 3: Use `event.fee_amount` when present**, mirroring `_fee_for_fill`:

```python
        charged = event.fee_amount
        if charged is None:
            charged = self._compute_fee(fill)
```

- [ ] **Step 4: Run** `trading/tests/execution trading/tests/parity`; **Step 5: negative control**.

---

### Task 8: Harden the tests the review called decorative

Six named weaknesses. Each is a real gap in coverage, not a style issue.

| File | Weakness | Fix |
|---|---|---|
| `test_risk_cash_gate_fails_closed.py` | No test for an **unbound** provider with `fail_closed_cash=True` — the most likely real path | Add `test_unbound_provider_denies_when_fail_closed` |
| `test_live_cash_anchor.py` | All four tests are wiring-only: no anchor, no denied BUY, no readiness assertion after unknown funds | Assert `cash_anchor_missing is False` after a cold boot, and assert a BUY is denied when funds are unreadable |
| `test_crash_restart_reconstruction.py` | One restart only, so it could not see per-restart anchor growth | Restart twice and count anchors (covered by Task 5's test; add the assertion here too) |
| `test_mode_parity_across_paths.py` | The ladder changes cash even with zero fees, so deleting fees entirely still passes | Bind a `FeeCalculator` and assert `total_fees > 0` in the projection path |
| `test_projection_is_api_contract.py` | The export test is decorative about API use | Fold it into the endpoint test, which is the one that proves the consumer |
| `test_append_before_mutate.py` | Checks only initial position/cash; misses retry and fee state | Extended by Task 6 |

- [ ] **Step 1: Apply each row.**
- [ ] **Step 2: For every new assertion, run a negative control** that removes the behavior it protects and confirm red.
- [ ] **Step 3: Run the full suite.**

---

## Final verification

```bash
.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q --timeout=300
cd frontend && npm run typecheck && npm run build && npm run e2e
```

Expected: `0 failed` on pytest (count ≥ 3787); typecheck clean; build succeeds; Playwright
all-pass with a captured summary. mypy clean on the changed modules.
