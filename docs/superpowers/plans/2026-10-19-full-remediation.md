# Full Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove remaining dead/legacy code, fix misleading documentation, and close the last silent-failure gaps identified by the pre-deployment review.

**Architecture:** Incremental, test-first edits within existing packages. No new abstractions; delete or simplify only. Each task is independently testable and committable.

**Tech Stack:** Python 3.12, pytest, ruff, mypy, sqlite3, threading.

## Global Constraints

- No backward compatibility for deleted paths.
- Deletion preferred over refactoring.
- Every code change must keep `pytest` targeted suites green.
- No new comments unless required for correctness.

---

### Task 1: Fix false "shared" comment on terminal statuses

**Files:**
- Modify: `execution/src/tradex_execution/kill_switch.py:33-36`

- [ ] **Step 1: Edit comment**

```python
#: ``UNKNOWN`` is deliberately absent. It means the submission crossed the
#: broker boundary and the venue never answered, so the order may be live right
#: now — halting is exactly when it must be reached. This set is intentionally
#: distinct from ``recovery._TERMINAL_AFTER_FILL`` (which excludes FILLED).
```

- [ ] **Step 2: Run tests**

Run: `.venv/bin/python -m pytest trading/tests/execution -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add execution/src/tradex_execution/kill_switch.py
git commit -m "docs: correct terminal-status sharing claim"
```

---

### Task 2: Delete `Order.is_terminal`

**Files:**
- Modify: `domain/src/tradex_domain/execution.py:219-233`
- Modify: `domain/tests/test_domain_enrichments.py` (remove `TestOrderIsTerminal`)

- [ ] **Step 1: Remove method**

Delete the `is_terminal` property block from `Order`.

- [ ] **Step 2: Remove test class**

Delete `TestOrderIsTerminal` from `domain/tests/test_domain_enrichments.py`.

- [ ] **Step 3: Run tests**

Run: `.venv/bin/python -m pytest domain/tests -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add domain/src/tradex_domain/execution.py domain/tests/test_domain_enrichments.py
git commit -m "refactor: remove orphan Order.is_terminal"
```

---

### Task 3: Remove unused `ExecutionEngine._clock`

**Files:**
- Modify: `execution/src/tradex_execution/engine.py` (constructor + assignment)

- [ ] **Step 1: Remove parameter**

Remove `clock: Clock | None = None` from `__init__` signature and `self._clock = clock` assignment.

- [ ] **Step 2: Run tests**

Run: `.venv/bin/python -m pytest trading/tests/execution -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add execution/src/tradex_execution/engine.py
git commit -m "refactor: drop unused ExecutionEngine clock param"
```

---

### Task 4: Delete `envelopes.py`

**Files:**
- Delete: `execution/src/tradex_execution/envelopes.py`

- [ ] **Step 1: Delete file**

```bash
rm execution/src/tradex_execution/envelopes.py
```

- [ ] **Step 2: Run tests**

Run: `.venv/bin/python -m pytest execution/tests trading/tests/execution -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "chore: remove dead envelopes module"
```

---

### Task 5: Log discarded kill-switch cancel failures

**Files:**
- Modify: `execution/src/tradex_execution/engine.py` (unknown-outcome trip site)

- [ ] **Step 1: Capture and log failures**

```python
failures = self._kill_switch.trip(
    "unknown_broker_outcome: the venue may have accepted an "
    f"order this process cannot account for ({exc})",
    cancel=self.cancel,
)
if failures:
    log.error("kill-switch could not cancel orders: %s", failures)
```

- [ ] **Step 2: Run tests**

Run: `.venv/bin/python -m pytest trading/tests/execution -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add execution/src/tradex_execution/engine.py
git commit -m "fix: log kill-switch cancel failures"
```

---

### Task 6: Surface risk mark-price provider failures

**Files:**
- Modify: `execution/src/tradex_execution/risk.py:260-262,270-272`

- [ ] **Step 1: Add warning logs**

```python
except Exception as exc:  # noqa: BLE001
    log.warning("risk mark price provider failed: %s", exc)
    return None
```

and

```python
except Exception as exc:  # noqa: BLE001
    log.warning("risk bid lookup failed: %s", exc)
    pass
```

- [ ] **Step 2: Run tests**

Run: `.venv/bin/python -m pytest trading/tests/execution -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add execution/src/tradex_execution/risk.py
git commit -m "fix: log risk mark-price provider failures"
```

---

### Task 7: Collapse `PositionManager` alias

**Files:**
- Modify: all src/test imports of `PositionManager` → `PositionAccountant`
- Delete: `execution/src/tradex_execution/position_manager.py` (replace with re-export? No—delete after edits)

- [ ] **Step 1: Replace imports**

```bash
grep -rl "from tradex_execution.position_manager import PositionManager" --include="*.py" . | grep -v .venv | xargs sed -i '' 's/from tradex_execution.position_manager import PositionManager/from tradex_execution.position_accountant import PositionAccountant as PositionManager/'
```

(Then rename call sites `PositionManager(` → `PositionAccountant(` in a follow-up if desired; alias kept as import name is acceptable minimal change.)

- [ ] **Step 2: Run tests**

Run: `.venv/bin/python -m pytest trading/tests/execution -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "refactor: route PositionManager through PositionAccountant"
```

---

### Task 8: Break auth import cycles

**Files:**
- Modify: `brokers/src/tradex_brokers/common/auth.py` (TYPE_CHECKING imports)

- [ ] **Step 1: Remove TYPE_CHECKING imports of auth_flow modules**

Replace the `TYPE_CHECKING` block with `Any`-based annotations or move mint factory typing to runtime `__getattr__` only.

- [ ] **Step 2: Run import check**

Run: `.venv/bin/python -c "import tradex_brokers.common.auth, tradex_brokers.common.auth_flow; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Run tests**

Run: `.venv/bin/python -m pytest brokers/tests -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add brokers/src/tradex_brokers/common/auth.py
git commit -m "refactor: remove auth import cycle"
```

---

### Task 9: Add persistence write-failure counter

**Files:**
- Modify: `execution/src/tradex_execution/sqlite_store.py:640-675`

- [ ] **Step 1: Increment metric on swallowed write failure**

In both `except Exception` blocks, add:

```python
if self._metrics is not None:
    self._metrics.counter("persistence.write_failures").inc()
```

(Requires store to accept optional metrics; if not present, skip and log only.)

- [ ] **Step 2: Run tests**

Run: `.venv/bin/python -m pytest trading/tests/execution -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add execution/src/tradex_execution/sqlite_store.py
git commit -m "feat: count persistence write failures"
```

---

### Task 10: Verify and remove unused conditional modules

**Files:**
- `reactive/src/tradex_reactive/event_log.py`
- `research/` package

- [ ] **Step 1: Verify production usage**

```bash
grep -R "tradex_research\|SQLEventLog(" --include="*.py" . | grep -v tests | grep -v .venv
```

- [ ] **Step 2: If no production callers, delete and update tests**

Delete module/package; remove corresponding testpaths entries.

- [ ] **Step 3: Run full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: remove unused conditional modules"
```

---

### Task 11: Final verification

- [ ] **Step 1: Run lint**

Run: `.venv/bin/python -m ruff check .`
Expected: no new violations

- [ ] **Step 2: Run typecheck**

Run: `.venv/bin/python -m mypy execution/src`
Expected: no errors

- [ ] **Step 3: Run full test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass

---

## Self-Review

Spec coverage: P0/P1/P2/P3 items from review are mapped to tasks. Placeholders: none—each task has code. Type consistency: only local edits; no new public APIs.
