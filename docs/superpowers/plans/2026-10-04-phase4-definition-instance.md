# Phase 4 — Definition/Instance Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Separate a strategy's *definition* (factory + spec) from its *runtime instance* (state bound to one instrument), so the composition root can create one instance per symbol — the path to 500-symbol strategy execution.

**Architecture:** Introduce a `StrategyDefinition` that pairs a strategy's spec with a factory `create(instrument, **config) -> instance`. Each shipped strategy module exports a definition instead of (or alongside) a single hardcoded instance. A new `instantiate_definitions` helper expands definitions × a universe into instances. `boot()` registers the per-binding instances. The existing instance-based registration stays working for backward compatibility (a definition whose factory ignores the instrument still yields the current behavior).

**Tech Stack:** Python 3.12, python, import-linter (enforced), ruff (line-length 100, complexity ratchet).

## Global Constraints

- Test command from repo root: `.venv/bin/python -m pytest <paths> -q`
- Import-linter: `tradex_strategy` may import `{tradex_domain, tradex_analytics, tradex_market_data}`; `tradex_domain` only stdlib; `tradex_runtime` may NOT import `tradex_analytics`. No new cross-package edges.
- Ruff ratchet: no new C901/PLR0912/PLR0915 violations; line length ≤ 100.
- **Backward compatibility:** the existing module-level instances and `all_strategies` discovery keep working. A definition is additive; a strategy with no definition registers as it does today.
- **No signal-generation behavior change.** The definition's factory must produce instances that behave identically to the current hardcoded ones.
- Each task ends with a green test run and a single commit.
- Out of scope: changing the engine's `register()` (it still takes instances), removing the module-level instances (keep them as the definition's default binding), multi-timeframe bindings.

---

### Task 1: StrategyDefinition + per-binding instantiation

**Files:**
- Create: `strategy/src/tradex_strategy/core/definitions.py`
- Modify: `strategy/src/tradex_strategy/extensions/__init__.py` (discover definitions)
- Modify: `strategy/src/tradex_strategy/extensions/strategies/sma_cross.py` (export a definition)
- Modify: `runtime/src/tradex_runtime/startup.py` (instantiate per binding at the seam ~821-851)
- Test: `trading/tests/strategy/test_definition_instance.py` (create)

**Interfaces:**
- Consumes: `StrategySpec` (domain), the strategy classes, `all_strategies` discovery, `boot()` seam at startup.py:821-851.
- Produces: `StrategyDefinition` (spec + factory), `instantiate_definitions(definitions, universe, config) -> list`, `all_definitions` (discovered).

**Behavior contract:** one definition produces one instance per instrument in the universe, each with isolated state. Mutating RELIANCE's state does not affect TCS's. The factory produces instances identical to the current hardcoded ones.

- [ ] **Step 1: Write the failing test**

Create `trading/tests/strategy/test_definition_instance.py`:

```python
"""Definition/Instance separation (Phase 4): one definition, many isolated instances."""

from __future__ import annotations

from tradex_domain import Equity, Timeframe
from tradex_domain.strategy import StrategySpec
from tradex_strategy.core.definitions import StrategyDefinition, instantiate_definitions
from tradex_strategy.extensions.strategies.sma_cross import SmaCrossStrategy


def _definition() -> StrategyDefinition:
    return StrategyDefinition(
        spec=StrategySpec(
            id="sma_cross",
            version="1.0.0",
            scope="INSTRUMENT",
            timeframes=(Timeframe.M1,),
            warmup_bars=20,
            required_indicators=("sma",),
        ),
        create=lambda instrument, **config: SmaCrossStrategy(
            strategy_id="sma_cross", instrument=instrument, **config
        ),
    )


def test_one_definition_many_instances() -> None:
    universe = [Equity.of("NSE", s) for s in ("RELIANCE", "TCS", "INFY")]
    instances = instantiate_definitions([_definition()], universe)
    assert len(instances) == 3
    assert [i.instrument.symbol for i in instances] == ["RELIANCE", "TCS", "INFY"]


def test_instances_have_isolated_state() -> None:
    universe = [Equity.of("NSE", s) for s in ("RELIANCE", "TCS")]
    instances = instantiate_definitions([_definition()], universe)
    reliance, tcs = instances
    # Mutate RELIANCE's state; TCS must be unaffected.
    reliance._closes.append(999.0)
    assert 999.0 not in tcs._closes


def test_definition_without_instrument_uses_default() -> None:
    """A definition whose factory ignores the instrument still yields an instance."""
    definition = StrategyDefinition(
        spec=StrategySpec(id="x", warmup_bars=0),
        create=lambda instrument, **config: SmaCrossStrategy("x", instrument),
    )
    (instance,) = instantiate_definitions([definition], [Equity.of("NSE", "RELIANCE")])
    assert instance.instrument.symbol == "RELIANCE"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_definition_instance.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradex_strategy.core.definitions'`.

- [ ] **Step 3: Implement StrategyDefinition + instantiate_definitions**

Create `strategy/src/tradex_strategy/core/definitions.py`:

```python
"""Strategy definition: a factory + spec, separated from the runtime instance."""

from __future__ import annotations

from typing import Any, Callable

from tradex_domain.instruments import Instrument
from tradex_domain.strategy import StrategySpec


class StrategyDefinition:
    """A strategy factory that can produce one instance per instrument.

    The definition is the stable, stateless half (what the strategy IS);
    the instance is the stateful half bound to one instrument (what it
    TRADES). Separating them lets the composition root create one instance
    per symbol without the strategy class knowing it is multi-symbol.
    """

    def __init__(
        self,
        spec: StrategySpec,
        create: Callable[..., Any],
    ) -> None:
        self.spec = spec
        self.create = create

    def instantiate(self, instrument: Instrument, **config: Any) -> Any:
        return self.create(instrument, **config)


def instantiate_definitions(
    definitions: list[StrategyDefinition],
    universe: list[Instrument],
    **config: Any,
) -> list:
    """Expand definitions × universe into per-binding instances."""
    return [
        definition.instantiate(instrument, **config)
        for definition in definitions
        for instrument in universe
    ]
```

- [ ] **Step 4: Export a definition from sma_cross**

In `sma_cross.py`, add a `StrategyDefinition` (keep the existing module-level instance for backward compat). Read the file to find the class constructor signature and the existing instance, then add:

```python
sma_cross_definition = StrategyDefinition(
    spec=StrategySpec(
        id="sma_cross",
        version="1.0.0",
        scope="INSTRUMENT",
        timeframes=(Timeframe.M1,),
        warmup_bars=20,
        required_indicators=("sma",),
    ),
    create=lambda instrument, **config: SmaCrossStrategy(
        strategy_id="sma_cross", instrument=instrument, **config
    ),
)
```

Add `sma_cross_definition` to the module's `__all__` (or the strategies package `__all__`). Import StrategyDefinition, StrategySpec, Timeframe.

- [ ] **Step 5: Discover definitions**

In `extensions/__init__.py`, add a `all_definitions` collection alongside `all_strategies`. Filter `__all__` exports by a marker (e.g. `isinstance(obj, StrategyDefinition)`). Export `all_definitions`. Keep `all_strategies` unchanged.

- [ ] **Step 6: Instantiate per binding in boot**

In `startup.py` at the seam (~821-851), after the existing instance registration, add a path that instantiates definitions over a configured universe and registers those instances. Read the seam to find how `strategies` is built and where to add per-binding instantiation. Use `instantiate_definitions` with a universe from config (or the strategy's declared instruments). Keep the existing instance registration working (a definition is additive). The Phase 3 spec validation (lines 861-870) must still run — apply it to the instantiated instances too, or to the definitions' specs.

- [ ] **Step 7: Run the test + regression**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_definition_instance.py trading/tests/strategy/test_extensions_discovery.py -q`
Expected: all pass.

Run: `.venv/bin/python -m pytest trading/tests/strategy -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add strategy/src/tradex_strategy/core/definitions.py strategy/src/tradex_strategy/extensions/__init__.py strategy/src/tradex_strategy/extensions/strategies/sma_cross.py runtime/src/tradex_runtime/startup.py trading/tests/strategy/test_definition_instance.py
git commit -m "feat(strategy): separate definition from instance (per-binding instantiation)"
```

---

## Self-Review

**1. Spec coverage:** StrategyDefinition + instantiate_definitions (Step 3), sma_cross definition (Step 4), discovery (Step 5), boot per-binding instantiation (Step 6), isolation test (Steps 1-2, 7). Acceptance: one definition → RELIANCE/TCS/INFY with isolated state → `test_one_definition_many_instances` + `test_instances_have_isolated_state`. No spec requirement unaddressed.

**2. Placeholder scan:** All code steps are complete. The boot seam (Step 6) is described at a high level because the exact insertion depends on the current seam structure — the implementer must read startup.py:821-870 and insert the per-binding instantiation without breaking the existing registration or the Phase 3 validation.

**3. Typeconsistency:** `StrategyDefinition(spec, create)` and `instantiate_definitions(definitions, universe, **config)` are consistent across definition and use. `definition.instantiate(instrument, **config)` is the single factory method. `all_definitions` is the single discovery export.

**Behavior-preservation check:** the definition's factory produces instances identical to the current hardcoded ones (Step 4). The existing instance registration stays (backward compat). The Phase 3 spec validation still runs. No signal-generation change.

---

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-10-04-phase4-definition-instance.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between tasks.

**2. Inline Execution** — execute in this session with checkpoints.

Which approach?
