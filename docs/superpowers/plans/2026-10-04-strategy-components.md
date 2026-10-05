# Quant P1 — Reusable Strategy Components Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Give strategies reusable, composable building blocks — a regime filter, a loss cooldown, a time exit — instead of each strategy hand-rolling regime gating, cooldowns, and exits. This raises the alpha quality of the strategy library without touching the kernel.

**Architecture:** A new `strategy/src/tradex_strategy/core/components.py` module with small, stateful, composable helpers. A strategy owns instances and calls them in its `on_bar`/`on_fill`. The helpers read indicators from the Phase 2 `AnalyticsView` (no new indicator math) and hold only strategy-local state (the Phase 2 rule: strategies keep decision state, not indicator buffers). A reference strategy composes all three.

**Tech Stack:** Python 3.12, python, import-linter (enforced), ruff (line-length 100, complexity ratchet).

## Global Constraints

- Test command from repo root: `.venv/bin/python -m pytest <paths> -q`
- Import-linter: `tradex_strategy` may import `{tradex_domain, tradex_analytics, tradex_market_data}`; `tradex_domain` only stdlib. No new cross-package edges.
- Ruff ratchet: no new C901/PLR0912/PLR0915 violations; line length ≤ 100.
- **No new indicator math.** The components read existing indicators (supertrend direction, atr, rsi) from the view or hold strategy-local state. Do NOT add indicators.
- **No kernel change.** The components live in the strategy layer; they do not touch the engine, execution, OMS, or the backtest engine.
- **Composable, not mandatory.** A strategy that does not use a component is unaffected. Each component is independently testable.
- Each task ends with a green test run and a single commit.
- Out of scope: a trailing-stop protection policy (needs backtest-engine stop-update support — a later phase), an ADX indicator (needs new math + goldens).

---

### Task 1: RegimeFilter + LossCooldown + TimeExit components

**Files:**
- Create: `strategy/src/tradex_strategy/core/components.py`
- Create: `strategy/src/tradex_strategy/extensions/strategies/regime_filtered_supertrend.py`
- Modify: `strategy/src/tradex_strategy/extensions/strategies/__init__.py` (export the new strategy)
- Test: `trading/tests/strategy/test_strategy_components.py` (create)

**Interfaces:**
- Consumes: `AnalyticsView.indicator(name, **params) -> dict[str, list]` (Phase 2), `Signal`/`StrategyContext` (domain), `StudyStrategyBase` (strategy).
- Produces: `RegimeFilter(view, *, trend_indicator="supertrend", params=...)` with `.allows(direction) -> bool`; `LossCooldown(max_bars)` with `.record_fill(pnl)`, `.record_bar()`, `.allows() -> bool`; `TimeExit(max_bars)` with `.record_entry(bar)`, `.should_exit(bar) -> bool`.

**Behavior contract:** each component is a small stateful helper. `RegimeFilter.allows` returns whether the current regime permits a trade (e.g. supertrend direction agrees). `LossCooldown` suppresses signals for `max_bars` bars after a losing fill. `TimeExit` signals an exit after `max_bars` bars in a position. None touch the kernel.

- [ ] **Step 1: Write the failing tests**

Create `trading/tests/strategy/test_strategy_components.py`:

```python
"""Reusable strategy components (Quant P1)."""

from __future__ import annotations

from tradex_strategy.core.components import LossCooldown, RegimeFilter, TimeExit


def test_loss_cooldown_suppresses_after_loss() -> None:
    cooldown = LossCooldown(max_bars=3)
    assert cooldown.allows() is True
    cooldown.record_fill(-100.0)  # a loss
    assert cooldown.allows() is False
    for _ in range(3):
        cooldown.record_bar()
    assert cooldown.allows() is True  # cooldown expired


def test_loss_cooldown_ignores_wins() -> None:
    cooldown = LossCooldown(max_bars=3)
    cooldown.record_fill(100.0)  # a win
    assert cooldown.allows() is True


def test_time_exit_fires_after_max_bars() -> None:
    exit_rule = TimeExit(max_bars=5)
    exit_rule.record_entry(bar=10)
    assert exit_rule.should_exit(bar=14) is False
    assert exit_rule.should_exit(bar=15) is True


def test_regime_filter_gates_on_supertrend_direction() -> None:
    class _View:
        def indicator(self, name, **params):
            return {"direction": [1]}

    regime = RegimeFilter(_View())
    assert regime.allows(direction=1) is True
    assert regime.allows(direction=-1) is False


def test_regime_filter_unknown_regime_denies() -> None:
    class _View:
        def indicator(self, name, **params):
            return {"direction": [None]}

    regime = RegimeFilter(_View())
    assert regime.allows(direction=1) is False  # no regime => no trade
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_strategy_components.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradex_strategy.core.components'`.

- [ ] **Step 3: Implement the components**

Create `strategy/src/tradex_strategy/core/components.py`:

```python
"""Reusable, composable strategy components.

Small stateful helpers a strategy owns and calls from its own callbacks:
a regime filter (gate trades on the market regime), a loss cooldown
(suppress signals after a losing trade), and a time exit (leave a
position after N bars). They read indicators from the Phase 2
AnalyticsView and hold only strategy-local decision state — no indicator
buffers, no kernel coupling.
"""

from __future__ import annotations

from typing import Any


class RegimeFilter:
    """Gate trades on the market regime read from an indicator.

    Default: the supertrend direction. ``allows(direction)`` returns True
    only when the indicator's latest value is known and agrees with the
    proposed trade direction. An unknown regime (warmup / None) denies —
    a strategy that cannot read the regime does not trade blind.
    """

    def __init__(
        self,
        view: Any,
        *,
        indicator: str = "supertrend",
        params: dict[str, Any] | None = None,
    ) -> None:
        self._view = view
        self._indicator = indicator
        self._params = params or {}

    def allows(self, direction: int) -> bool:
        result = self._view.indicator(self._indicator, **self._params)
        values = result.get("direction") or result.get("value") or []
        if not values:
            return False
        latest = values[-1]
        if latest is None:
            return False
        return int(latest) == int(direction)


class LossCooldown:
    """Suppress signals for ``max_bars`` bars after a losing fill."""

    def __init__(self, max_bars: int) -> None:
        if max_bars < 0:
            raise ValueError("max_bars must be >= 0")
        self._max_bars = max_bars
        self._bars_remaining = 0

    def record_fill(self, pnl: float) -> None:
        if pnl < 0:
            self._bars_remaining = self._max_bars

    def record_bar(self) -> None:
        if self._bars_remaining > 0:
            self._bars_remaining -= 1

    def allows(self) -> bool:
        return self._bars_remaining == 0


class TimeExit:
    """Signal an exit after ``max_bars`` bars in a position."""

    def __init__(self, max_bars: int) -> None:
        if max_bars <= 0:
            raise ValueError("max_bars must be > 0")
        self._max_bars = max_bars
        self._entry_bar: int | None = None

    def record_entry(self, bar: int) -> None:
        self._entry_bar = bar

    def clear(self) -> None:
        self._entry_bar = None

    def should_exit(self, bar: int) -> bool:
        if self._entry_bar is None:
            return False
        return bar - self._entry_bar >= self._max_bars
```

- [ ] **Step 4: Write the reference strategy**

Create `strategy/src/tradex_strategy/extensions/strategies/regime_filtered_supertrend.py` — a `StudyStrategyBase` subclass that:
- reads supertrend from the view,
- only emits a signal when `RegimeFilter.allows(direction)` is True,
- suppresses signals when `LossCooldown` is active,
- tracks entries for `TimeExit`.

Read `supertrend_flip.py` (the closest existing strategy) and mirror its structure, adding the three components. Declare a `spec` (Phase 3) with the required indicators. Export it in the strategies package `__all__`.

- [ ] **Step 5: Run the component tests + strategy regression**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_strategy_components.py trading/tests/strategy/test_study_strategies.py -q`
Expected: all pass.

Run: `.venv/bin/python -m pytest trading/tests/strategy -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add strategy/src/tradex_strategy/core/components.py strategy/src/tradex_strategy/extensions/strategies/regime_filtered_supertrend.py strategy/src/tradex_strategy/extensions/strategies/__init__.py trading/tests/strategy/test_strategy_components.py
git commit -m "feat(strategy): reusable regime filter, loss cooldown, and time exit components"
```

---

## Self-Review

**1. Spec coverage:** RegimeFilter + LossCooldown + TimeExit (Step 3), reference strategy (Step 4), tests (Steps 1-2, 5). Covers the quant P1 "regime filter, time exit, loss cooldown" items. Trailing stop is deliberately out of scope (needs backtest-engine stop-update support). No spec requirement unaddressed.

**2. Placeholder scan:** All component code is complete. Step 4 (reference strategy) is described structurally because it mirrors `supertrend_flip.py` — the implementer reads that file and adapts.

**3. Typeconsistency:** `RegimeFilter.allows(direction)`, `LossCooldown.record_fill/record_bar/allows`, `TimeExit.record_entry/clear/should_exit` are consistent across definition, tests, and the reference strategy.

**Behavior-preservation check:** the components are additive (a strategy that doesn't use them is unaffected). No indicator math added. No kernel change. The reference strategy is new (doesn't alter existing strategies).

---

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-10-04-strategy-components.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task.

**2. Inline Execution** — execute in this session with checkpoints.

Which approach?
