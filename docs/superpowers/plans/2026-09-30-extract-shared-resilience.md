# Extract Shared Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the `market_data → brokers` layering violation by extracting the rate-limiting code into a shared package.

**Architecture:** Create a `shared/` package containing the generic rate-limiting classes (`MultiBucketRateLimiter`, `TokenBucketRateLimiter`, `RollingWindowCounter`, `RateLimitConfig`, `bucket_for_path`, `limiter_for_provider`). Both `brokers` and `market_data` import from `shared`. The `brokers.common.resilience` module re-exports from `shared` for backward compatibility. Circuit-breaking and retry stay in `brokers`.

**Tech Stack:** Python 3.13, tradex_domain

## Global Constraints

1. **All price values must use `Decimal`, never `float`.**
2. **No package may reach through an abstraction to its internals (Law of Demeter).**
3. **All public modules must declare `__all__`.**
4. **The `market_data → brokers` dependency must be eliminated.**
5. **Existing tests must pass without modification (backward compat via re-exports).**

---

### Task 1: Create `shared/` package with rate-limiting classes

**Files:**
- Create: `shared/src/tradex_shared/__init__.py`
- Create: `shared/src/tradex_shared/rate_limiting.py`
- Modify: `pyproject.toml` (add `tradex_shared` to `root_packages`)

**Interfaces:**
- Consumes: Nothing (foundation)
- Produces: `RateLimitConfig`, `TokenBucketRateLimiter`, `RollingWindowCounter`, `MultiBucketRateLimiter`, `bucket_for_path`, `limiter_for_provider`

- [ ] **Step 1: Create the package structure**

Create `shared/src/tradex_shared/__init__.py`:
```python
"""Shared infrastructure — rate limiting, resilience primitives."""
from tradex_shared.rate_limiting import (
    MultiBucketRateLimiter,
    RateLimitConfig,
    RollingWindowCounter,
    TokenBucketRateLimiter,
    bucket_for_path,
    limiter_for_provider,
)

__all__ = [
    "MultiBucketRateLimiter",
    "RateLimitConfig",
    "RollingWindowCounter",
    "TokenBucketRateLimiter",
    "bucket_for_path",
    "limiter_for_provider",
]
```

- [ ] **Step 2: Write the rate-limiting module**

Create `shared/src/tradex_shared/rate_limiting.py` by extracting the rate-limiting classes from `brokers/src/tradex_brokers/common/resilience.py`:
- `RateLimitConfig` (dataclass)
- `TokenBucketRateLimiter` (class)
- `RollingWindowCounter` (class)
- `MultiBucketRateLimiter` (class)
- `bucket_for_path` (function)
- `limiter_for_provider` (function)
- `_load_rate_tables`, `_rate_tables`, `table_for_provider`, `_env_overrides`, `limiter_from_table` (helper functions)

Copy these verbatim from `resilience.py`. Do not change logic.

- [ ] **Step 3: Register the package**

Add `"tradex_shared"` to `root_packages` in `pyproject.toml` (before `tradex_domain`).

- [ ] **Step 4: Verify the package imports**

Run: `.venv/bin/python -c "from tradex_shared import MultiBucketRateLimiter, RateLimitConfig; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add shared/ pyproject.toml
git commit -m "feat(shared): add shared rate-limiting package

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 2: Update `brokers.common.resilience` to re-export from shared

**Files:**
- Modify: `brokers/src/tradex_brokers/common/resilience.py`

**Interfaces:**
- Consumes: `tradex_shared.rate_limiting` (Task 1)
- Produces: Same public API as before (backward compat)

- [ ] **Step 1: Replace rate-limiting definitions with re-exports**

In `resilience.py`, remove the rate-limiting class/function definitions and replace with:
```python
from tradex_shared.rate_limiting import (
    MultiBucketRateLimiter,
    RateLimitConfig,
    RollingWindowCounter,
    TokenBucketRateLimiter,
    bucket_for_path,
    limiter_for_provider,
)
```

Keep `CircuitBreaker`, `CircuitBreakerConfig`, `RetryableHttpClient`, `ResiliencePipeline`, `RetryConfig`, `RetryExhaustedError`, `retryable` in `resilience.py` (they are broker-specific).

- [ ] **Step 2: Verify backward compat**

Run: `.venv/bin/python -c "from tradex_brokers.common.resilience import MultiBucketRateLimiter, RateLimitConfig, CircuitBreaker; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Run broker tests**

Run: `.venv/bin/python -m pytest brokers/tests/common/test_resilience.py -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add brokers/src/tradex_brokers/common/resilience.py
git commit -m "refactor(brokers): re-export rate-limiting from shared

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 3: Update `market_data.parallel_fetcher` to import from shared

**Files:**
- Modify: `market_data/src/tradex_market_data/parallel_fetcher.py`

**Interfaces:**
- Consumes: `tradex_shared.rate_limiting` (Task 1)

- [ ] **Step 1: Update the import**

Change:
```python
from tradex_brokers.common.resilience import (
    MultiBucketRateLimiter,
    limiter_for_provider,
)
```
To:
```python
from tradex_shared import (
    MultiBucketRateLimiter,
    limiter_for_provider,
)
```

- [ ] **Step 2: Verify no `market_data → brokers` dependency**

Run: `grep -rn "from tradex_brokers" market_data/src/`
Expected: Empty (no output)

- [ ] **Step 3: Run market_data tests**

Run: `.venv/bin/python -m pytest trading/tests/datalake/test_parallel_fetcher.py -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add market_data/src/tradex_market_data/parallel_fetcher.py
git commit -m "refactor(market_data): import rate-limiting from shared, not brokers

Eliminates the market_data -> brokers layering violation.

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 4: Update remaining importers

**Files:**
- Modify: `brokers/src/tradex_brokers/common/client_shared.py`
- Modify: `brokers/src/tradex_brokers/common/rate_table.py`
- Modify: `brokers/src/tradex_brokers/common/__init__.py`

**Interfaces:**
- Consumes: `tradex_shared.rate_limiting` (Task 1)

- [ ] **Step 1: Update `client_shared.py`**

Change the resilience import to import rate-limiting from `tradex_shared` and circuit/retry from `tradex_brokers.common.resilience`:
```python
from tradex_shared import limiter_for_provider
from tradex_brokers.common.resilience import (
    CircuitBreaker,
    ResiliencePipeline,
    RetryableHttpClient,
)
```

- [ ] **Step 2: Update `rate_table.py`**

Change:
```python
from tradex_brokers.common.resilience import RateLimitConfig
```
To:
```python
from tradex_shared import RateLimitConfig
```

- [ ] **Step 3: Update `__init__.py` re-exports**

Change the resilience re-export to import rate-limiting from `tradex_shared`:
```python
from tradex_shared import (
    MultiBucketRateLimiter,
    RateLimitConfig,
    TokenBucketRateLimiter,
    bucket_for_path,
    limiter_for_provider,
)
from tradex_brokers.common.resilience import (
    CircuitBreaker,
    ResiliencePipeline,
    RetryableHttpClient,
)
```

- [ ] **Step 4: Run all affected tests**

Run: `.venv/bin/python -m pytest brokers/tests trading/tests/datalake -q`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add brokers/src/tradex_brokers/common/
git commit -m "refactor(brokers): import rate-limiting from shared

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 5: Update import-linter contracts

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: Nothing
- Produces: Updated import-linter contracts

- [ ] **Step 1: Add `shared` to the `market_data` allowlist**

In the import-linter contract for `market_data`, add `"tradex_shared"` to the allowed packages.

- [ ] **Step 2: Add a `shared` contract**

Add a contract that `shared` may only import from `tradex_domain` and `tradex_shared`.

- [ ] **Step 3: Verify boundaries**

Run: `.venv/bin/python -m pytest tests/test_import_boundaries.py -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "chore: update import-linter contracts for shared package

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 6: Full verification

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ brokers/tests trading/tests -q`
Expected: All pass

- [ ] **Step 2: Run ruff**

Run: `.venv/bin/python -m ruff check .`
Expected: All checks passed

- [ ] **Step 3: Run ratchet**

Run: `.venv/bin/python scripts/ruff_ratchet.py`
Expected: OK

- [ ] **Step 4: Verify no `market_data → brokers` dependency**

Run: `grep -rn "from tradex_brokers" market_data/src/`
Expected: Empty

- [ ] **Step 5: Commit (if any fixes needed)**

```bash
git add -u
git commit -m "fix: address verification findings

Co-Authored-By: Agent <agent@example.com>"
```
