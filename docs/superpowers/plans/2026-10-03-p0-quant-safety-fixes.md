# P0 Quant Safety Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three highest-money-risk quant gaps in TradeX v4: the naked-short hole in paper/live, the D1-hardcoded scanner, and the raw-return default score in grid search.

**Architecture:** All three fixes are additive and strangler-safe. The short gate is a new `RiskManager` policy flag (class default `True` preserves every existing constructor; `RiskConfig` default `False` flips the platform boundary to safe). The scanner timeframe is a new defaulted `ScannerDefinition` field threaded through the existing `run → _scan → _evaluate → _history/_cache_key` path. The grid-search score is a module-level `_default_score` helper replacing both `total_return` default sites.

**Tech Stack:** Python 3.12, pytest, Decimal money, import-linter (enforced in `pyproject.toml`), ruff (line-length 100, complexity ratchet).

## Global Constraints

- Test command from repo root: `.venv/bin/python -m pytest <paths> -q`
- Money is `Decimal` everywhere; never float for price/quantity/P&L.
- Import-linter contracts (`pyproject.toml:148-388`): `tradex_execution` may import only `{tradex_domain, tradex_reactive, tradex_observability}`; `tradex_strategy` may import only `{tradex_domain, tradex_analytics, tradex_market_data}`; `tradex_config` only `tradex_domain`. No new cross-package edges.
- Ruff ratchet (`pyproject.toml:86-109`): no new C901/PLR0912/PLR0915 violations; line length ≤ 100.
- Each task ends with a green test run and a single commit.
- Out of scope (deliberate, see review): `walk_forward.py`'s own "Defaults to total_return" in-sample scoring (WFO already validates OOS per fold; changing it alters fold-promotion semantics — separate plan), shared analytics cache, strategy components, slippage calibration.

---

### Task 1: Short-selling policy gate

**Files:**
- Modify: `execution/src/tradex_execution/risk.py:49-67` (constructor), `:73` (store), `:423-425` (gate in `check()`), `:774-814` (new `_opens_short` next to `_increases_exposure`)
- Modify: `config/src/tradex_config/schema.py:150` (new `RiskConfig.allow_short` field, before `__post_init__` at `:152`)
- Modify: `runtime/src/tradex_runtime/startup.py:637-659` (pass `allow_short=cfg.risk.allow_short`)
- Modify: `interfaces/src/tradex_interfaces/routes/chart.py:227` (`RiskManager(allow_short=False)` for live-parity in UI backtests)
- Test: `trading/tests/execution/test_short_gate.py` (create)

**Interfaces:**
- Consumes: `OrderRequest(instrument, side, order_type, quantity, price, time_in_force)` (`domain/execution.py:69`); `Position(instrument, quantity, avg_price, realized_pnl, unrealized_pnl)` with **signed** `quantity` (`domain/execution.py:321-336`, `is_short` ⇔ `quantity.value < 0`); `RiskManager._positions_provider` (already bound, `risk.py:104`); `RiskManager._deny(reason, now=now)` (`risk.py:425`); `RiskManager.rejected_count` (`risk.py:816`).
- Produces: `RiskManager(allow_short: bool = True)` — keyword-only, so all ~100 existing constructors are source-compatible; deny reason string `"short_selling_disabled"`; `RiskConfig.allow_short: bool = False`.

**Behavior contract:** a SELL that only trims or closes a long is a *reduction* and stays allowed; a SELL that flips the book short or deepens an existing short is denied when `allow_short=False`. A BUY is never gated by this policy. With no `positions_provider`, a SELL cannot be proven to be a reduction → fail closed (deny), matching `_increases_exposure`'s conservative branch.

- [ ] **Step 1: Write the failing test**

Create `trading/tests/execution/test_short_gate.py`:

```python
"""Short-selling policy gate (P0).

A SELL that only trims or closes a long is a reduction and stays
allowed; a SELL that flips the book short or deepens a short is
denied when ``allow_short=False``. Each test names its negative
control, mirroring test_risk_budget_and_marks.py.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_config.schema import RiskConfig
from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest, Position
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Money, Price, Quantity
from tradex_execution.risk import RiskManager

_INST = Equity.of("NSE", "TEST")


def _request(side: OrderSide, qty: str = "100") -> OrderRequest:
    return OrderRequest(
        instrument=_INST,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )


def _position(qty: str = "0") -> Position:
    return Position(
        instrument=_INST,
        quantity=Quantity(value=Decimal(qty)),
        avg_price=Price(value=Decimal("100")),
        realized_pnl=Money(amount=Decimal("0")),
        unrealized_pnl=Money(amount=Decimal("0")),
    )


def _rm(allow_short: bool = False, qty: str = "0") -> RiskManager:
    return RiskManager(
        allow_short=allow_short,
        positions_provider=lambda: [_position(qty)],
    )


def test_sell_against_flat_book_is_denied() -> None:
    rm = _rm(allow_short=False, qty="0")
    assert rm.check(_request(OrderSide.SELL)) is False


def test_sell_trimming_a_long_is_allowed() -> None:
    rm = _rm(allow_short=False, qty="100")
    assert rm.check(_request(OrderSide.SELL, qty="40")) is True


def test_sell_closing_a_long_exactly_is_allowed() -> None:
    rm = _rm(allow_short=False, qty="100")
    assert rm.check(_request(OrderSide.SELL, qty="100")) is True


def test_sell_flipping_long_to_short_is_denied() -> None:
    rm = _rm(allow_short=False, qty="100")
    assert rm.check(_request(OrderSide.SELL, qty="150")) is False


def test_sell_deepening_a_short_is_denied() -> None:
    rm = _rm(allow_short=False, qty="-100")
    assert rm.check(_request(OrderSide.SELL, qty="50")) is False


def test_buy_is_never_the_short_gate() -> None:
    rm = _rm(allow_short=False, qty="0")
    assert rm.check(_request(OrderSide.BUY)) is True


def test_allow_short_true_keeps_legacy_behaviour() -> None:
    rm = _rm(allow_short=True, qty="0")
    assert rm.check(_request(OrderSide.SELL)) is True


def test_no_position_book_fails_closed() -> None:
    rm = RiskManager(allow_short=False)
    assert rm.check(_request(OrderSide.SELL)) is False


def test_deny_is_counted() -> None:
    rm = _rm(allow_short=False, qty="0")
    rm.check(_request(OrderSide.SELL))
    assert rm.rejected_count == 1


def test_risk_config_default_denies_shorts() -> None:
    assert RiskConfig().allow_short is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest trading/tests/execution/test_short_gate.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'allow_short'`

- [ ] **Step 3: Implement the gate**

In `execution/src/tradex_execution/risk.py`:

3a. Constructor — add after `live_orders_enabled: bool = True,` (line 55):

```python
        #: Short-selling policy. When False, a SELL that would open or
        #: grow a short position is denied; a SELL that only trims or
        #: closes a long stays allowed (flattening is never blocked).
        #: Class-level default True preserves every existing direct
        #: constructor; ``boot()`` binds ``RiskConfig.allow_short``
        #: (default False) so the platform boundary is safe by default.
        allow_short: bool = True,
```

3b. Store — add after `self._live_orders_enabled = live_orders_enabled` (line 73):

```python
        self._allow_short = allow_short
```

3c. Gate in `check()` — add immediately after the master gate block (after line 425, `return self._deny("live_orders_disabled", now=now)` and its closing brace):

```python
            # Short-selling policy gate. A SELL that would open or
            # grow a short is denied unless the account is opted in.
            # A SELL that only reduces a long stays allowed: this gate
            # never blocks flattening. Checked early because it is a
            # hard policy, not a limit that scales with the book.
            if (
                request.side is OrderSide.SELL
                and not self._allow_short
                and self._opens_short(request)
            ):
                return self._deny("short_selling_disabled", now=now)
```

3d. New method — add after `_increases_exposure` (after line 814):

```python
    def _opens_short(self, request: OrderRequest) -> bool:
        """True when a SELL would open or grow a short position.

        Uses the same per-side gross arithmetic as
        :meth:`_increases_exposure`: a SELL of 40 against a long 100
        leaves gross short at 0 (a reduction, allowed); a SELL of 150
        against a long 100 grows gross short 0 → 50 (an open, gated).
        Without a position book a SELL cannot be proven to be a
        reduction, so this fails closed — the same conservative
        branch :meth:`_increases_exposure` takes.
        """
        if self._positions_provider is None:
            return True
        signed = -request.quantity.value
        for pos in self._positions_provider():
            if pos.instrument == request.instrument:
                resulting = pos.quantity.value + signed
                short_before = max(-pos.quantity.value, Decimal(0))
                short_after = max(-resulting, Decimal(0))
                return short_after > short_before
        # No position for this instrument: any SELL opens a short.
        return True
```

In `config/src/tradex_config/schema.py`, add after the `risk_fraction` field (line 150), before `__post_init__`:

```python
    #: Short-selling policy. False (the platform default) denies any
    #: SELL that would open or grow a short position; a SELL that only
    #: reduces a long stays allowed. ``boot()`` passes this to
    #: ``RiskManager``. Operators who trade short-enabled books set
    #: ``risk.allow_short: true``.
    allow_short: bool = False
```

In `runtime/src/tradex_runtime/startup.py`, add after `max_drawdown_pct=limits["max_drawdown_pct"],` (line 643) inside the `RiskManager(...)` call:

```python
        allow_short=cfg.risk.allow_short,
```

In `interfaces/src/tradex_interfaces/routes/chart.py:227`, replace `risk = RiskManager()` with:

```python
    # allow_short=False matches the session default (RiskConfig), so a
    # UI backtest cannot show profits from shorts the live session
    # would have rejected — the parity rule this route's cost gates
    # already enforce.
    risk = RiskManager(allow_short=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest trading/tests/execution/test_short_gate.py -q`
Expected: 10 passed

- [ ] **Step 5: Run the surrounding suites to prove no regression**

The risk-manager change must not disturb the ~100 existing `RiskManager()` constructors (class default `True` keeps behavior) nor the boot path.

Run: `.venv/bin/python -m pytest trading/tests/execution runtime/tests tests/runtime_audit -q`
Expected: all pass (existing boot tests exercise the `startup.py` wiring; a signature error would fail here)

- [ ] **Step 6: Commit**

```bash
git add execution/src/tradex_execution/risk.py config/src/tradex_config/schema.py runtime/src/tradex_runtime/startup.py interfaces/src/tradex_interfaces/routes/chart.py trading/tests/execution/test_short_gate.py
git commit -m "fix(risk): deny naked shorts by default (allow_short policy gate)"
```

---

### Task 2: Scanner timeframe threading

**Files:**
- Modify: `domain/src/tradex_domain/strategy.py:9` (import), `:69-73` (`ScannerDefinition.timeframe` field)
- Modify: `strategy/src/tradex_strategy/core/scanner.py:182-196` (`_cache_key`), `:349-406` (`_evaluate`), `:439-455` (`_history`)
- Test: `trading/tests/strategy/test_scanner_timeframe.py` (create)

**Interfaces:**
- Consumes: `ScannerDefinition(universe, conditions, rank_by, limit)` — all fields defaulted, so an appended defaulted field is backward-compatible for every keyword constructor (`extensions/scanners/*.py`, tests). `Timeframe` is already imported in `scanner.py` (it references `Timeframe.D1` at `:410` and `:451`).
- Produces: `ScannerDefinition.timeframe: Timeframe = Timeframe.D1`; `ScannerEngine._history(instrument, days, timeframe)`, `_cache_key(definition, instrument, start, end, timeframe)`; `_evaluate` reads `definition.timeframe`.

**Behavior contract:** the default (D1) preserves today's behavior exactly. A definition carrying `timeframe=Timeframe.M5` makes the engine fetch and evaluate 5-minute bars. The cache key includes the timeframe, so a D1 scan and an M5 scan of the same definition never share a cache entry.

- [ ] **Step 1: Write the failing test**

Create `trading/tests/strategy/test_scanner_timeframe.py`:

```python
"""Scanner timeframe threading (P0).

A ScannerDefinition declares the bar timeframe it evaluates on.
The default (D1) preserves the engine's historical daily-scan
behaviour; an intraday definition must make the engine fetch and
evaluate that timeframe, and the scan cache must key on it.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import (
    Candle,
    Condition,
    Equity,
    HistoricalSeries,
    OHLC,
    Price,
    Quantity,
    ScannerDefinition,
    Timeframe,
)
from tradex_strategy.core.scanner import ScannerEngine


def _eq(symbol: str = "RELIANCE") -> Equity:
    return Equity.of("NSE", symbol)


class _RecordingMarket:
    """Provider that records the timeframe of every history call."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, Timeframe]] = []

    def history(self, instrument, timeframe, start, end) -> HistoricalSeries:
        self.calls.append((instrument, timeframe))
        return HistoricalSeries(
            instrument=instrument,
            timeframe=timeframe,
            candles=[],
        )


def _condition() -> Condition:
    return Condition(name="close", params={}, operator=">", threshold=1.0)


def test_definition_defaults_to_d1() -> None:
    definition = ScannerDefinition(universe=[_eq()])
    assert definition.timeframe is Timeframe.D1


def test_engine_fetches_definition_timeframe() -> None:
    market = _RecordingMarket()
    engine = ScannerEngine(market=market, min_call_interval=0.0)
    definition = ScannerDefinition(
        universe=[_eq()],
        conditions=[_condition()],
        timeframe=Timeframe.M5,
    )
    engine.run(definition)
    assert market.calls, "engine never called the provider"
    assert all(tf is Timeframe.M5 for _, tf in market.calls)


def test_default_definition_still_fetches_d1() -> None:
    market = _RecordingMarket()
    engine = ScannerEngine(market=market, min_call_interval=0.0)
    engine.run(ScannerDefinition(universe=[_eq()], conditions=[_condition()]))
    assert market.calls
    assert all(tf is Timeframe.D1 for _, tf in market.calls)


def test_cache_key_separates_timeframes() -> None:
    """A D1 scan and an M5 scan of the same definition must not share
    a cache entry — both hit the provider."""
    market = _RecordingMarket()
    engine = ScannerEngine(market=market, min_call_interval=0.0)
    shared = dict(universe=[_eq()], conditions=[_condition()])
    engine.run(ScannerDefinition(timeframe=Timeframe.D1, **shared))
    engine.run(ScannerDefinition(timeframe=Timeframe.M5, **shared))
    timeframes = {tf for _, tf in market.calls}
    assert timeframes == {Timeframe.D1, Timeframe.M5}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_scanner_timeframe.py -q`
Expected: FAIL — `TypeError: ScannerDefinition() got an unexpected keyword argument 'timeframe'`

- [ ] **Step 3: Implement**

In `domain/src/tradex_domain/strategy.py`:

3a. Line 9 — change the import:

```python
from tradex_domain.enums import OrderSide, Timeframe
```

3b. `ScannerDefinition` (lines 69-73) — append the field:

```python
@dataclass(frozen=True, slots=True)
class ScannerDefinition(Serializable):
    universe: list[Instrument] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    rank_by: str = "score"
    limit: int = 20
    #: Bar timeframe the scanner evaluates on. D1 preserves the
    #: engine's historical daily-scan behaviour; an intraday value
    #: (M5/M15) makes ScannerEngine fetch and evaluate that timeframe.
    timeframe: Timeframe = Timeframe.D1
```

In `strategy/src/tradex_strategy/core/scanner.py`:

3c. `_cache_key` (lines 182-196) — take `timeframe` and key on it:

```python
    def _cache_key(
        self,
        definition: ScannerDefinition,
        instrument: Any,
        start: datetime,
        end: datetime,
        timeframe: Timeframe,
    ) -> tuple:
        return (
            self._definition_id(definition),
            str(instrument),
            start.isoformat(),
            end.isoformat(),
            str(timeframe),
        )
```

3d. `_evaluate` (line 355) — derive the timeframe from the definition and pass it to both `_history` (line 356) and `_cache_key` (line 366):

```python
        timeframe = definition.timeframe
        series = self._history(instrument, days, timeframe)
```
```python
            cache_key = self._cache_key(
                definition, instrument, start, end, timeframe
            )
```

3e. `_history` (lines 439-455) — take `timeframe` and use it in the provider call:

```python
    def _history(
        self,
        instrument: Any,
        days: int,
        timeframe: Timeframe,
    ) -> HistoricalSeries | None:
        ...
        return self._market.history(instrument, timeframe, start, end)
```

(Keep the provider try/except and the `None` return exactly as they are — only the signature and the `Timeframe.D1` literal change.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_scanner_timeframe.py -q`
Expected: 4 passed

- [ ] **Step 5: Run the scanner + discovery suites**

Run: `.venv/bin/python -m pytest trading/tests/strategy -q`
Expected: all pass (the D1 default makes every existing scanner definition behave identically)

- [ ] **Step 6: Commit**

```bash
git add domain/src/tradex_domain/strategy.py strategy/src/tradex_strategy/core/scanner.py trading/tests/strategy/test_scanner_timeframe.py
git commit -m "feat(scanner): thread declared timeframe through scan history and cache"
```

---

### Task 3: Risk-adjusted default score for grid search

**Files:**
- Modify: `replay/src/tradex_replay/optimization.py:1-10` (imports + `_default_score`), `:27-29` (`__post_init__`), `:63` (docstring), `:93` (`_evaluate`)
- Test: `replay/tests/test_optimization_scoring.py` (create)

**Interfaces:**
- Consumes: `BacktestResult(total_return, sharpe, max_drawdown, num_trades, ...)` — first four fields required (`replay/backtest.py:206-220`); `sharpe` is already computed on every run.
- Produces: `_default_score(result: BacktestResult) -> float` — `result.sharpe` when finite, else `0.0`. Used by both default sites; an explicit `score_fn=` still overrides.

**Behavior contract:** the default ranking is risk-adjusted (Sharpe), so a high-return/high-drawdown grid point no longer beats a steadier one. Non-finite Sharpe (flat or single-observation equity curve) scores `0.0` — never NaN, which would poison `max()`/`sorted()`.

- [ ] **Step 1: Write the failing test**

Create `replay/tests/test_optimization_scoring.py`:

```python
"""grid_search default scoring (P0).

The default score must be risk-adjusted: raw total_return ranks a
high-return / high-drawdown grid point above a steadier one, which is
the overfitting trap grid search exists to escape.
"""

from __future__ import annotations

from tradex_replay.backtest import BacktestResult
from tradex_replay.optimization import (
    OptimizationResult,
    _default_score,
    grid_search,
)


def _result(
    total_return: float,
    sharpe: float,
    max_drawdown: float = 0.05,
) -> BacktestResult:
    return BacktestResult(
        total_return=total_return,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        num_trades=1,
    )


def test_default_score_is_sharpe() -> None:
    assert _default_score(_result(0.10, 1.5)) == 1.5


def test_default_score_nan_sharpe_is_zero() -> None:
    assert _default_score(_result(0.99, float("nan"))) == 0.0


def test_default_score_inf_sharpe_is_zero() -> None:
    assert _default_score(_result(0.99, float("inf"))) == 0.0


def test_optimization_result_default_score_is_sharpe() -> None:
    opt = OptimizationResult(params={"p": 1}, result=_result(0.10, 2.0))
    assert opt.score == 2.0


def test_grid_search_ranks_sharpe_over_return() -> None:
    """The steady 20%/2.0-Sharpe run must beat the 90%/0.3-Sharpe run."""
    hot = _result(0.90, 0.3, max_drawdown=0.50)
    steady = _result(0.20, 2.0)
    library = {"hot": hot, "steady": steady}
    gs = grid_search(
        {"name": ["hot", "steady"]},
        lambda params: library[params["name"]],
    )
    assert gs.best is not None
    assert gs.best.params == {"name": "steady"}


def test_explicit_score_fn_still_wins() -> None:
    hot = _result(0.90, 0.3)
    steady = _result(0.20, 2.0)
    library = {"hot": hot, "steady": steady}
    gs = grid_search(
        {"name": ["hot", "steady"]},
        lambda params: library[params["name"]],
        score_fn=lambda r: r.total_return,
    )
    assert gs.best is not None
    assert gs.best.params == {"name": "hot"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest replay/tests/test_optimization_scoring.py -q`
Expected: FAIL — `ImportError: cannot import name '_default_score'`

- [ ] **Step 3: Implement**

In `replay/src/tradex_replay/optimization.py`:

3a. Add `import math` to the stdlib imports at the top (after `import itertools`).

3b. Add after the `GridSearchResult` class (before `grid_search`):

```python
def _default_score(result: BacktestResult) -> float:
    """Risk-adjusted default score: Sharpe, floored to 0.0 when not finite.

    Raw ``total_return`` as the default ranks a high-return,
    high-drawdown grid point above a steadier one — the classic
    overfitting trap. Sharpe is already computed on every
    ``BacktestResult``, so the default ranks on risk-adjusted return.
    A non-finite Sharpe (flat or single-observation equity curve)
    scores 0.0 rather than poisoning ``max``/``sorted`` with NaN.
    """
    return result.sharpe if math.isfinite(result.sharpe) else 0.0
```

3c. `OptimizationResult.__post_init__` (lines 26-29) — replace the default:

```python
    def __post_init__(self) -> None:
        # Default score is risk-adjusted Sharpe (see _default_score).
        if self.score == 0.0:
            object.__setattr__(self, "score", _default_score(self.result))
```

3d. `grid_search` docstring (line 63) — change:

```python
        Function to score a BacktestResult. Defaults to risk-adjusted
        Sharpe (see ``_default_score``).
```

3e. `_evaluate` (line 93) — replace the fallback:

```python
            score = (
                score_fn(bt_result) if score_fn else _default_score(bt_result)
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest replay/tests/test_optimization_scoring.py -q`
Expected: 6 passed

- [ ] **Step 5: Run the replay suite**

Run: `.venv/bin/python -m pytest replay/tests -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add replay/src/tradex_replay/optimization.py replay/tests/test_optimization_scoring.py
git commit -m "fix(replay): score grid_search on risk-adjusted Sharpe by default"
```

---

## Self-Review

**1. Spec coverage:** P0-1 naked-short gate → Task 1 (unit gate + config default + boot wiring + UI-backtest parity). P0-2 scanner intraday timeframe → Task 2 (domain field + engine threading + cache key). P0-3 grid-search scoring → Task 3 (both default sites + explicit-override preserved). No spec requirement is unaddressed.

**2. Placeholder scan:** No TBD/TODO; every step contains the exact code and the exact command with expected output. The one deliberately-scoped-out item (`walk_forward.py` in-sample scoring) is named in Global Constraints with the reason.

**3. Type consistency:** `allow_short: bool` is spelled identically across `RiskManager.__init__`, `RiskConfig`, `startup.py`, `chart.py`, and the tests. `_default_score(result: BacktestResult) -> float` is the single helper consumed by both `__post_init__` and `_evaluate`. `timeframe: Timeframe` is the parameter name at every `ScannerEngine` site and on `ScannerDefinition`. `_opens_short` reuses `_increases_exposure`'s signed-quantity arithmetic (`risk.py:793-813`), so the two cannot drift apart.

**Behavior-change disclosure (Task 1):** live/paper sessions now reject short-opening SELLs by default (`RiskConfig.allow_short = False`). This is the intended fix; operators opt in with `risk.allow_short: true`. Research backtests are unchanged (`RiskManager`'s class default is `True`, and `BacktestEngine`'s own default constructor is untouched). Existing shipped strategies that emit SELL signals will see those orders rejected in paper/live until opted in — verified by the Task 1 suite's `test_allow_short_true_keeps_legacy_behaviour` negative control.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-10-03-p0-quant-safety-fixes.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
