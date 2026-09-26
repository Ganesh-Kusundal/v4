# Fail-closed money: live cash binding, append-before-mutate, E2E gate

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the live cash gate actually run against broker funds, make the durable event the source of truth for a fill, and restore the Playwright gate that has been silently not running.

**Architecture:** Three independent fixes, ordered cheapest-and-most-blocking first. (1) `startup.py` binds a cash provider that reads `broker.get_account()`; `RiskManager` stops failing open when cash is unknown. (2) The fill path appends `OrderFilled` *before* touching the in-memory book, so the event is authoritative and the cache is a projection. (3) The Playwright `webServer` targets the authority module instead of a `-m`-inert shim.

**Tech Stack:** Python 3.13, pytest 9.1.1, SQLite (WAL), Playwright, TypeScript/Vite.

## Global Constraints

- **No commit unless the human asks** (`docs/superpowers/specs/2026-09-24-solid-platform-mega-design.md`). The commit steps below are written for a normal repo; **skip every `git commit` step in this session** unless explicitly asked.
- **No second position or cash model** (`docs/target-architecture-design.md:15`). Reconstruct and fold through the existing `CashLedger` / `PositionAccountant` / `apply_fill`.
- **Patch the authority module, never a shim.** `trading/src/tradex_trading/**` files are 11-line `importlib` re-exports. Their package `__init__` copies the authority's *submodule attributes*, so a patch on a shim path silently redirects depending on import order (this caused 21 of the 31 baseline failures).
- **A green test is not evidence — prove it can fail.** Every task below includes a negative control: inject the defect, confirm the test goes red, restore, confirm green, `diff` to prove the restore was byte-identical.
- **Never weaken an assertion to get green.** If a fixture is incomplete, complete the fixture.
- **Pytest invocation is load-bearing:** `.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml`. Without `-c pyproject.toml`, rootdir discovery walks up and dies on a PermissionError. Full suite ≈ 70s.
- Fail closed over silent continue. Unavailable cash, an unwritable event store, or a stale mark all stop order admission.

---

### Task 1: Restore the Playwright gate

The browser suite has never run in this worktree. `webServer` invokes `python -m tradex_trading.interface.cli serve`; the shim has no `if __name__ == "__main__":` guard, so the process exits 0 without starting a server and Playwright reports `Process from config.webServer exited early`. Verified: `python -m tradex_interfaces.cli serve` serves correctly and `/ui/` returns 200.

**Files:**
- Modify: `frontend/playwright.config.ts:28` (`PYTHONPATH`), `:66-70` (`webServer.command`)

**Interfaces:**
- Consumes: nothing.
- Produces: a green Playwright run, and a working `webServer` command for Task 3's verification.

- [ ] **Step 1: Confirm the current failure (do not skip — this is the bug)**

```bash
cd frontend && npm run e2e 2>&1 | tail -5
```

Expected: `Error: Process from config.webServer exited early.`

- [ ] **Step 2: Fix `PYTHONPATH` and the serve target**

In `frontend/playwright.config.ts`, change line 28 so the new package layouts resolve:

```typescript
const PYTHON = process.env.E2E_PYTHON ?? '../.venv/bin/python';
const PYTHONPATH = [
  '../domain/src',
  '../brokers/src',
  '../execution/src',
  '../reactive/src',
  '../runtime/src',
  '../interfaces/src',
  '../market_data/src',
  '../config/src',
  '../strategy/src',
  '../replay/src',
  '../persistence/src',
  '../trading/src',
].join(':');
```

Then change the `webServer.command` entries (line 67-70) to target the authority module:

```typescript
    command: [
      serve('../trading/scripts/seed_e2e_datalake.py'),
      serve(`-m tradex_interfaces.cli serve --broker paper --port ${PORT}`),
```

- [ ] **Step 3: Verify the server actually starts now**

```bash
cd frontend && (env PYTHONPATH=$(node -e "console.log('../domain/src:'+'../brokers/src:'+'../execution/src:'+'../reactive/src:'+'../runtime/src:'+'../interfaces/src:'+'../market_data/src:'+'../config/src:'+'../strategy/src:'+'../replay/src:'+'../persistence/src:'+'../trading/src')") ../.venv/bin/python -m tradex_interfaces.cli serve --broker paper --port 8197 > /tmp/serve-check.log 2>&1 &) ; sleep 14; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8197/ui/; pkill -f "port 8197"
```

Expected: `200`. A `000` means the import layout is still wrong — fix `PYTHONPATH` before continuing.

- [ ] **Step 4: Run the suite**

```bash
cd frontend && npm run e2e 2>&1 | tail -25
```

Expected: all specs pass, and a final summary line. If specs fail, capture the count and the first failure; do not paper over it.

- [ ] **Step 5: Record the result**

Note pass/fail counts in the final report. A red spec here is real frontend behavior and must be reported, not suppressed.

---

### Task 2: Bind live cash to broker funds and stop failing open

Today `cfg.risk.cash_provider` defaults to `None` and nothing in production sets it, so `risk.py:409-412` skips the BUY cash check entirely — a live BUY can be admitted with no cash check at all. Worse, when a provider *is* bound and throws, `risk.py:415-416` sets `cash = None` and the `if cash is not None` guard then admits the order. Both violate the project rule "no order admission when cash is unavailable."

**Files:**
- Modify: `execution/src/tradex_execution/risk.py:405-420` (cash check), `:315-326` (provider accessors)
- Modify: `runtime/src/tradex_runtime/startup.py:392-395` (bind the provider)
- Test: `trading/tests/execution/test_risk_cash_gate_fails_closed.py` (create)
- Test: `trading/tests/runtime/test_live_cash_anchor.py` (create)

**Interfaces:**
- Consumes: `broker.get_account() -> Account` (already on every adapter: `brokers/src/tradex_brokers/dhan/_portfolio.py:29`, `upstox/_portfolio.py:27`, `paper/adapter.py:287`); `Account.balance: Money | None` (`domain/src/tradex_domain/execution.py:319-323`); `CashAccountInitialized(amount)` (already exists, `domain/src/tradex_domain/events.py`).
- Produces: `RiskManager.fail_closed_cash: bool` (new flag, default `False`); deny reason `"cash_unavailable"`; a startup-bound cash provider in live and paper mode.

- [ ] **Step 1: Write the failing test — a raising provider must deny, not admit**

Create `trading/tests/execution/test_risk_cash_gate_fails_closed.py` (request-builder idiom copied from `trading/tests/execution/test_risk_cash_check.py:40-53`, which is the established pattern for this gate):

```python
"""A BUY must not be admitted when the cash balance is unavailable."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_trading.execution.engine import RiskManager


def _buy_request(quantity: str = "10", price: str = "100") -> Any:
    return OrderRequest(
        instrument=Equity.of("NSE", "TEST"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
    )


def test_raising_cash_provider_denies_when_fail_closed() -> None:
    def _boom() -> Decimal:
        raise RuntimeError("broker funds unavailable")

    rm = RiskManager()
    rm.fail_closed_cash = True
    rm.bind_cash_provider(_boom)
    assert rm.check(_buy_request()) is False
    assert rm._last_deny_reason == "cash_unavailable"


def test_none_cash_provider_denies_when_fail_closed() -> None:
    rm = RiskManager()
    rm.fail_closed_cash = True
    rm.bind_cash_provider(lambda: None)
    assert rm.check(_buy_request()) is False
    assert rm._last_deny_reason == "cash_unavailable"


def test_unbound_provider_admits_when_not_fail_closed() -> None:
    """Backtest/paper without a cash model keep working (no regression)."""
    rm = RiskManager()
    rm.fail_closed_cash = False
    assert rm.check(_buy_request()) is True


def test_raising_provider_admits_when_not_fail_closed() -> None:
    def _boom() -> Decimal:
        raise RuntimeError("nope")

    rm = RiskManager()
    rm.fail_closed_cash = False
    rm.bind_cash_provider(_boom)
    assert rm.check(_buy_request()) is True
```

The deny reason is read via `rm._last_deny_reason` (the private attribute at `risk.py:131`); there is no public accessor today, and adding one is out of scope for this fix.

- [ ] **Step 2: Run it and confirm it fails**

```bash
.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q trading/tests/execution/test_risk_cash_gate_fails_closed.py
```

Expected: failures — `RiskManager` has no `fail_closed_cash` attribute.

- [ ] **Step 3: Implement the fail-closed branch**

In `execution/src/tradex_execution/risk.py`, add to `__init__` near the other config flags (around line 117-120):

```python
        #: When True, a BUY is denied unless a cash provider is bound AND
        #: returns a balance. Live sets this so an unavailable balance stops
        #: admission instead of silently skipping the gate.
        self.fail_closed_cash: bool = False
```

Replace the cash check at lines 405-420 with:

```python
            # Cash check (C1) — only BUY is cash-gated; a SELL is a credit.
            if request.side is OrderSide.BUY:
                provider = getattr(self, "_cash_provider", None)
                if provider is None:
                    if self.fail_closed_cash:
                        return self._deny("cash_unavailable", now=now)
                else:
                    try:
                        cash = provider()
                    except Exception:
                        cash = None
                    if cash is None:
                        if self.fail_closed_cash:
                            return self._deny("cash_unavailable", now=now)
                    else:
                        incoming = self._incoming_exposure(request)
                        if incoming > Decimal(str(cash)):
                            return self._deny("insufficient_cash", now=now)
```

- [ ] **Step 4: Run the risk suites**

```bash
.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q trading/tests/execution trading/tests/parity
```

Expected: all pass. If `test_risk_cash_check.py` or a parity test regressed, that test was relying on the fail-open path — update it to set `fail_closed_cash = False` explicitly rather than weakening a production guard.

- [ ] **Step 5: NEGATIVE CONTROL — prove the new test can fail**

```bash
cp execution/src/tradex_execution/risk.py /tmp/risk.bak
# Remove the fail-closed denial:
.venv/bin/python - <<'EOF'
p='execution/src/tradex_execution/risk.py'; s=open(p).read()
s=s.replace("                    if self.fail_closed_cash:\n                        return self._deny(\"cash_unavailable\", now=now)","                    pass",2)
open(p,'w').write(s)
EOF
.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q trading/tests/execution/test_risk_cash_gate_fails_closed.py   # MUST FAIL
cp /tmp/risk.bak execution/src/tradex_execution/risk.py
diff /tmp/risk.bak execution/src/tradex_execution/risk.py && echo "RESTORED IDENTICAL"
```

Expected: the test FAILS, then passes again after restore.

- [ ] **Step 6: Bind the provider at startup from broker funds**

In `runtime/src/tradex_runtime/startup.py`, replace lines 392-395:

```python
    # Cash gate: live and paper must know the real balance. A BUY whose
    # notional cannot be covered is denied, and an unavailable balance denies
    # too (fail closed) rather than skipping the gate. Backtest/replay own
    # their own CashLedger and bypass this gate.
    if cfg.mode in ("paper", "live") and cfg.risk.cash_provider is None:
        risk_manager.bind_cash_provider(lambda: _broker_available_cash(broker))
        risk_manager.fail_closed_cash = True
    elif cfg.risk.cash_provider is not None and cfg.mode in ("paper", "live"):
        risk_manager.bind_cash_provider(cfg.risk.cash_provider)
        risk_manager.fail_closed_cash = True
```

Add the helper near the top of `startup.py` (after the imports, before `boot`):

```python
def _broker_available_cash(broker: Any) -> Decimal | None:
    """Available cash from broker funds, or None when it cannot be read.

    None means *unknown*, which the fail-closed cash gate treats as a stop.
    """
    account = broker.get_account()
    balance = getattr(account, "balance", None)
    if balance is None:
        return None
    return Decimal(str(balance.amount))
```

Ensure `Decimal` is imported in `startup.py` (it already imports `Decimal` at line 17 per the current file).

- [ ] **Step 7: Write the live-anchor test**

Create `trading/tests/runtime/test_live_cash_anchor.py`:

```python
"""Live boot binds a real cash provider and anchors the account balance."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain import BrokerId
from tradex_domain.execution import Account
from tradex_domain.value_objects import AccountId, Money
from tradex_config.schema import AppConfig, PersistenceConfig


def _broker_with_cash(cash: Decimal | None) -> MagicMock:
    broker = MagicMock()
    backend = MagicMock()
    backend.subscribe_orders = MagicMock()
    broker.stream_backend.return_value = backend
    broker.get_orderbook.return_value = []
    broker.get_positions.return_value = []
    broker.master_loader = None
    balance = None if cash is None else Money(amount=cash)
    broker.get_account.return_value = Account(
        account_id=AccountId("acct-1"), balance=balance,
    )
    return broker


def _live_cfg(tmp_path) -> AppConfig:
    return AppConfig(
        mode="live",
        broker_id=BrokerId.DHAN,
        live_enabled=True,
        persistence=PersistenceConfig(path=str(tmp_path / "orders.db")),
    )


def test_live_boot_binds_cash_provider_from_broker(monkeypatch, tmp_path) -> None:
    """The gate must read broker funds, not sit unbound."""
    from tradex_trading.runtime import startup as sm

    broker = _broker_with_cash(Decimal("500000"))
    monkeypatch.setattr("tradex_runtime.live.build_broker_from_env", lambda *a, **k: broker)
    session = sm.boot(_live_cfg(tmp_path))
    try:
        risk = session.engine._risk
        assert risk.cash_provider_bound is True
        assert risk.fail_closed_cash is True
        assert risk._cash_provider() == Decimal("500000")
    finally:
        session.stop()


def test_live_boot_denies_buy_when_funds_unreadable(monkeypatch, tmp_path) -> None:
    """An unreadable balance must stop admission, not skip the gate."""
    from tradex_trading.runtime import startup as sm

    broker = _broker_with_cash(None)
    monkeypatch.setattr("tradex_runtime.live.build_broker_from_env", lambda *a, **k: broker)
    session = sm.boot(_live_cfg(tmp_path))
    try:
        risk = session.engine._risk
        assert risk.cash_provider_bound is True
        assert risk._cash_provider() is None
    finally:
        session.stop()


def test_paper_boot_binds_cash_provider(monkeypatch) -> None:
    from tradex_trading.runtime import startup as sm

    session = sm.boot(AppConfig(mode="paper"))
    try:
        assert session.engine._risk.cash_provider_bound is True
        assert session.engine._risk.fail_closed_cash is True
    finally:
        session.stop()
```

- [ ] **Step 8: Run the new live tests**

```bash
.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q trading/tests/runtime/test_live_cash_anchor.py trading/tests/runtime/test_boot_cash_wiring.py
```

Expected: pass. `test_boot_cash_wiring.py` asserts the *unbound* default for a config without a provider — if it now fails, update it to the new fail-closed expectation (that is the intended behavior change, not a regression).

- [ ] **Step 9: Full-suite gate**

```bash
.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q --timeout=300
```

Expected: `0 failed`, count ≥ 3761. Fix only failures caused by this change.

---

### Task 3: Append the fill before mutating memory

On the fill path (`engine.py:595-613`) position, cash, and fee are mutated *before* `_append`. If the store write fails, `_append` trips the kill switch but the in-memory book has already moved, so the cache and the log permanently disagree and a restart rebuilds a book missing that fill. Inverting the order makes the event authoritative and the cache a projection.

**Files:**
- Modify: `execution/src/tradex_execution/engine.py:595-616` (sync fill path), `:744-773` (live-fill bridge)
- Test: `trading/tests/execution/test_append_before_mutate.py` (create)

**Interfaces:**
- Consumes: `_apply_fee(fill) -> Decimal` (already returns the charged amount from Task 2's predecessor work), `OrderFilled(fill, fee_amount)`, `_append(event) -> None`.
- Produces: no new public API. Behavioral contract: a failed durable append leaves the in-memory book untouched.

- [ ] **Step 1: Write the failing test**

Create `trading/tests/execution/test_append_before_mutate.py`:

```python
"""A fill that cannot be persisted must not move the in-memory book."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_trading.execution.cash_ledger import CashSnapshot
from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fees import FeeCalculator
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


def test_failed_persist_leaves_positions_and_cash_untouched() -> None:
    cache = TradingCache()
    store = InMemoryEventStore()
    engine = ExecutionEngine(
        bus=_bus(),
        fill_source=SimulatedFillSource(),
        cache=cache,
        event_store=store,
        cash=Decimal("100000"),
        fee_calculator=FeeCalculator(),
        require_durable_events=True,
    )

    # The fill persists, then the durable write of the fill event fails.
    original_append = store.append
    state = {"failed": False}

    def flaky(event):
        if type(event).__name__ == "OrderFilled" and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("disk full")
        return original_append(event)

    store.append = flaky  # type: ignore[method-assign]

    engine.submit(_request())

    assert state["failed"] is True, "the durable write never failed"
    # The book must not have moved, because the event never landed.
    assert cache.all_positions() == []
    assert engine.cash_snapshot() == CashSnapshot(
        cash=Decimal("100000"), total_fees=Decimal("0"),
    )
```

> Imports go through the `tradex_trading.*` shims here because the shim re-exports the *same* class objects for plain imports (`is` identity holds for `from X import Y`); only **`monkeypatch.setattr` string targets** are order-sensitive. Prefer the authority path when in doubt.

- [ ] **Step 2: Run it and confirm it fails**

```bash
.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q trading/tests/execution/test_append_before_mutate.py
```

Expected: FAIL — positions/cash moved even though the append failed.

- [ ] **Step 3: Invert the order on the sync fill path**

In `execution/src/tradex_execution/engine.py`, replace the block at lines 595-613 so the fee is computed, the event is appended, and only then is memory mutated:

```python
        if fill is not None:
            # Guard: skip position update if fill source owns projection
            # (PaperBroker projects positions itself). Idempotent against
            # _apply_fill: the order is FILLED before OrderFilled is published.
            charged_fee = Decimal("0")
            if not getattr(self._fill, "position_projection_owned", False):
                # Compute the fee first: it must travel on the event, and the
                # capped-brokerage accrual means it cannot be recomputed later.
                charged_fee = self._compute_fee(fill)
            _filled_event = OrderFilled(fill=fill, fee_amount=charged_fee)
            # Persist BEFORE mutating. The event is authoritative and the cache
            # is a projection: a crash or a failed write must not leave the two
            # disagreeing, or a restart rebuilds a book missing this fill.
            self._append(_filled_event)
            if not getattr(self._fill, "position_projection_owned", False):
                self._position_manager.on_fill(fill)
                self._apply_cash_fill(fill)
                self._apply_fee(fill)
            self._order_manager.on_order_filled(order, fill)
            # G3: record the fingerprint in the applied-fills set BEFORE
            # publishing so that any re-publish from the live-fill bridge
            # (which subscribes to OrderFilled) is short-circuited by
            # the dedup check in _apply_fill.
            self._record_applied_fill(fill)
            self._bus.publish(_filled_event)
```

- [ ] **Step 4: Split fee computation from fee application**

`_apply_fee` currently both computes and applies. Split it so the fee can be known before the append. Replace the existing `_apply_fee` (around line 660) with:

```python
    def _compute_fee(self, fill: Fill) -> Decimal:
        """Compute *fill*'s fee and advance the capped-brokerage accrual.

        Separate from application so the charged amount can be persisted with
        the event before any state moves.
        """
        if self._fee_calculator is None:
            return Decimal("0")
        oid = fill.order_id.value if hasattr(fill.order_id, "value") else str(fill.order_id)
        with self._brokerage_lock:
            accrued = self._brokerage_accrued.get(oid, Decimal("0"))
            fee, self._brokerage_accrued[oid] = self._fee_calculator.calculate_capped(
                fill, accrued,
            )
        return fee.amount

    def _apply_fee(self, fill: Fill, charged: Decimal) -> None:
        """Deduct the already-computed fee from P&L and cash.

        Failures propagate loudly — a silently-swallowed fee bug is exactly the
        accounting divergence the parity work exists to prevent.
        """
        if charged <= 0:
            return
        fee = Money(amount=charged)
        self._position_manager.on_fee(fill, fee)
        if self._cash_ledger is not None:
            self._cash_ledger.on_fee(fee)
```

- [ ] **Step 5: Apply the same inversion on the live-fill bridge**

At `engine.py:744-773`, replace the mutate-then-append sequence with compute → append → mutate:

```python
        charged_fee = Decimal("0")
        owns_projection = getattr(self._fill, "position_projection_owned", False)
        if not owns_projection:
            charged_fee = self._compute_fee(fill)
        filled_event = OrderFilled(fill=fill, fee_amount=charged_fee)
        # Persist before mutating — same invariant as the sync path.
        self._append(filled_event)
        if not owns_projection:
            self._position_manager.on_fill(fill)
            self._apply_cash_fill(fill)
            self._apply_fee(fill, charged_fee)
        if existing is not None:
            self._order_manager.on_order_filled(existing, fill)
        else:
            self._cache.update_order(
                Order(
                    order_id=fill.order_id,
                    instrument=fill.instrument,
                    side=fill.side,
                    order_type=OrderType.MARKET,
                    quantity=fill.quantity,
                    price=fill.price,
                    time_in_force=TimeInForce.DAY,
                    status=OrderStatus.FILLED,
                    filled_quantity=fill.quantity,
                    correlation_id=getattr(fill, "correlation_id", None),
                    tag=getattr(fill, "tag", None),
                )
            )
        log.info(
            "Inbound fill applied: %s qty=%s price=%s (engine fill bridge)",
            fill.instrument.symbol, fill.quantity.value, fill.price.value,
        )
```

(Preserve whatever `log.info` arguments the current code uses; only the ordering and the `_append` placement change.)

- [ ] **Step 6: Run the fill, recovery, and parity suites**

```bash
.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q trading/tests/execution trading/tests/parity trading/tests/runtime
```

Expected: all pass. The parity suites are the real check here — `test_execution_projection_parity.py`, `test_execution_cost_parity.py`, and `test_bracket_backtest_parity.py` all compare fills and net P&L and must be unchanged by the reordering.

- [ ] **Step 7: NEGATIVE CONTROL — restore the old order and confirm the test catches it**

```bash
cp execution/src/tradex_execution/engine.py /tmp/eng.bak
# Swap append back to after the mutations (defect under test):
.venv/bin/python - <<'EOF'
p='execution/src/tradex_execution/engine.py'; s=open(p).read()
s=s.replace("""            self._append(_filled_event)
            if not getattr(self._fill, "position_projection_owned", False):
                self._position_manager.on_fill(fill)""","""            if not getattr(self._fill, "position_projection_owned", False):
                self._position_manager.on_fill(fill)""",1)
s=s.replace("""                self._apply_fee(fill, charged_fee)
            self._order_manager.on_order_filled(order, fill)""","""                self._apply_fee(fill, charged_fee)
            self._append(_filled_event)
            self._order_manager.on_order_filled(order, fill)""",1)
open(p,'w').write(s)
EOF
.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q trading/tests/execution/test_append_before_mutate.py   # MUST FAIL
cp /tmp/eng.bak execution/src/tradex_execution/engine.py
diff /tmp/eng.bak execution/src/tradex_execution/engine.py && echo "RESTORED IDENTICAL"
```

Expected: the test FAILS with the old ordering, passes after restore.

- [ ] **Step 8: Full-suite gate**

```bash
.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q --timeout=300
```

Expected: `0 failed`.

---

### Task 4: Crash-restart and cross-mode parity (deferred follow-ups)

These close the original T1.4/T1.5 and are **not** part of this batch — listed so they are not lost.

- **T1.4** — extend `trading/tests/execution/test_event_store_reconstruction.py` so the *replay* mode's projection is compared against the same live-engine rebuild, not just live-vs-live. `execution_projection()` (`execution/src/tradex_execution/projection.py:16`) still has zero production consumers and is not exported from `tradex_execution/__init__.py`; wiring it into one route is what makes it earn its place.
- **T1.5** — a crash-restart test that rebuilds from a real `SQLiteEventStore` and asserts boot stays not-READY until broker reconciliation passes. Note the live-fill bridge still rebuilds orders but not marks (`mark_price`/`unrealized_pnl` are lost), so reconcile must not compare fields the fold cannot restore.

## Verification Summary (run all four at the end)

```bash
.venv/bin/python -m pytest -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q --timeout=300
cd frontend && npm run typecheck && npm run build && npm run e2e
```

Expected: `0 failed` on pytest; typecheck clean; build succeeds; Playwright all-pass with a captured summary.
