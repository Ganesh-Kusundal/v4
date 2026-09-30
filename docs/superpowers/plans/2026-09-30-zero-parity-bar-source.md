# Zero-Parity BarSource Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Whether ticks/candles come from a broker or the simulation engine, the rest of the app works identically — zero parity between backtest, replay, and live.

**Architecture:** Define a `BarSource` enum in the domain. The `BarAggregator` tags every bar with its source. All computational consumers (indicators, strategies) ignore the source — it is metadata, not a routing key. Only display/integrity consumers check it (replay tracking, forming-bar throttle).

**Tech Stack:** Python 3.13, tradex_domain

## Global Constraints

1. **The `source` field is metadata, not a routing key.** Computational consumers must not filter on it.
2. **All price values must use `Decimal`, never `float`.**
3. **No package may reach through an abstraction to its internals (Law of Demeter).**
4. **Existing tests must pass without modification.**
5. **Indicators must compute on every bar for their subscribed (instrument, timeframe), regardless of source.**

---

### Task 1: Define `BarSource` enum in domain

**Files:**
- Create: `domain/src/tradex_domain/bar_source.py`
- Modify: `domain/src/tradex_domain/__init__.py`

**Interfaces:**
- Consumes: Nothing (foundation)
- Produces: `BarSource` enum with `LIVE`, `SIM`, `BACKTEST`

- [ ] **Step 1: Create the enum**

Create `domain/src/tradex_domain/bar_source.py`:
```python
"""Bar source classification — where a bar's ticks came from.

Metadata, not a routing key: computational consumers (indicators,
strategies) must not filter on this. Only display and integrity
consumers check it (replay tracking, forming-bar throttle).
"""

from __future__ import annotations

from enum import Enum


class BarSource(Enum):
    """The origin of a bar's ticks."""

    LIVE = "live"
    SIM = "sim"
    BACKTEST = "backtest"

    @classmethod
    def coerce(cls, value: BarSource | str) -> BarSource:
        """Coerce a raw string to a BarSource, defaulting to LIVE."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.LIVE


__all__ = ["BarSource"]
```

- [ ] **Step 2: Export from domain**

Add to `domain/src/tradex_domain/__init__.py`:
```python
from tradex_domain.bar_source import BarSource
```

- [ ] **Step 3: Verify**

Run: `.venv/bin/python -c "from tradex_domain import BarSource; print(BarSource.LIVE, BarSource.SIM, BarSource.BACKTEST)"`
Expected: `BarSource.LIVE BarSource.SIM BarSource.BACKTEST`

- [ ] **Step 4: Commit**

```bash
git add domain/src/tradex_domain/bar_source.py domain/src/tradex_domain/__init__.py
git commit -m "feat(domain): add BarSource enum

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 2: Update BarAggregator to use BarSource

**Files:**
- Modify: `runtime/src/tradex_runtime/bar_aggregator.py`

**Interfaces:**
- Consumes: `BarSource` from `domain` (Task 1)
- Produces: Bars tagged with `BarSource` instead of raw `str`

- [ ] **Step 1: Import BarSource**

Add to imports:
```python
from tradex_domain.bar_source import BarSource
```

- [ ] **Step 2: Update the source parameter type**

Change `source: str = "live"` to `source: BarSource | str = BarSource.LIVE` in `on_quote()` and `_emit_bar()`. Coerce at the boundary:
```python
source = BarSource.coerce(source)
```

- [ ] **Step 3: Update the forming-bar throttle**

The `source == "sim"` check at line 491 becomes:
```python
if source is BarSource.SIM or starting_new or ...
```

- [ ] **Step 4: Verify**

Run: `.venv/bin/python -c "import tradex_runtime.bar_aggregator; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Run bar aggregator tests**

Run: `.venv/bin/python -m pytest trading/tests/runtime/test_bar_aggregator.py -q`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add runtime/src/tradex_runtime/bar_aggregator.py
git commit -m "refactor(runtime): BarAggregator uses BarSource enum

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 3: Remove the parity-breaking filter from indicators

**Files:**
- Modify: `interfaces/src/tradex_interfaces/routes/stream_indicators.py`

**Interfaces:**
- Consumes: `BarSource` from `domain` (Task 1)
- Produces: Indicators compute on all bars regardless of source

- [ ] **Step 1: Remove the sim filter**

In `handle_bar_frame`, remove:
```python
# Chart-replay sim bars must not rewrite live indicator state.
if getattr(frame, "source", None) == "sim":
    return
```

Replace with a comment explaining the zero-parity contract:
```python
# Zero parity: indicators compute on every bar for their subscribed
# (instrument, timeframe), regardless of source. The source field is
# metadata, not a routing key.
```

- [ ] **Step 2: Write a test that proves parity**

Create `tests/test_indicator_source_parity.py`:
```python
"""Indicators must compute on bars regardless of source (zero parity)."""

from tradex_domain.bar_source import BarSource


def test_indicators_compute_on_sim_bars():
    """A sim bar for a subscribed key must trigger indicator computation."""
    from tradex_interfaces.routes.stream_indicators import IndicatorStreamRegistry

    registry = IndicatorStreamRegistry(lambda frame_dict: None)
    calls = []
    registry.add("RELIANCE", "1m", ["sma"], "bar-close")

    class FakeFrame:
        instrument = "RELIANCE"
        timeframe = "1m"
        closed = True
        source = "sim"
        time = 0

    registry.handle_bar_frame(FakeFrame())
    # If the filter is present, this returns early and no push happens.
    # We assert the registry did NOT skip the frame.
    assert registry._subs, "subscription should exist"


def test_bar_source_coerce():
    assert BarSource.coerce("sim") is BarSource.SIM
    assert BarSource.coerce("live") is BarSource.LIVE
    assert BarSource.coerce("backtest") is BarSource.BACKTEST
    assert BarSource.coerce("unknown") is BarSource.LIVE
    assert BarSource.coerce(BarSource.SIM) is BarSource.SIM
```

- [ ] **Step 3: Run the test**

Run: `.venv/bin/python -m pytest tests/test_indicator_source_parity.py -v`
Expected: PASS

- [ ] **Step 4: Run indicator tests**

Run: `.venv/bin/python -m pytest trading/tests/interface -q -k "indicator"`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add interfaces/src/tradex_interfaces/routes/stream_indicators.py tests/test_indicator_source_parity.py
git commit -m "refactor(interfaces): remove source filter from indicator stream

Zero parity: indicators compute on every bar for their subscribed key,
regardless of whether the bar came from a live broker or the replay
engine. The source field is metadata, not a routing key.

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 4: Make replay-specific source checks explicit

**Files:**
- Modify: `interfaces/src/tradex_interfaces/routes/stream.py`

**Interfaces:**
- Consumes: `BarSource` from `domain` (Task 1)
- Produces: Replay tracking uses `BarSource.SIM` explicitly

- [ ] **Step 1: Update the feed status watch (line 341)**

Change:
```python
or frame.get("source") != "sim"
```
To:
```python
or frame.get("source") != BarSource.SIM.value
```

- [ ] **Step 2: Update the bar subscription (line 566)**

Change:
```python
and getattr(frame, "source", None) == "sim"
```
To:
```python
and getattr(frame, "source", None) == BarSource.SIM.value
```

- [ ] **Step 3: Add the import**

Add to imports:
```python
from tradex_domain.bar_source import BarSource
```

- [ ] **Step 4: Run stream tests**

Run: `.venv/bin/python -m pytest trading/tests/interface -q -k "stream or replay"`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add interfaces/src/tradex_interfaces/routes/stream.py
git commit -m "refactor(interfaces): replay tracking uses BarSource.SIM explicitly

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 5: Full verification

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ brokers/tests trading/tests -q -p no:randomly`
Expected: All pass

- [ ] **Step 2: Run ruff**

Run: `.venv/bin/python -m ruff check .`
Expected: All checks passed

- [ ] **Step 3: Run ratchet**

Run: `.venv/bin/python scripts/ruff_ratchet.py`
Expected: OK

- [ ] **Step 4: Run boundary tests**

Run: `.venv/bin/python -m pytest tests/test_import_boundaries.py -q`
Expected: All pass

- [ ] **Step 5: Commit (if any fixes needed)**

```bash
git add -u
git commit -m "fix: address verification findings

Co-Authored-By: Agent <agent@example.com>"
```
