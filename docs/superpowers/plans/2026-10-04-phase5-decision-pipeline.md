# Phase 5 — DecisionPipeline Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Extract the signal → sizing → protection → order-construction logic out of `ReactiveStrategyEngine` into a pure `DecisionPipeline`, so the decision is independently testable and the engine is purely an orchestrator.

**Architecture:** A new `DecisionPipeline` encapsulates the decision: given a signal + entry price, it sizes (via the `PositionSizer`), builds the protective request (via `protective_request`), and returns an `OrderRequest` (or `None` if refused). The engine keeps the orchestration — defer (`next_open`), flush, correlation ids, submit (`_place`) — and delegates the decision to the pipeline. The public facade (`ReactiveStrategyEngine`) is unchanged. `next_open`, `fill_reference`, sizing timing, bracket semantics, and AC-22 are all preserved.

**Tech Stack:** Python 3.12, python, import-linter (enforced), ruff (line-length 100, complexity ratchet).

## Global Constraints

- Test command from repo root: `.venv/bin/python -m pytest <paths> -q`
- Import-linter: `tradex_strategy` may import `{tradex_domain, tradex_analytics, tradex_market_data}`. No new cross-package edges.
- Ruff ratchet: no new C901/PLR0912/PLR0915 violations; line length ≤ 100.
- **Parity (critical):** given identical market state, strategy state, configuration, and account state, the new pipeline must generate the same Signal, OrderRequest, protective levels, quantity, and fill reference as v4. This is a parity refactor, NOT a strategy rewrite.
- **Do NOT alter:** `next_open` deferred-queue behavior, `fill_reference` semantics, sizing timing (provisional at signal, re-size at flush), bracket semantics (stop/target pair), AC-22 (submit is the only path to venue).
- Each task ends with a green test run and a single commit.
- Out of scope: changing the sizer, changing protective_request, changing the engine's public API.

---

### Task 1: DecisionPipeline + engine delegation + parity test

**Files:**
- Create: `strategy/src/tradex_strategy/core/decision_pipeline.py`
- Modify: `strategy/src/tradex_strategy/core/engine.py` (delegate to the pipeline in `_flush_pending` ~339 and `_maybe_publish_order` ~553)
- Test: `trading/tests/strategy/test_decision_pipeline_parity.py` (create)

**Interfaces:**
- Consumes: `PositionSizer` (tradex_strategy.core.sizing), `protective_request` (tradex_strategy.core.brackets), `Signal`/`OrderRequest` (domain).
- Produces: `DecisionPipeline(sizer).build_request(signal, entry_price, strategy_id, *, correlation_id, tag, reference_timestamp) -> OrderRequest | None`.

**Behavior contract:** the pipeline is a pure decision component. The engine's `_maybe_publish_order` (signal_close path) and `_flush_pending` (next_open path) call `self._pipeline.build_request(...)` instead of inlining size → protective_request. The deferred queue, re-sizing at flush, correlation ids, and `_place` are unchanged.

- [ ] **Step 1: Write the failing parity test**

Create `trading/tests/strategy/test_decision_pipeline_parity.py`:

```python
"""DecisionPipeline parity: same inputs -> same OrderRequest as the inline path."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import Equity, OrderSide, Signal
from tradex_domain.value_objects import Price
from tradex_strategy.core.brackets import protective_request
from tradex_strategy.core.decision_pipeline import DecisionPipeline
from tradex_strategy.core.sizing import unconfigured_sizer


def _signal() -> Signal:
    return Signal(
        instrument=Equity.of("NSE", "RELIANCE"),
        direction=OrderSide.BUY,
        strength=1.0,
        reason="test",
        timestamp=datetime(2026, 8, 5, 15, 0, tzinfo=UTC),
    )


def test_pipeline_matches_inline_protective_request() -> None:
    sizer = unconfigured_sizer()
    pipeline = DecisionPipeline(sizer)
    signal = _signal()
    entry = Price(value=Decimal("100"))
    got = pipeline.build_request(
        signal, entry, "sma_cross",
        correlation_id=None, tag="sma_cross@1.0.0",
        reference_timestamp=signal.timestamp,
    )
    # The inline path the engine used to run:
    from tradex_strategy.core.sizing import PositionSizer
    decision = sizer.size(signal, reference_price=entry)
    expected = protective_request(
        signal=signal, entry=entry, quantity=decision.quantity,
        correlation_id=None, tag="sma_cross@1.0.0",
        strategy_id="sma_cross", reference_timestamp=signal.timestamp,
    )
    assert got is not None
    assert got.quantity == expected.quantity
    assert got.stop_loss_price == expected.stop_loss_price
    assert got.target_price == expected.target_price
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_decision_pipeline_parity.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradex_strategy.core.decision_pipeline'`.

- [ ] **Step 3: Implement DecisionPipeline**

Create `strategy/src/tradex_strategy/core/decision_pipeline.py`:

```python
"""Pure decision pipeline: signal + price -> sized, protected OrderRequest."""

from __future__ import annotations

from typing import Any

from tradex_domain.execution import OrderRequest
from tradex_domain.strategy import Signal
from tradex_domain.value_objects import Price

from tradex_strategy.core.brackets import protective_request
from tradex_strategy.core.sizing import PositionSizer


class DecisionPipeline:
    """Encapsulates the signal -> sizing -> protection -> order decision.

    The engine keeps the orchestration (defer, flush, correlation ids,
    submit) and delegates the decision here, so the decision is independently
    testable and the engine is purely an orchestrator. This is a parity
    extraction: the pipeline produces the same OrderRequest the engine's
    inline path produced.
    """

    def __init__(self, sizer: PositionSizer) -> None:
        self._sizer = sizer

    def build_request(
        self,
        signal: Signal,
        entry_price: Price,
        strategy_id: str,
        *,
        correlation_id: Any = None,
        tag: str | None = None,
        reference_timestamp: Any = None,
    ) -> OrderRequest | None:
        decision = self._sizer.size(signal, reference_price=entry_price)
        if decision.quantity is None:
            return None
        return protective_request(
            signal=signal,
            entry=entry_price,
            quantity=decision.quantity,
            correlation_id=correlation_id,
            tag=tag,
            strategy_id=strategy_id,
            reference_timestamp=reference_timestamp,
        )
```

- [ ] **Step 4: Delegate in the engine**

In `strategy/src/tradex_strategy/core/engine.py`:

4a. Import: `from tradex_strategy.core.decision_pipeline import DecisionPipeline`.

4b. `__init__` — construct the pipeline after `self._sizer` (line 132): `self._pipeline = DecisionPipeline(self._sizer)`.

4c. `_flush_pending` (~339) — replace the inline `quantity = self._size(...)` + `protective_request(...)` with `request = self._pipeline.build_request(pending["signal"], open_price, pending["strategy_id"], correlation_id=pending["correlation_id"], tag=..., reference_timestamp=...)`. Keep the `if quantity is None: continue` guard as `if request is None: continue`. Keep the `_place` call.

4d. `_maybe_publish_order` signal_close path (~553) — replace the inline `quantity = self._size(...)` + `protective_request(...)` with `request = self._pipeline.build_request(result, price, strategy_id, correlation_id=correlation_id, tag=tag, reference_timestamp=...)`. Keep the `if quantity is None: return` guard as `if request is None: return`. Keep the `_place` call and the trace logging.

Read both methods fully before editing to preserve the exact control flow (defer vs flush, correlation id stamping, trace logging).

- [ ] **Step 5: Run the parity test + regression**

Run: `.venv/bin/python -m pytest trading/tests/strategy/test_decision_pipeline_parity.py trading/tests/strategy/test_strategy_engine.py -q`
Expected: all pass.

Run: `.venv/bin/python -m pytest trading/tests/strategy -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add strategy/src/tradex_strategy/core/decision_pipeline.py strategy/src/tradex_strategy/core/engine.py trading/tests/strategy/test_decision_pipeline_parity.py
git commit -m "refactor(strategy): extract DecisionPipeline from the engine"
```

---

## Self-Review

**1. Spec coverage:** DecisionPipeline (Step 3), engine delegation (Step 4), parity test (Steps 1-2, 5). Acceptance: same inputs → same OrderRequest → `test_pipeline_matches_inline_protective_request`. No spec requirement unaddressed.

**2. Placeholder scan:** All code steps are complete. Step 4 references specific line numbers but instructs the implementer to read the methods fully first (the line numbers shifted across Phases 1-4).

**3. Typeconsistency:** `DecisionPipeline(sizer).build_request(signal, entry_price, strategy_id, *, correlation_id, tag, reference_timestamp)` is the single API used by both engine call sites and the test. The pipeline returns `OrderRequest | None`, matching the inline path's behavior.

**Behavior-preservation check:** the pipeline does size → protective_request → return, exactly the inline path. The engine keeps defer/flush/correlation/submit. The `if quantity is None` guards become `if request is None`. No change to next_open, fill_reference, sizing timing, bracket semantics, or AC-22.

---

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-10-04-phase5-decision-pipeline.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between tasks.

**2. Inline Execution** — execute in this session with checkpoints.

Which approach?
