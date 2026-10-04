# Phase 2 — Shared Analytics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute each indicator once per (instrument, timeframe, params) and share it across all strategies, instead of every strategy recomputing it over its own private candle buffer.

**Architecture:** Add a canonical `BarStore` (one series per instrument+timeframe, fed as candles arrive) and an `IndicatorCache` (keyed by instrument+timeframe+indicator+params+bar-version) to `tradex_analytics`. An `AnalyticsRuntime` composes them and exposes a read-only `AnalyticsView` bound to one (instrument, timeframe). `ReactiveStrategyEngine` ingests each candle into the runtime and passes the view via `StrategyContext.analytics`. Strategies read `context.analytics.indicator(name, **params)` instead of calling analytics functions directly. The existing `AnalyticsEngine`/`IndicatorRegistry`/`compute_indicator` remain the sole mathematical path — no indicator API changes.

**Tech Stack:** Python 3.12, python, Decimal money, import-linter (enforced), ruff (line-length 100, complexity ratchet).

## Global Constraints

- Test command from repo root: `.venv/bin/python -m pytest <paths> -q`
- Import-linter: `tradex_analytics` may import only `{tradex_domain}`. `tradex_strategy` may import only `{tradex_domain, tradex_analytics, tradex_market_data}`. `tradex_replay` may import `{tradex_domain, tradex_execution, tradex_strategy, tradex_analytics, tradex_reactive, tradex_market_data}`. No new cross-package edges.
- Ruff ratchet: no new C901/PLR0912/PLR0915 violations; line length ≤ 100.
- **No indicator math changes.** `compute_indicator` / `IndicatorRegistry` / `AnalyticsEngine.indicator_values` are the only computation path. The cache stores their result; it never recomputes differently.
- **Behavior preservation:** the engine's `_bar_count` / `_flush_pending` / routing guard semantics are untouched. The analytics ingest happens in `_wrap_on_bar` AFTER the routing guard and BEFORE the strategy call, so the view reflects the current bar.
- **Backward compat:** `StrategyContext.analytics` defaults to `None`. A strategy reading `context.analytics` when it is `None` falls back to its local computation (temporary; removed once all strategies migrate). The engine always provides a view when an `AnalyticsRuntime` is wired.
- Each task ends with a green test run and a single commit.
- Out of scope: incremental indicator `update()` API (rejected by design), FeatureStore as a separate store (the IndicatorCache is the store for Phase 2), strategy-local buffer removal (§33 — only remove a buffer when it becomes fully redundant), rolling-buffer memory bound (note as follow-up; BarStore grows for the session, which is acceptable).

---

### Task 1: Analytics runtime (BarStore + IndicatorCache + AnalyticsView)

**Files:**
- Create: `analytics/src/tradex_analytics/cache.py`
- Create: `analytics/src/tradex_analytics/views.py`
- Test: `analytics/tests/test_cache.py` (create), `analytics/tests/test_views.py` (create)

**Interfaces:**
- Consumes: `compute_indicator(key, candles, params, registry)` from `tradex_analytics.indicators` (returns `dict[str, list]`); `HistoricalSeries`, `Candle`, `Timeframe`, `InstrumentId` from `tradex_domain`.
- Produces:
  - `BarStore`: `.ingest(candle) -> None`, `.series(instrument_id, timeframe) -> HistoricalSeries | None`, `.version(instrument_id, timeframe) -> int`.
  - `IndicatorCache`: `.get_or_compute(key: tuple, version: int, compute_fn: Callable[[], dict]) -> dict`.
  - `AnalyticsView` (Protocol): `.indicator(name: str, **params) -> dict[str, list]` (latest computed result, dict of plot→list).
  - `AnalyticsRuntime`: `.ingest(candle) -> None`, `.view(instrument_id, timeframe) -> AnalyticsView`.

- [ ] **Step 1: Write the failing tests**

Create `analytics/tests/test_cache.py`:

```python
"""BarStore + IndicatorCache: canonical series, versioned cache dedup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain import Candle, Equity, OHLC, Price, Quantity, Timeframe
from tradex_analytics.cache import BarStore, IndicatorCache


def _candle(symbol: str, i: int, tf: Timeframe = Timeframe.M1) -> Candle:
    ts = datetime(2026, 8, 5, 15, 0, tzinfo=UTC) + timedelta(minutes=i)
    return Candle(
        instrument=Equity.of("NSE", symbol),
        timeframe=tf,
        ohlc=OHLC(
            open=Price(value=Decimal("100")),
            high=Price(value=Decimal("101")),
            low=Price(value=Decimal("99")),
            close=Price(value=Decimal(str(100 + i))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


def test_store_ingest_appends_and_bumps_version() -> None:
    store = BarStore()
    inst = Equity.of("NSE", "RELIANCE").instrument_id
    assert store.version(inst, Timeframe.M1) == 0
    store.ingest(_candle("RELIANCE", 0))
    store.ingest(_candle("RELIANCE", 1))
    assert store.version(inst, Timeframe.M1) == 2
    series = store.series(inst, Timeframe.M1)
    assert series is not None and len(series.candles) == 2


def test_store_separates_instruments_and_timeframes() -> None:
    store = BarStore()
    store.ingest(_candle("RELIANCE", 0))
    store.ingest(_candle("TCS", 0))
    store.ingest(_candle("RELIANCE", 0, Timeframe.M5))
    r = Equity.of("NSE", "RELIANCE").instrument_id
    t = Equity.of("NSE", "TCS").instrument_id
    assert len(store.series(r, Timeframe.M1).candles) == 1
    assert len(store.series(t, Timeframe.M1).candles) == 1
    assert len(store.series(r, Timeframe.M5).candles) == 1


def test_cache_returns_same_object_for_same_version() -> None:
    cache = IndicatorCache()
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"value": [1.0, 2.0]}

    key = ("k",)
    assert cache.get_or_compute(key, 1, compute) == {"value": [1.0, 2.0]}
    assert cache.get_or_compute(key, 1, compute) == {"value": [1.0, 2.0]}
    assert calls["n"] == 1  # computed once, served twice


def test_cache_recomputes_on_version_bump() -> None:
    cache = IndicatorCache()
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"value": [float(calls["n"])]}

    key = ("k",)
    assert cache.get_or_compute(key, 1, compute) == {"value": [1.0]}
    assert cache.get_or_compute(key, 2, compute) == {"value": [2.0]}
    assert calls["n"] == 2
```

Create `analytics/tests/test_views.py`:

```python
"""AnalyticsView: cached result equals the direct indicator computation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain import Candle, Equity, OHLC, Price, Quantity, Timeframe
from tradex_analytics.indicators import compute_indicator
from tradex_analytics.registry import REGISTRY
from tradex_analytics.runtime import AnalyticsRuntime


def _candle(symbol: str, i: int) -> Candle:
    ts = datetime(2026, 8, 5, 15, 0, tzinfo=UTC) + timedelta(minutes=i)
    return Candle(
        instrument=Equity.of("NSE", symbol),
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=Decimal("100")),
            high=Price(value=Decimal("101")),
            low=Price(value=Decimal("99")),
            close=Price(value=Decimal(str(100 + i))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


def test_view_indicator_matches_direct_computation() -> None:
    rt = AnalyticsRuntime()
    for i in range(30):
        rt.ingest(_candle("RELIANCE", i))
    inst = Equity.of("NSE", "RELIANCE").instrument_id
    view = rt.view(inst, Timeframe.M1)
    got = view.indicator("sma", period=5)
    series = rt._store.series(inst, Timeframe.M1)
    expected = compute_indicator("sma", series.candles, {"period": 5}, REGISTRY)
    assert got["value"] == expected["value"]


def test_view_indicator_none_before_warmup() -> None:
    rt = AnalyticsRuntime()
    for i in range(3):
        rt.ingest(_candle("RELIANCE", i))
    inst = Equity.of("NSE", "RELIANCE").instrument_id
    view = rt.view(inst, Timeframe.M1)
    got = view.indicator("sma", period=5)
    assert got["value"][-1] is None  # not enough bars yet
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest analytics/tests/test_cache.py analytics/tests/test_views.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradex_analytics.cache'` (and `runtime`/`views`).

- [ ] **Step 3: Implement BarStore + IndicatorCache**

Create `analytics/src/tradex_analytics/cache.py`:

```python
"""Canonical bar store and indicator cache for shared analytics."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tradex_domain.market import Candle, HistoricalSeries
from tradex_domain.value_objects import InstrumentId  # re-export path


class BarStore:
    """One canonical candle series per (instrument, timeframe).

    Fed by the engine as candles arrive. A version counter per key
    invalidates cached indicator results when the series changes (a new
    bar, or a late-tick revision — the version bumps on every ingest).
    """

    def __init__(self) -> None:
        self._series: dict[tuple[InstrumentId, Any], HistoricalSeries] = {}
        self._versions: dict[tuple[InstrumentId, Any], int] = {}

    def ingest(self, candle: Candle) -> None:
        key = (candle.instrument.instrument_id, candle.timeframe)
        self._versions[key] = self._versions.get(key, 0) + 1
        series = self._series.get(key)
        if series is None:
            self._series[key] = HistoricalSeries(
                instrument=candle.instrument,
                timeframe=candle.timeframe,
                candles=[candle],
                start=candle.timestamp,
                end=candle.timestamp,
            )
        else:
            self._series[key] = HistoricalSeries(
                instrument=series.instrument,
                timeframe=series.timeframe,
                candles=[*series.candles, candle],
                start=series.start,
                end=candle.timestamp,
            )

    def series(self, instrument_id: InstrumentId, timeframe) -> HistoricalSeries | None:
        return self._series.get((instrument_id, timeframe))

    def version(self, instrument_id: InstrumentId, timeframe) -> int:
        return self._versions.get((instrument_id, timeframe), 0)


class IndicatorCache:
    """Caches the latest indicator result per key, keyed by bar-version.

    ``get_or_compute`` returns the cached result when the version matches,
    else recomputes and overwrites (only the latest version is kept, so
    memory is bounded by the result size, not the version count).
    """

    def __init__(self) -> None:
        self._entries: dict[tuple, tuple[int, dict]] = {}

    def get_or_compute(
        self, key: tuple, version: int, compute_fn: Callable[[], dict],
    ) -> dict:
        entry = self._entries.get(key)
        if entry is not None and entry[0] == version:
            return entry[1]
        result = compute_fn()
        self._entries[key] = (version, result)
        return result
```

Note: `InstrumentId` is in `tradex_domain.value_objects` (not `tradex_domain.instruments`). Fix the import: `from tradex_domain.value_objects import InstrumentId`. The `timeframe` param type is `Any` (Timeframe) — import `Timeframe` from `tradex_domain.enums` for the annotation.

- [ ] **Step 4: Implement AnalyticsView + AnalyticsRuntime**

Create `analytics/src/tradex_analytics/views.py`:

```python
"""Read-only analytics view over the cached runtime."""

from __future__ import annotations

from typing import Any, Protocol

from tradex_analytics.cache import BarStore, IndicatorCache


class AnalyticsView(Protocol):
    """What a strategy sees: indicator values for one (instrument, timeframe)."""

    def indicator(self, name: str, **params: Any) -> dict[str, list]:
        """Latest computed indicator result (dict of plot -> list)."""
        ...


class CachedAnalyticsView:
    """View bound to one (instrument, timeframe), reading the shared cache."""

    def __init__(
        self, store: BarStore, cache: IndicatorCache, instrument_id, timeframe,
    ) -> None:
        self._store = store
        self._cache = cache
        self._instrument_id = instrument_id
        self._timeframe = timeframe

    def indicator(self, name: str, **params: Any) -> dict[str, list]:
        series = self._store.series(self._instrument_id, self._timeframe)
        if series is None:
            return {}
        version = self._store.version(self._instrument_id, self._timeframe)
        key = (str(self._instrument_id), self._timeframe, name.lower(), tuple(sorted(params.items())))
        return self._cache.get_or_compute(
            key, version, lambda: self._compute(name, series, params),
        )

    @staticmethod
    def _compute(name: str, series, params: dict) -> dict:
        from tradex_analytics.indicators import compute_indicator
        from tradex_analytics.registry import REGISTRY

        return compute_indicator(name, series.candles, params, REGISTRY)


class AnalyticsRuntime:
    """Composes BarStore + IndicatorCache and feeds them from candles."""

    def __init__(self) -> None:
        self._store = BarStore()
        self._cache = IndicatorCache()

    def ingest(self, candle) -> None:
        self._store.ingest(candle)

    def view(self, instrument_id, timeframe) -> CachedAnalyticsView:
        return CachedAnalyticsView(self._store, self._cache, instrument_id, timeframe)
```

- [ ] **Step 5: Run the analytics tests**

Run: `.venv/bin/python -m pytest analytics/tests/test_cache.py analytics/tests/test_views.py -q`
Expected: all pass.

- [ ] **Step 6: Run the analytics regression suite**

Run: `.venv/bin/python -m pytest analytics/tests -q`
Expected: all pass (no existing analytics behavior changed).

- [ ] **Step 7: Commit**

```bash
git add analytics/src/tradex_analytics/cache.py analytics/src/tradex_analytics/views.py analytics/tests/test_cache.py analytics/tests/test_views.py
git commit -m "feat(analytics): add BarStore, IndicatorCache, and AnalyticsRuntime"
```

---

### Task 2: Wire the view through context, engine, and backtest

**Files:**
- Modify: `domain/src/tradex_domain/strategy.py` (`StrategyContext.analytics` field)
- Modify: `strategy/src/tradex_strategy/core/engine.py` (accept `analytics`, ingest, pass view)
- Modify: `replay/src/tradex_replay/backtest.py` (pass analytics to ReactiveStrategyEngine)
- Test: `trading/tests/strategy/test_analytics_wiring.py` (create)

**Interfaces:**
- Consumes: `AnalyticsRuntime` (`tradex_analytics.runtime`), `StrategyContext` (domain).
- Produces: `StrategyContext.analytics: AnalyticsView | None = None`; `ReactiveStrategyEngine(..., analytics: AnalyticsRuntime | None = None)`.

- [ ] **Step 1: Write the failing test**

Create `trading/tests/strategy/test_analytics_wiring.py`:

```python
"""Engine ingests candles into the analytics runtime and passes the view."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain import Candle, Equity, OHLC, Price, Quantity, Timeframe
from tradex_analytics.runtime import AnalyticsRuntime
from tradex_strategy.core.engine import ReactiveStrategyEngine


def _candle(symbol: str, i: int) -> Candle:
    ts = datetime(2026, 8, 5, 15, 0, tzinfo=UTC) + timedelta(minutes=i)
    return Candle(
        instrument=Equity.of("NSE", symbol),
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=Decimal("100")),
            high=Price(value=Decimal("101")),
            low=Price(value=Decimal("99")),
            close=Price(value=Decimal(str(100 + i))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


class _ViewCheckStrategy:
    """Records whether context.analytics was present on each bar."""

    def __init__(self) -> None:
        self.views: list = []

    @property
    def strategy_id(self) -> str:
        return "view_check"

    @property
    def instruments(self):
        return [Equity.of("NSE", "RELIANCE").instrument_id]

    def on_bar(self, context, candle):
        self.views.append(context.analytics)
        return None


def test_engine_passes_analytics_view() -> None:
    rt = AnalyticsRuntime()
    engine = ReactiveStrategyEngine(bus=_FakeBus(), submit=lambda r: None, analytics=rt)
    strategy = _ViewCheckStrategy()
    engine.register(strategy)
    engine._bus.publish(_candle("RELIANCE", 0))
    assert len(strategy.views) == 1
    assert strategy.views[0] is not None


def test_engine_without_analytics_passes_none_view() -> None:
    engine = ReactiveStrategyEngine(bus=_FakeBus(), submit=lambda r: None)
    strategy = _ViewCheckStrategy()
    engine.register(strategy)
    engine._bus.publish(_candle("RELIANCE", 0))
    assert strategy.views[0] is None


class _FakeBus:
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_analytics_wiring.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'analytics'`.

- [ ] **Step 3: Add the context field**

In `domain/src/tradex_domain/strategy.py`, add to `StrategyContext` (after `bar_count`):

```python
    #: Read-only analytics view for the current (instrument, timeframe),
    #: or None when no AnalyticsRuntime is wired. Strategies read indicator
    #: values from this view instead of computing them locally.
    analytics: Any = None
```

(Use `Any` to avoid a domain→analytics import; the view is duck-typed. Import `Any` is already present in the file's typing imports — verify.)

- [ ] **Step 4: Wire the engine**

In `strategy/src/tradex_strategy/core/engine.py`:

4a. Import at top: `from tradex_analytics.runtime import AnalyticsRuntime` — WAIT: import-linter forbids `tradex_strategy` importing `tradex_analytics`? No — it ALLOWS it: `tradex_strategy` may import `{tradex_domain, tradex_analytics, tradex_market_data}`. So `from tradex_analytics.runtime import AnalyticsRuntime` is allowed. But to keep the engine decoupled and avoid a hard dependency, use `TYPE_CHECKING` for the annotation and duck-type at runtime. Add:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from tradex_analytics.runtime import AnalyticsRuntime
```

4b. `__init__` — add `analytics: AnalyticsRuntime | None = None` keyword param (after `sizer`), store `self._analytics = analytics`.

4c. `_wrap_on_bar` — after the routing guard (the `if inst_id is not None and not self._router.delivers(...)` block), before `ctx = self._make_context(...)`, add:

```python
            # Feed the canonical bar store and bind the view to this
            # candle's (instrument, timeframe). The strategy reads indicator
            # values from the shared view instead of computing locally.
            view = None
            if self._analytics is not None:
                self._analytics.ingest(candle)
                view = self._analytics.view(
                    candle.instrument.instrument_id, candle.timeframe
                )
```

Then pass `analytics=view` to `self._make_context(...)`.

- [ ] **Step 5: Wire the backtest**

In `replay/src/tradex_replay/backtest.py`, at the `ReactiveStrategyEngine(...)` construction (line 716), add an `analytics` runtime. Create one in `run` before the engine construction:

```python
        from tradex_analytics.runtime import AnalyticsRuntime
        analytics = AnalyticsRuntime()
        strategy_engine = ReactiveStrategyEngine(
            bus, engine.submit, fill_reference="next_open", sizer=sizer,
            analytics=analytics,
        )
```

(Verify the BacktestEngine constructor doesn't already accept analytics; if it does, thread it through. Otherwise create per-run as above.)

- [ ] **Step 6: Run the wiring test + regression**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_analytics_wiring.py trading/tests/strategy/test_context_injection.py -q`
Expected: all pass.

Run: `.venv/bin/python -m pytest trading/tests/strategy -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add domain/src/tradex_domain/strategy.py strategy/src/tradex_strategy/core/engine.py replay/src/tradex_replay/backtest.py trading/tests/strategy/test_analytics_wiring.py
git commit -m "feat(strategy): pass shared analytics view through engine context"
```

---

### Task 3: Refactor strategies to consume the shared view

**Files:**
- Modify: `strategy/src/tradex_strategy/extensions/strategies/sma_cross.py`
- Modify: `strategy/src/tradex_strategy/extensions/strategies/supertrend_flip.py`
- Modify: `strategy/src/tradex_strategy/extensions/strategies/macd_cross.py`
- Modify: `strategy/src/tradex_strategy/extensions/strategies/rsi_reversal.py`
- Modify: `strategy/src/tradex_strategy/extensions/strategies/bollinger_breakout.py`
- Modify: `strategy/src/tradex_strategy/extensions/strategies/ema_ribbon_pullback.py`
- Test: `trading/tests/strategy/test_strategy_analytics_parity.py` (create)

**Interfaces:**
- Consumes: `context.analytics.indicator(name, **params) -> dict[str, list]` (the view from Task 2).
- Produces: strategies that no longer import `tradex_analytics.indicators` directly; they read from `context.analytics` and fall back to local computation when `context.analytics is None`.

**Behavior contract:** each refactored strategy produces IDENTICAL signals to before (the view returns the same `compute_indicator` result the strategy used to compute locally). The fallback (when `context.analytics is None`) preserves behavior for direct strategy tests that don't wire a runtime.

- [ ] **Step 1: Write the parity test**

Create `trading/tests/strategy/test_strategy_analytics_parity.py`:

```python
"""Refactored strategies emit identical signals via the shared view."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain import Candle, Equity, OHLC, Price, Quantity, Timeframe
from tradex_analytics.runtime import AnalyticsRuntime
from tradex_strategy.core.engine import ReactiveStrategyEngine
from tradex_strategy.extensions.strategies.supertrend_flip import SupertrendFlipStrategy


def _candles(symbol: str, n: int) -> list[Candle]:
    out = []
    for i in range(n):
        ts = datetime(2026, 8, 5, 15, 0, tzinfo=UTC) + timedelta(minutes=i)
        out.append(Candle(
            instrument=Equity.of("NSE", symbol),
            timeframe=Timeframe.M1,
            ohlc=OHLC(
                open=Price(value=Decimal("100")),
                high=Price(value=Decimal("101")),
                low=Price(value=Decimal("99")),
                close=Price(value=Decimal(str(100 + (i % 7) - 3))),
            ),
            volume=Quantity(value=Decimal("1000")),
            timestamp=ts,
        ))
    return out


class _FakeBus:
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


def test_supertrend_flip_signals_match_direct_computation() -> None:
    inst = Equity.of("NSE", "RELIANCE")
    candles = _candles("RELIANCE", 40)

    # Via the shared view (engine + runtime).
    rt = AnalyticsRuntime()
    engine = ReactiveStrategyEngine(bus=_FakeBus(), submit=lambda r: None, analytics=rt)
    via_view = SupertrendFlipStrategy("st_view", inst)
    engine.register(via_view)
    for c in candles:
        engine._bus.publish(c)

    # Direct (no engine): strategy computes locally.
    direct = SupertrendFlipStrategy("st_direct", inst)
    for c in candles:
        direct.on_bar(None, c)

    assert [s.reason for s in via_view.signals] == [s.reason for s in direct.signals]
    assert len(via_view.signals) > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_strategy_analytics_parity.py -q`
Expected: FAIL — the strategy doesn't read `context.analytics` yet (AttributeError or signal mismatch).

- [ ] **Step 3: Refactor the study strategies**

For each strategy extending `StudyStrategyBase` (supertrend_flip, macd_cross, rsi_reversal, bollinger_breakout, ema_ribbon_pullback), replace the direct analytics call in `_on_bar_ready` with the view, and add a fallback when `context.analytics is None`. Example for `supertrend_flip.py`:

```python
    def _on_bar_ready(self, context, candles: list[Any]):
        if context is not None and context.analytics is not None:
            result = context.analytics.indicator(
                "supertrend", period=self._period, multiplier=self._multiplier
            )
        else:
            result = supertrend(candles, self._period, self._multiplier)
        cur_dir = result["direction"][-1]
        ...
```

Apply the same pattern to the other four, using their respective indicator name + params. Keep the `from tradex_analytics.indicators import ...` import ONLY as the fallback path (so the import remains used). Remove the strategy-local `_candles` buffer in `StudyStrategyBase.on_bar` ONLY if it becomes fully redundant — otherwise keep it for the fallback and warmup.

- [ ] **Step 4: Refactor the standalone strategies**

For `sma_cross.py`, `mean_reversion.py`, `bracket_breakout.py` (standalone, not StudyStrategyBase), apply the same view-with-fallback pattern to their indicator computations. `sma_cross` computes SMA inline — replace with `context.analytics.indicator("sma", period=...)` (two calls: fast and slow) with inline fallback.

- [ ] **Step 5: Run the parity test + strategy regression**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_strategy_analytics_parity.py trading/tests/strategy/test_study_strategies.py trading/tests/strategy/test_strategy_engine.py -q`
Expected: all pass.

Run: `.venv/bin/python -m pytest trading/tests/strategy -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add strategy/src/tradex_strategy/extensions/strategies/ trading/tests/strategy/test_strategy_analytics_parity.py
git commit -m "refactor(strategy): consume shared analytics view instead of direct imports"
```

---

## Self-Review

**1. Spec coverage:** BarStore + IndicatorCache + AnalyticsView + AnalyticsRuntime (Task 1). Context + engine + backtest wiring (Task 2). 6 strategy refactors with parity (Task 3). Acceptance: "5 strategies same RSI(14) → one calculated state, five consumers" → the cache dedup test (`test_cache_returns_same_object_for_same_version`) + the runtime sharing one BarStore/cache across strategies. "cached result = direct indicator function result" → `test_view_indicator_matches_direct_computation`. No spec requirement unaddressed.

**2. Placeholder scan:** The `InstrumentId` import path is corrected in-line (value_objects, not instruments). The `Any` annotation for `StrategyContext.analytics` avoids a domain→analytics dependency (import-linter forbids it). All code steps are complete.

**3. Typeconsistency:** `AnalyticsRuntime.ingest(candle)` / `.view(instrument_id, timeframe)` are the single API used by the engine. `AnalyticsView.indicator(name, **params) -> dict[str, list]` matches `compute_indicator`'s return. `StrategyContext.analytics` is the single field name across engine and strategies. `BarStore.ingest/series/version` consistent across cache and runtime.

**Behavior-preservation check:** the engine ingest is after the routing guard and before the strategy call (Task 2 Step 4c) — `_bar_count`/`_flush_pending` untouched. The fallback (`context.analytics is None` → local computation) preserves behavior for direct strategy tests. The backtest creates a fresh `AnalyticsRuntime` per run (no cross-run leakage).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-10-04-phase2-shared-analytics.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
