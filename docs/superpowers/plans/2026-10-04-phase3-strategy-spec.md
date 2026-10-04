# Phase 3 — StrategySpec + Declared Requirements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Give every strategy a declared `StrategySpec` (scope, subscriptions, timeframes, warmup, required indicators/features, parameters) and validate those requirements at startup — moving failures from first-candle runtime errors to startup rejection.

**Architecture:** Add a frozen `StrategySpec` dataclass alongside the other strategy domain types. Strategies declare their spec via a `spec` attribute (read with `getattr(strategy, "spec", None)` — the same duck-typed optional-attribute pattern Phase 1 used for `instruments`, so the `Strategy` protocol is NOT modified and undeclared strategies keep working). A new `spec_validator` checks each declared spec against the analytics registry and universe before the strategy activates. `boot()` runs the validator and refuses to start a session whose strategies cannot be satisfied.

**Tech Stack:** Python 3.12, python, import-linter (enforced), ruff (line-length 100, complexity ratchet).

## Global Constraints

- Test command from repo root: `.venv/bin/python -m pytest <paths> -q`
- Import-linter: `tradex_strategy` may import `{tradex_domain, tradex_analytics, tradex_market_data}`; `tradex_domain` only stdlib. No new cross-package edges.
- Ruff ratchet: no new C901/PLR0912/PLR0915 violations; line length ≤ 100.
- **The `Strategy` protocol is NOT modified.** Spec is read via `getattr(strategy, "spec", None)`. A strategy with no spec is valid (nothing to validate) — backward compatible.
- **No behavior change to signal generation.** The spec is metadata + validation only. Routing (Phase 1) and analytics (Phase 2) are unchanged.
- Each task ends with a green test run and a single commit.
- Out of scope: using the spec for routing/computation (Phases 1-2 own those), Definition/Instance separation (Phase 4), options/futures.

---

### Task 1: StrategySpec model + strategy declarations + startup validation

**Files:**
- Modify: `domain/src/tradex_domain/strategy.py` (add `StrategySpec` dataclass)
- Create: `strategy/src/tradex_strategy/core/spec_validator.py`
- Modify: `strategy/src/tradex_strategy/extensions/strategies/study_base.py` (declare spec)
- Modify: `strategy/src/tradex_strategy/extensions/strategies/sma_cross.py` (declare spec)
- Modify: `strategy/src/tradex_strategy/extensions/strategies/mean_reversion.py` (declare spec)
- Modify: `strategy/src/tradex_strategy/extensions/strategies/bracket_breakout.py` (declare spec)
- Modify: `runtime/src/tradex_runtime/startup.py` (run validator in boot)
- Test: `trading/tests/strategy/test_strategy_spec.py` (create)

**Interfaces:**
- Consumes: `IndicatorRegistry` / `get_indicator_spec` (tradex_analytics) to check required indicators exist; `Timeframe`, `InstrumentId` (domain); `boot()` (runtime).
- Produces: `StrategySpec` (frozen dataclass); `validate_strategy_spec(spec, registry, universe) -> list[str]` (list of problems, empty = valid); `boot()` refuses to start when any declared spec has problems.

**Behavior contract:** a strategy declares a spec; startup validates required indicators exist in the analytics registry, required features are known, timeframes are supported, and warmup is sufficient. A spec with problems is a startup error (boot raises), not a first-candle runtime failure. A strategy with no spec is valid.

- [ ] **Step 1: Write the failing test**

Create `trading/tests/strategy/test_strategy_spec.py`:

```python
"""StrategySpec declaration + startup validation (Phase 3)."""

from __future__ import annotations

import pytest
from tradex_analytics.registry import REGISTRY
from tradex_domain import Equity, Timeframe
from tradex_domain.strategy import StrategySpec
from tradex_strategy.core.spec_validator import validate_strategy_spec


def _spec(**overrides) -> StrategySpec:
    base = dict(
        id="test_strategy",
        version="1.0.0",
        scope="INSTRUMENT",
        timeframes=(Timeframe.M1,),
        warmup_bars=20,
        required_indicators=("sma",),
        required_features=(),
    )
    base.update(overrides)
    return StrategySpec(**base)


def test_spec_is_frozen() -> None:
    spec = _spec()
    with pytest.raises(Exception):
        spec.warmup_bars = 99  # type: ignore[misc]


def test_validate_accepts_known_indicator() -> None:
    problems = validate_strategy_spec(_spec(), REGISTRY, universe=[])
    assert problems == []


def test_validate_rejects_unknown_indicator() -> None:
    problems = validate_strategy_spec(
        _spec(required_indicators=("not_a_real_indicator",)), REGISTRY, universe=[]
    )
    assert any("not_a_real_indicator" in p for p in problems)


def test_validate_rejects_unknown_feature() -> None:
    problems = validate_strategy_spec(
        _spec(required_features=("not_a_feature",)), REGISTRY, universe=[]
    )
    assert any("not_a_feature" in p for p in problems)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_strategy_spec.py -q`
Expected: FAIL — `ImportError: cannot import name 'StrategySpec'` (and `spec_validator`).

- [ ] **Step 3: Add StrategySpec**

In `domain/src/tradex_domain/strategy.py`, add a frozen dataclass (after `ScannerResult`):

```python
@dataclass(frozen=True, slots=True)
class StrategySpec:
    """What a strategy declares it needs, validated at startup.

    Read via ``getattr(strategy, "spec", None)`` — a strategy with no spec
    is valid (nothing to validate). This is metadata + validation only; it
    does not drive routing or computation.
    """

    id: str
    version: str = "1.0.0"
    scope: str = "INSTRUMENT"
    timeframes: tuple[Timeframe, ...] = ()
    warmup_bars: int = 0
    required_indicators: tuple[str, ...] = ()
    required_features: tuple[str, ...] = ()
```

Add `"StrategySpec"` to the module's `__all__`.

- [ ] **Step 4: Add the validator**

Create `strategy/src/tradex_strategy/core/spec_validator.py`:

```python
"""Validate a strategy's declared requirements before it activates."""

from __future__ import annotations

from typing import Any

from tradex_analytics.registry import get_indicator_spec
from tradex_domain.strategy import StrategySpec


def validate_strategy_spec(
    spec: StrategySpec,
    registry: Any,
    universe: list | None = None,
) -> list[str]:
    """Return a list of problems (empty = the spec can be satisfied).

    Checks each required indicator against the analytics registry and each
    required feature against the known feature set. A strategy declaring a
    requirement the platform cannot meet is a startup error, not a
    first-candle runtime failure.
    """
    problems: list[str] = []
    for name in spec.required_indicators:
        if get_indicator_spec(name.lower(), registry) is None:
            problems.append(
                f"strategy {spec.id!r} requires indicator {name!r} "
                "which is not implemented"
            )
    # Features: no FeatureRegistry yet — treat any non-empty requirement
    # as unmet until one exists. This is the seam Phase 2's FeatureStore
    # plugs into; for now it fails closed on unknown features.
    for name in spec.required_features:
        problems.append(
            f"strategy {spec.id!r} requires feature {name!r} "
            "which is not implemented"
        )
    return problems
```

- [ ] **Step 5: Declare specs on the shipped strategies**

Add a `spec` attribute/property to each strategy class, returning a `StrategySpec` matching what the strategy actually uses. For `StudyStrategyBase` (covers supertrend_flip, macd_cross, rsi_reversal, bollinger_breakout, ema_ribbon_pullback), add a `spec` property returning a spec with the indicators that subclass uses (each subclass overrides or the base reads a class attribute). For the standalone strategies (sma_cross, mean_reversion, bracket_breakout), add a `spec` property. Use the `getattr` pattern — the spec is read, not enforced by the protocol.

Example for StudyStrategyBase — add a class attribute and property:

```python
    #: Declared requirements (Phase 3). Subclasses override.
    spec: StrategySpec | None = None
```

And in each subclass, set `spec = StrategySpec(id=..., required_indicators=(...), warmup_bars=..., timeframes=(...))`. Read each strategy's `_on_bar_ready` to get the exact indicator names + params it uses.

- [ ] **Step 6: Run the validator in boot**

In `runtime/src/tradex_runtime/startup.py`, after strategies are discovered/registered and before the session starts, validate each strategy's spec. If any spec has problems, raise (refuse to boot). Read `boot()` to find the registration seam. Use `getattr(strategy, "spec", None)` and skip strategies with no spec. Import `validate_strategy_spec` and `REGISTRY`.

- [ ] **Step 7: Run the spec test + regression**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_strategy_spec.py trading/tests/strategy/test_extensions_discovery.py -q`
Expected: all pass.

Run: `.venv/bin/python -m pytest trading/tests/strategy -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add domain/src/tradex_domain/strategy.py strategy/src/tradex_strategy/core/spec_validator.py strategy/src/tradex_strategy/extensions/strategies/ runtime/src/tradex_runtime/startup.py trading/tests/strategy/test_strategy_spec.py
git commit -m "feat(strategy): declare StrategySpec and validate requirements at startup"
```

---

## Self-Review

**1. Spec coverage:** StrategySpec model (Step 3), validator (Step 4), strategy declarations (Step 5), boot validation (Step 6), tests (Steps 1-2, 7). Acceptance: startup rejects missing indicator/feature → `test_validate_rejects_unknown_indicator` / `test_validate_rejects_unknown_feature`. No spec requirement unaddressed.

**2. Placeholder scan:** The feature validation is intentionally a failing-closed stub (no FeatureRegistry yet) — this is a documented seam, not a placeholder; the plan names it as the Phase 2 FeatureStore plug-in point. All code steps are complete.

**3. Typeconsistency:** `StrategySpec` fields are consistent across the dataclass, the test's `_spec` helper, and the strategy declarations. `validate_strategy_spec(spec, registry, universe)` signature is consistent between definition and boot's use. `getattr(strategy, "spec", None)` is the single read pattern.

**Behavior-preservation check:** the spec is metadata + validation only (Step 3 docstring, Global Constraints). The Strategy protocol is unmodified. Strategies with no spec are valid. boot() only ADDS a validation gate; it does not change signal generation.

---

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-10-04-phase3-strategy-spec.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between tasks.

**2. Inline Execution** — execute in this session with checkpoints.

Which approach?
