# Phase 1 — Per-Symbol Event Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ReactiveStrategyEngine` deliver each market event only to the strategies subscribed to that instrument, instead of broadcasting every event to every strategy.

**Architecture:** Introduce a `SubscriptionRouter` (index of `(event_type, instrument_id) → strategy_ids`, plus a wildcard set per event type for universe-level subscribers). The engine builds the index at `register()` from each strategy's declared `instruments`, and each wrapper handler consults the router before evaluating the strategy. The guard is placed **after** the bar bookkeeping (`_bar_count += 1`, `_flush_pending`) so engine state evolves exactly as before — only the strategy call is skipped. Strategies that declare no `instruments` (e.g. the portfolio-style `MultiSymbolSmaCross`) get wildcard subscription, preserving today's behavior.

**Tech Stack:** Python 3.12, python, RxPY (existing bus), import-linter (enforced in `pyproject.toml`), ruff (line-length 100, complexity ratchet).

## Global Constraints

- Test command from repo root: `.venv/bin/python -m pytest <paths> -q`
- Import-linter contracts (`pyproject.toml`): `tradex_strategy` may import only `{tradex_domain, tradex_analytics, tradex_market_data}`. `routing.py` imports only `tradex_domain` — no new cross-package edges.
- Ruff ratchet: no new C901/PLR0912/PLR0915 violations; line length ≤ 100.
- **Behavior preservation (critical):** `_bar_count` and `_flush_pending` must evolve exactly as they do today. The routing guard in `_wrap_on_bar` goes AFTER `self._bar_count += 1` and AFTER `self._flush_pending(candle)`, BEFORE the strategy call. With N strategies registered, `_bar_count` still increments N times per candle (once per strategy handler, before any guard).
- **Backward compatibility:** a strategy that does not declare `instruments` receives every event (wildcard) — identical to today. The `Strategy` protocol is NOT modified (no `instruments` member added); the engine reads it via `getattr(strategy, "instruments", None)`.
- **Defensive instrument extraction:** events may lack an `instrument` (test fakes). The guard uses `getattr` and, when no instrument is present, delivers to all subscribers (cannot route without an instrument).
- Each step ends with a green test run and a single commit.
- Out of scope: timeframe-aware routing (Phase 3+), `StrategySpec` declaration model (Phase 3), changes to indicator math or execution.

---

### Task 1: SubscriptionRouter + engine routing + strategy declarations

**Files:**
- Create: `strategy/src/tradex_strategy/core/routing.py`
- Modify: `strategy/src/tradex_strategy/core/engine.py` (`__init__`, `register`, `unregister`, `dispose_all`, `_wrap_on_bar`, `_wrap_on_quote`, `_wrap_on_depth`, `_wrap_on_fill`)
- Modify: `strategy/src/tradex_strategy/extensions/strategies/study_base.py` (add `instruments` property)
- Modify: `strategy/src/tradex_strategy/extensions/strategies/sma_cross.py` (add `instruments` property)
- Modify: `strategy/src/tradex_strategy/extensions/strategies/mean_reversion.py` (add `instruments` property)
- Modify: `strategy/src/tradex_strategy/extensions/strategies/bracket_breakout.py` (add `instruments` property)
- Modify: `strategy/src/tradex_strategy/core/buy_and_hold.py` (add `instruments` property)
- Test: `trading/tests/strategy/test_routing.py` (create)

**Interfaces:**
- Consumes: `InstrumentId` (`tradex_domain.instruments`), `Candle`/`Quote`/`Depth`/`Fill` (`tradex_domain` / `tradex_domain.market`), `ReactiveStrategyEngine.register/unregister/dispose_all`, the wrapper handlers `_wrap_on_bar/_wrap_on_quote/_wrap_on_depth/_wrap_on_fill`.
- Produces: `StrategySubscription(event_type: type, instrument_id: InstrumentId | None = None)` (None = wildcard); `SubscriptionRouter` with `.subscribe(sub, strategy_id)`, `.unsubscribe(strategy_id)`, `.delivers(strategy_id, event_type, instrument_id) -> bool`; engine helper `_event_instrument_id(event) -> InstrumentId | None`; strategy property `instruments -> Iterable[InstrumentId]`.

**Behavior contract:**
- A strategy declaring `instruments = [X]` receives Candle/Quote/Depth only for instrument X. A strategy declaring nothing receives all events (wildcard).
- Fills route by owning strategy: a fill with `strategy_id=S` is delivered only to S; a fill with `strategy_id=None` (manual/legacy) is delivered to all (current behavior).
- `_bar_count` and `_flush_pending` are untouched by routing — they run for every strategy handler exactly as before the guard.
- `MultiSymbolSmaCross` declares no `instruments` → wildcard (it trades every instrument by design).

- [ ] **Step 1: Write the failing tests**

Create `trading/tests/strategy/test_routing.py`:

```python
"""Per-symbol event routing (Phase 1).

A strategy declaring ``instruments`` receives only that instrument's
events; a strategy declaring none receives everything (wildcard).
The routing guard must not disturb engine bookkeeping: ``_bar_count``
and ``_flush_pending`` run for every strategy handler exactly as before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import Candle, Equity, OHLC, Price, Quantity
from tradex_domain.market import Depth, Quote
from tradex_strategy.core.engine import ReactiveStrategyEngine
from tradex_strategy.core.routing import StrategySubscription, SubscriptionRouter


def _eq(symbol: str) -> Equity:
    return Equity.of("NSE", symbol)


def _candle(symbol: str, ts: datetime | None = None) -> Candle:
    return Candle(
        instrument=_eq(symbol),
        timeframe=None,  # set below
        ohlc=OHLC(
            open=Price(value=Decimal("100")),
            high=Price(value=Decimal("101")),
            low=Price(value=Decimal("99")),
            close=Price(value=Decimal("100")),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts or datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
    )


class _RecordingStrategy:
    """Strategy that records which events it received."""

    def __init__(self, strategy_id: str, instruments=None) -> None:
        self._id = strategy_id
        self._instruments = instruments
        self.bars: list = []
        self.quotes: list = []
        self.fills: list = []

    @property
    def strategy_id(self) -> str:
        return self._id

    @property
    def instruments(self):
        return self._instruments

    def on_bar(self, context, candle):
        self.bars.append(candle)
        return None

    def on_quote(self, context, quote):
        self.quotes.append(quote)
        return None

    def on_fill(self, context, fill) -> None:
        self.fills.append(fill)


class _FakeBus:
    """Minimal bus capturing subscribed handlers per event type."""

    def __init__(self) -> None:
        self.handlers: dict[type, list] = {}

    def of_type(self, event_type):
        return self

    def subscribe(self, handler):
        self.handlers.setdefault(type(handler), []).append(handler)
        return self

    def publish(self, event) -> None:
        for handler in self.handlers.get(type(event), []):
            handler(event)


def _engine(strategies: list[_RecordingStrategy]) -> ReactiveStrategyEngine:
    bus = _FakeBus()
    engine = ReactiveStrategyEngine(bus=bus, submit=lambda req: None)
    for s in strategies:
        engine.register(s)
    return engine


# --- router unit tests ------------------------------------------------------


def test_router_per_instrument_delivery() -> None:
    router = SubscriptionRouter()
    router.subscribe(StrategySubscription(Candle, _eq("RELIANCE").instrument_id), "s1")
    assert router.delivers("s1", Candle, _eq("RELIANCE").instrument_id) is True
    assert router.delivers("s1", Candle, _eq("TCS").instrument_id) is False


def test_router_wildcard_delivery() -> None:
    router = SubscriptionRouter()
    router.subscribe(StrategySubscription(Candle), "s1")
    assert router.delivers("s1", Candle, _eq("RELIANCE").instrument_id) is True
    assert router.delivers("s1", Candle, _eq("TCS").instrument_id) is True


def test_router_unsubscribe_stops_delivery() -> None:
    router = SubscriptionRouter()
    router.subscribe(StrategySubscription(Candle, _eq("RELIANCE").instrument_id), "s1")
    router.unsubscribe("s1")
    assert router.delivers("s1", Candle, _eq("RELIANCE").instrument_id) is False


# --- engine routing tests ---------------------------------------------------


def test_candle_reaches_only_bound_strategy() -> None:
    reliance = _RecordingStrategy("reliance", [_eq("RELIANCE").instrument_id])
    tcs = _RecordingStrategy("tcs", [_eq("TCS").instrument_id])
    engine = _engine([reliance, tcs])
    engine._bus.publish(_candle("RELIANCE"))
    assert len(reliance.bars) == 1
    assert len(tcs.bars) == 0


def test_candle_reaches_universal_strategy() -> None:
    universal = _RecordingStrategy("universal", None)
    engine = _engine([universal])
    engine._bus.publish(_candle("RELIANCE"))
    engine._bus.publish(_candle("TCS"))
    assert len(universal.bars) == 2


def test_bar_count_still_increments_for_all_handlers() -> None:
    """The guard is after bookkeeping: every strategy handler increments
    _bar_count, so with 2 strategies one candle increments it twice."""
    s1 = _RecordingStrategy("s1", [_eq("RELIANCE").instrument_id])
    s2 = _RecordingStrategy("s2", [_eq("TCS").instrument_id])
    engine = _engine([s1, s2])
    engine._bus.publish(_candle("RELIANCE"))
    assert engine._bar_count == 2


def test_quote_reaches_only_bound_strategy() -> None:
    reliance = _RecordingStrategy("reliance", [_eq("RELIANCE").instrument_id])
    tcs = _RecordingStrategy("tcs", [_eq("TCS").instrument_id])
    engine = _engine([reliance, tcs])
    quote = Quote(
        instrument=_eq("RELIANCE"),
        ltp=Price(value=Decimal("100")),
        timestamp=datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
    )
    engine._bus.publish(quote)
    assert len(reliance.quotes) == 1
    assert len(tcs.quotes) == 0


def test_fill_routes_by_owning_strategy() -> None:
    from tradex_domain.execution import Fill, OrderId

    s1 = _RecordingStrategy("s1", [_eq("RELIANCE").instrument_id])
    s2 = _RecordingStrategy("s2", [_eq("TCS").instrument_id])
    engine = _engine([s1, s2])
    fill = Fill(
        order_id=OrderId(value="o1"),
        instrument=_eq("RELIANCE"),
        side=None,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        strategy_id="s1",
    )
    engine._bus.publish(fill)
    assert len(s1.fills) == 1
    assert len(s2.fills) == 0
```

Note: `Candle` requires a `timeframe` — check the real constructor and set it to a valid `Timeframe` (e.g. `Timeframe.M1`) rather than `None`. `Fill.side` requires an `OrderSide` — set it to `OrderSide.BUY`. `Quote` may require more fields — inspect `tradex_domain.market.Quote` and `tradex_domain.execution.Fill` before finalizing the fakes, and adjust the constructors to match the real required fields.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_routing.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradex_strategy.core.routing'`

- [ ] **Step 3: Implement the router**

Create `strategy/src/tradex_strategy/core/routing.py`:

```python
"""Per-symbol event routing for the strategy engine.

Strategies declare the instruments they trade; the engine routes each
market event only to the strategies subscribed to that instrument.
Strategies that declare nothing (portfolio/multi-symbol strategies that
trade every instrument) receive a wildcard subscription — every event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tradex_domain.instruments import InstrumentId


@dataclass(frozen=True, slots=True)
class StrategySubscription:
    """One strategy's subscription to one event type.

    ``instrument_id=None`` is a wildcard: the strategy receives every
    instrument's events of that type.
    """

    event_type: type
    instrument_id: InstrumentId | None = None


class SubscriptionRouter:
    """Indexes strategy subscriptions by (event_type, instrument_id).

    A wildcard set per event type holds strategies subscribed to all
    instruments. ``delivers`` is the hot-path check the engine's wrapper
    handlers call before evaluating a strategy.
    """

    def __init__(self) -> None:
        self._index: dict[tuple[type, InstrumentId], set[str]] = {}
        self._wildcard: dict[type, set[str]] = {}

    def subscribe(self, sub: StrategySubscription, strategy_id: str) -> None:
        if sub.instrument_id is None:
            self._wildcard.setdefault(sub.event_type, set()).add(strategy_id)
        else:
            self._index.setdefault((sub.event_type, sub.instrument_id), set()).add(
                strategy_id
            )

    def unsubscribe(self, strategy_id: str) -> None:
        for ids in self._index.values():
            ids.discard(strategy_id)
        for ids in self._wildcard.values():
            ids.discard(strategy_id)

    def delivers(
        self, strategy_id: str, event_type: type, instrument_id: InstrumentId
    ) -> bool:
        if strategy_id in self._wildcard.get(event_id_type(event_type), ()):
            return True
        return strategy_id in self._index.get((event_type, instrument_id), ())


def event_instrument_id(event: Any) -> InstrumentId | None:
    """Best-effort instrument id of a market event; None when absent.

    Test fakes and some legacy events carry no instrument. Routing
    cannot key on a missing instrument, so the engine delivers those to
    every subscriber (conservative).
    """
    inst = getattr(event, "instrument", None)
    if inst is None:
        return None
    return getattr(inst, "instrument_id", None)
```

Fix the typo: `event_id_type(event_type)` should be `event_type`.

- [ ] **Step 4: Wire the engine**

In `strategy/src/tradex_strategy/core/engine.py`:

4a. Add the import at the top (with the other `tradex_strategy.core` imports):

```python
from tradex_strategy.core.routing import (
    StrategySubscription,
    SubscriptionRouter,
    event_instrument_id,
)
```

4b. `__init__` — add the router after `self._strategies: dict[str, Any] = {}` (line 118):

```python
        # Per-symbol event routing (Phase 1). Built at register() from
        # each strategy's declared instruments; consulted by the wrapper
        # handlers before a strategy is evaluated.
        self._router = SubscriptionRouter()
```

4c. `register()` — build the routing index from the strategy's declared instruments, before the bus subscriptions (insert between line 563 `self._strategies[strategy.strategy_id] = strategy` and the `from tradex_domain import Candle, Fill, Quote` import block):

```python
        # Build the routing index. Strategies declaring instruments are
        # routed per instrument; those declaring none (portfolio strategies
        # that trade everything) get a wildcard subscription.
        instruments = getattr(strategy, "instruments", None)
        if instruments:
            for inst in instruments:
                inst_id = getattr(inst, "instrument_id", inst)
                self._router.subscribe(
                    StrategySubscription(Candle, inst_id), strategy.strategy_id
                )
                self._router.subscribe(
                    StrategySubscription(Quote, inst_id), strategy.strategy_id
                )
                self._router.subscribe(
                    StrategySubscription(Depth, inst_id), strategy.strategy_id
                )
        else:
            self._router.subscribe(StrategySubscription(Candle), strategy.strategy_id)
            self._router.subscribe(StrategySubscription(Quote), strategy.strategy_id)
            self._router.subscribe(StrategySubscription(Depth), strategy.strategy_id)
```

(The `from tradex_domain import Candle, Fill, Quote` / `from tradex_domain.market import Depth` imports already in `register()` cover the event types.)

4d. `unregister()` — add `self._router.unsubscribe(strategy_id)` after `self._strategies.pop(strategy_id, None)` (line 581).

4e. `dispose_all()` — add `self._router.unsubscribe(strategy_id)` inside the loop (after line 596 `self._dispose_strategy(strategy_id)`), or clear the router after the loop.

4f. `_wrap_on_bar` — add the routing guard AFTER the bookkeeping, BEFORE the context creation. The current body (lines 188-208) becomes:

```python
        def handler(candle: Any) -> Any:
            if strategy.strategy_id in self._gated:
                return None
            self._bar_count += 1
            if self._fill_reference == "next_open":
                self._flush_pending(candle)
            # Routing guard (Phase 1): skip the strategy call for
            # instruments it is not subscribed to. Placed AFTER the
            # bookkeeping above so _bar_count and _flush_pending evolve
            # exactly as before — only the strategy evaluation is skipped.
            inst_id = event_instrument_id(candle)
            if inst_id is not None and not self._router.delivers(
                strategy.strategy_id, Candle, inst_id
            ):
                return None
            ctx = self._make_context(
                bar_count=self._bar_count,
                timestamp=getattr(candle, "timestamp", None),
            )
            result = strategy.on_bar(ctx, candle)
            self._maybe_publish_order(result, strategy, candle)
            return result
```

4g. `_wrap_on_quote` — add the guard after the gated check (line 394-395), before the context:

```python
            inst_id = event_instrument_id(quote)
            if inst_id is not None and not self._router.delivers(
                strategy.strategy_id, Quote, inst_id
            ):
                return None
```

4h. `_wrap_on_depth` — add the same guard after the gated check (line 405-406), before the context, using `Depth`.

4i. `_wrap_on_fill` — add fill routing by owning strategy after the gated check (line 417-418), before the context:

```python
            # Fills route by owning strategy, not instrument. A fill with
            # no strategy_id (manual/legacy) goes to all strategies.
            if fill.strategy_id is not None and fill.strategy_id != strategy.strategy_id:
                return None
```

- [ ] **Step 5: Declare instruments on the shipped strategies**

Add an `instruments` property to each single-instrument strategy class, returning the bound instrument's id. For `StudyStrategyBase` (covers BollingerBreakout, EmaRibbonPullback, SupertrendFlip, RsiReversal, MacdCross), add after the `instrument` property (study_base.py:79-81):

```python
    @property
    def instruments(self):
        """Instrument ids this strategy is bound to (routing)."""
        return [self._instrument.instrument_id]
```

For `sma_cross.py`, `mean_reversion.py`, `bracket_breakout.py` (each has an `instrument` property returning `self._instrument`), add the same property. For `buy_and_hold.py` (has `self._instrument`, no `instrument` property), add:

```python
    @property
    def instruments(self):
        """Instrument ids this strategy is bound to (routing)."""
        return [self._instrument.instrument_id]
```

Do NOT add `instruments` to `MultiSymbolSmaCross` — it trades every instrument and must keep wildcard subscription. Add a one-line comment to its class docstring noting it intentionally declares no instruments (wildcard routing).

- [ ] **Step 6: Run the routing tests**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_routing.py -q`
Expected: all pass (router unit tests + engine routing tests). Fix the fakes first if the real `Candle`/`Quote`/`Fill` constructors require different fields.

- [ ] **Step 7: Run the full strategy + engine regression suites**

Run: `.venv/bin/python -m pytest trading/tests/strategy -q`
Expected: all pass — especially `test_context_injection.py` (bar_count tests use instrument-less FakeCandles → guard passes through), `test_pending_durability.py` (pending aging unchanged), `test_strategy_engine.py`, `test_extensions_discovery.py`.

- [ ] **Step 8: Run ruff + import-linter**

Run: `.venv/bin/ruff check strategy/src/tradex_strategy/core/routing.py strategy/src/tradex_strategy/core/engine.py strategy/src/tradex_strategy/extensions/strategies/ trading/tests/strategy/test_routing.py`
Expected: All checks passed
Run: `.venv/bin/lint-imports`
Expected: Contracts: 13 kept, 0 broken (or the repo's current kept count, 0 broken)

- [ ] **Step 9: Commit**

```bash
git add strategy/src/tradex_strategy/core/routing.py strategy/src/tradex_strategy/core/engine.py strategy/src/tradex_strategy/extensions/strategies/study_base.py strategy/src/tradex_strategy/extensions/strategies/sma_cross.py strategy/src/tradex_strategy/extensions/strategies/mean_reversion.py strategy/src/tradex_strategy/extensions/strategies/bracket_breakout.py strategy/src/tradex_strategy/core/buy_and_hold.py trading/tests/strategy/test_routing.py
git commit -m "feat(strategy): route market events per instrument (SubscriptionRouter)"
```

---

## Self-Review

**1. Spec coverage:** Router (Step 3) → routing.py. Engine wiring (Step 4) → register builds index, guards in all four handlers, unregister/dispose clean up. Strategy declarations (Step 5) → all single-instrument strategies declare; MultiSymbolSmaCross stays wildcard. Tests (Steps 1-2, 6-7) → router unit tests + engine routing tests + regression. Acceptance criteria (RELIANCE event → only RELIANCE subscribers; TCS → only TCS; universe subscribers) → `test_candle_reaches_only_bound_strategy`, `test_candle_reaches_universal_strategy`. No spec requirement unaddressed.

**2. Placeholder scan:** The test fakes carry an explicit note to verify `Candle`/`Quote`/`Fill` required fields against the real constructors — this is a flagged verification step, not a placeholder; the implementer must inspect `tradex_domain.market` and `tradex_domain.execution` and adjust. All code steps are complete.

**3. Type consistency:** `StrategySubscription(event_type, instrument_id)` is the single constructor used by the engine. `SubscriptionRouter.subscribe(sub, strategy_id)` / `.delivers(strategy_id, event_type, instrument_id)` signatures are consistent across definition and use. `event_instrument_id(event)` is the single extraction helper used by all three instrument-routed guards. `instruments` property name is identical across all strategy classes and the engine's `getattr`.

**Behavior-preservation check:** the guard in `_wrap_on_bar` is after `_bar_count += 1` and `_flush_pending` (Step 4f) — verified against the Global Constraint. `test_bar_count_still_increments_for_all_handlers` proves the N× increment is preserved. The instrument-less FakeCandle in existing tests passes the guard via the `inst_id is not None` check (Step 3 `event_instrument_id` returns None → guard skipped).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-10-04-phase1-per-symbol-routing.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
