# v4 Pre-Deployment Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close all ten NO-GO blockers from the 2026-10-22 pre-deployment review by making packaging, live gates, idempotency, durability, persistence and teardown correct, deleting every dead or duplicate path, and proving the result with a hermetic test suite and a deploy gate.

**Architecture:** Seven work packages, executed in order. WP0 fixes the test harness and captures a truthful baseline. WP1 makes the tree installable and the container buildable. WP2 closes the live boot/order gates and strategy-selection hole. WP3 makes idempotency and durability fail closed with transaction-safe SQLite. WP4 collapses persistence to one authority (the event log). WP5 deletes dead modules, duplicate engines and stale docs. WP6 makes the suite hermetic, fixes CI, and adds the pre-deploy gate.

**Tech Stack:** Python 3.12+, uv workspace, pytest, stdlib `sqlite3`, RxPY, FastAPI/uvicorn, Docker.

**Spec:** `docs/superpowers/specs/2026-10-22-v4-predeploy-remediation-design.md`

---

## Global Constraints

- Work on branch `feat/tier1-3-remediation`. Do not rebase, amend or force-push.
- Python 3.12+; `uv` only. **No new runtime or dev dependencies.**
- No partial fixes, no backward-compatibility shims, no "keep it just in case". Deleting a path means deleting its tests, wires and docs in the same task.
- No live orders and no venue authentication. Tests use `PaperBroker`, fakes and `tmp_path` only.
- Never read, print, commit or copy `.env.local`, credentials or tokens.
- Every durable test path lives under `tmp_path`. A test must never write into the repository's `runtime/` directory.
- Run tests with `uv run --no-sync python -m pytest ... -q -p no:cacheprovider`. The full suite is slow: two measured baseline runs took 9 and 14 minutes. Run it in the background (or with a shell timeout of at least 20 minutes); `--timeout=300` only bounds each individual test, not the run.
- One commit per task, using the message given in the task's final step. Do not batch tasks into one commit.
- Do not touch unrelated dirty files that were present before this plan started.
- If a targeted test run reveals a failure outside the files this task changes, stop and report it; do not "fix" it by editing unrelated code.
- The full-suite baseline is `78 failed, 6389 passed, 16 skipped, 10 xfailed`. No task may increase the number of failures. T20 must reach `0 failed`.

## Baseline

Captured in Task 1 before any production edit:

```bash
mkdir -p /tmp/v4-predeploy
uv run --no-sync python -m pytest -c pyproject.toml -q -p no:cacheprovider \
  --timeout=300 > /tmp/v4-predeploy/baseline.txt 2>&1 || true
grep -E "^(FAILED|ERROR) [^ ]" /tmp/v4-predeploy/baseline.txt | sort \
  > /tmp/v4-predeploy/baseline-failures.txt
wc -l /tmp/v4-predeploy/baseline-failures.txt
tail -n 1 /tmp/v4-predeploy/baseline.txt
```

Expected: 78 failure lines; summary line ends `78 failed, 6389 passed, 16 skipped, 10 xfailed`.

The `[^ ]` after the space matters: captured log lines such as
`ERROR    tradex_runtime.startup:...` also start at column 0, and a plain
`^(FAILED|ERROR)` grep counts them as failures (82 instead of 78 on the
measured baseline). Pytest's short-summary lines have exactly one space
before the node id.

## File Structure

**Created**

| Path | Responsibility |
|---|---|
| `tests/test_packaging_authority.py` | Guard: no `shared/`, no `tradex_shared`, workspace manifests exist |
| `tests/test_removed_surfaces.py` | Guard: every deleted module/package stays deleted |
| `tests/test_hermetic_runtime.py` | Guard: the suite's runtime directory is a temp dir, never `<repo>/runtime` |
| `scripts/mypy_ratchet.py` | Fail when the repo-wide mypy error count grows past `.mypy_baseline` |
| `.mypy_baseline` | The measured type-debt count the ratchet protects |
| `.dockerignore` | Keep secrets, VCS data, venvs and runtime DBs out of the build context |
| `scripts/predeploy_gate.sh` | One command that runs the deploy-blocking checks |
| `market_data/tests/test_parallel_fetcher_deadline.py` | Deadline reaches the fetcher through a public method |
| `config/tests/test_env_strict.py` | Strict broker parsing, strategy ids, live flags, `serve_config` |
| `runtime/tests/test_strategy_selection.py` | `_select_strategies` unit contract |

**Modified (production)**

- `pyproject.toml`, `conftest.py`, `.github/workflows/quality-gate.yml`, `.pre-commit-config.yaml`
- `README.md`, `docs/ARCHITECTURE.md`
- `brokers/tests/common/test_runtime_root.py`
- `deploy/Dockerfile`
- `config/src/tradex_config/{env,schema}.py`
- `runtime/src/tradex_runtime/{startup,session_impl,__init__}.py`
- `execution/src/tradex_execution/{engine,sqlite_store}.py`
- `strategy/src/tradex_strategy/core/engine.py`
- `interfaces/src/tradex_interfaces/{fastapi_app,cli}.py`
- `interfaces/src/tradex_interfaces/routes/orders.py`
- `market_data/src/tradex_market_data/parallel_fetcher.py`
- `domain/src/tradex_domain/__init__.py`, `domain/src/tradex_domain/protocols.py`
- `brokers/src/tradex_brokers/**` imports of `tradex_shared`
- `observability/tests/test_deploy_artifacts.py`

**Deleted (tracked production)**

- `shared/` (folded into `tradex_domain`)
- `research/`
- `runtime/src/tradex_runtime/shutdown.py`
- `brokers/src/tradex_brokers/common/http_response.py`
- `interfaces/src/tradex_interfaces/replay_run.py`
- `market_data/src/tradex_market_data/universe.py`
- `scripts/extract_package.py`
- Order-store classes and wiring inside `execution/src/tradex_execution/sqlite_store.py`
- `RuntimeContext`, `boot_context`, `ingest_fill`/`FillIngestPort`
- `PricingService`, `ReplayFillSource`, `SlippageAwareFillSource`, `fee_for_fill`
- `StrategyArtifactStore` protocol, `run_with_loader`, `get_rate_limit_config`, `resolve_fill_path`, `ALL_ROUTES`, analytics helpers
- `SessionRecovery`, `OrderRepository`, and the recovery tests that only exercise them
- `trading/tests/deployment/test_env_persistence.py` (reads the repo's `.env.local`; fails on a clean CI checkout)
- Stale review/architecture docs listed in Task 18

**Left untouched (pre-existing work)**

`git ls-files` is unreliable in this checkout (content filters are configured);
`git status --porcelain` is authoritative. The following untracked paths are
the user's in-progress work and are NOT part of this remediation. Do not delete,
edit, stage, or "fix" them; leave their tests collectable.

- `analytics/src/tradex_analytics/{screening,sensitivity}.py`
- `brokers/src/tradex_brokers/**/retry_config.py`
- `config/src/tradex_config/broker_limits.py`
- `data/.sync.lock`
- `domain/src/tradex_domain/clocks.py`
- `execution/src/tradex_execution/{audit_log,risk_manager,sim_gateway,sim_portfolio}.py` and their tests
- `replay/src/tradex_replay/{backtest_adapter,backtest_engine,monte_carlo}.py` and their tests
- `runtime/src/tradex_runtime/{clocks,environments}.py` and `runtime/tests/{test_clocks,test_cross_env,test_environments,test_zero_parity_source_agnostic}.py`
- `strategy/src/tradex_strategy/base.py`, `strategy/tests/*`
- `tests/test_perf_backtest.py`
- `trading/.venv` (never commit; never delete)
- pre-existing `docs/specs/*`, `runtime/*.db` artifacts, `trading/.venv`

**Pre-existing tracked changes must be checkpointed first.** The working tree
is not clean: dozens of tracked files are modified and several stale docs are
already deleted (among them `trading-predeploy-plan.md`,
`docs/deep-refactor-plan.md`, the old `docs/superpowers/plans/*`, and
`trading/scripts/{fill_gaps,repair_gaps}.py`). The line numbers and file
contents in this plan were captured against that working tree, so the content
must be preserved, not reverted. The plan also edits files that currently
carry pre-existing hunks: `domain/src/tradex_domain/{__init__,constants,
protocols}.py`, `runtime/src/tradex_runtime/__init__.py`,
`interfaces/src/tradex_interfaces/cli.py`,
`market_data/src/tradex_market_data/parallel_fetcher.py`,
`shared/src/tradex_shared/rate_limiting.py`,
`tests/domain/test_constants.py`, `tests/test_import_boundaries.py`, and
`docs/ARCHITECTURE.md`.

**Preflight gate (operator action, before Task 1):**

```bash
mkdir -p /tmp/v4-predeploy
git status --porcelain | grep -v '^??' > /tmp/v4-predeploy/tracked-dirty.txt
cat /tmp/v4-predeploy/tracked-dirty.txt
```

Expected: empty. If it is not empty, **stop**. Ask the operator to checkpoint
the current tracked state unchanged — `git add -u && git commit -m "chore:
checkpoint pre-existing work"` — or to stash it under a unique tag
(`git stash push -m v4-predeploy-preexisting -- <paths>`), then re-run the
gate. The executor must never stage, revert, or commit those hunks itself.
Committing unchanged is the supported path: it preserves the content the line
anchors refer to and leaves the baseline numbers in Task 1 valid. A stash
changes the content under test, so baseline counts will differ.

Untracked files stay in place throughout; they are collectable by pytest.

This table is the map, not the manifest. Each task's **Files** block is
authoritative for the exact tests, configs and docs that task touches,
including test files this table omits for brevity.

---

### Task 1: WP0 — Baseline and test-harness truth

**Files:**
- Modify: `tests/test_no_foreign_private_writes.py`
- Modify: `conftest.py`
- Modify: `.github/workflows/quality-gate.yml:104-105`
- Test: `tests/test_no_foreign_private_writes.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_workspace_src_dirs() -> list[Path]`, `_SRC_DIRS` derived from workspace members plus `shared/src` when it exists. Later tasks rely on this scan covering every real source root.

Wanted: the pytest short-summary lines. The `[^ ]` keeps captured log lines
like `ERROR    tradex_runtime.startup:...` out of the count; without it the
measured baseline reports 82 instead of 78.

- [ ] **Step 1: Capture the baseline**

Run this one in the background; a measured run takes 9–14 minutes.

```bash
mkdir -p /tmp/v4-predeploy
uv run --no-sync python -m pytest -c pyproject.toml -q -p no:cacheprovider \
  --timeout=300 > /tmp/v4-predeploy/baseline.txt 2>&1 || true
grep -E "^(FAILED|ERROR) [^ ]" /tmp/v4-predeploy/baseline.txt | sort \
  > /tmp/v4-predeploy/baseline-failures.txt
wc -l /tmp/v4-predeploy/baseline-failures.txt
tail -n 1 /tmp/v4-predeploy/baseline.txt
```

Expected: 78 lines; summary ends `78 failed, 6389 passed, 16 skipped, 10 xfailed`. Record the number in the commit body.

- [ ] **Step 2: Write the failing harness test**

Replace the hardcoded `_SRC_DIRS` list in `tests/test_no_foreign_private_writes.py` with discovery, and add the self-check:

```python
import tomllib

_ROOT = Path(__file__).resolve().parent.parent


def _workspace_src_dirs() -> list[Path]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = data["tool"]["uv"]["workspace"]["members"]
    dirs = [_ROOT / member / "src" for member in members]
    if (_ROOT / "shared/src").is_dir():
        dirs.append(_ROOT / "shared/src")
    return sorted(path for path in dirs if path.is_dir())


_SRC_DIRS = _workspace_src_dirs()


def test_the_scan_covers_every_workspace_source_root() -> None:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = data["tool"]["uv"]["workspace"]["members"]
    expected = {(_ROOT / member / "src") for member in members if (_ROOT / member / "src").is_dir()}
    if (_ROOT / "shared/src").is_dir():
        expected.add(_ROOT / "shared/src")
    assert set(_SRC_DIRS) == expected
    assert all(path.is_dir() for path in _SRC_DIRS), "a scan root does not exist"
```

- [ ] **Step 3: Run it to see the harness gap**

```bash
uv run --no-sync python -m pytest tests/test_no_foreign_private_writes.py -q -p no:cacheprovider
```

Expected before the edit: FAIL — the hardcoded list names `trading/src`, `operations/src` and `persistence/src`, which are not directories, and omits `shared/src`. After adding discovery and the test: PASS.

- [ ] **Step 4: Reduce the root `conftest.py` path hack**

Replace the loop with only the still-uninstalled tree:

```python
"""Root test bootstrap.

`shared` is not a workspace member yet; every other package is installed
editable by `uv sync`. Task 3 folds `shared` into `tradex_domain` and removes
this block entirely.
"""

from __future__ import annotations

import sys
from pathlib import Path

_V4 = Path(__file__).resolve().parent

for _src in ("shared/src",):
    _path = str(_V4 / _src)
    if _path not in sys.path:
        sys.path.insert(0, _path)
```

- [ ] **Step 5: Prove the bootstrap is not load-bearing for installed packages**

```bash
uv run --no-sync python -m pytest domain/tests brokers/tests execution/tests -q -p no:cacheprovider
```

Expected: no new collection/import errors compared with the baseline (the known failures in those directories remain).

- [ ] **Step 6: Fix the dead CI mypy path**

The package directory is `services/duckdb-analytics/src/duck_analytics`; CI names `tradex_duckdb`, which does not exist. Change `.github/workflows/quality-gate.yml:105`:

```yaml
      - name: Mypy (duckdb-analytics)
        run: uv run --no-sync mypy services/duckdb-analytics/src/duck_analytics
```

Verify the path resolves:

```bash
uv run --no-sync mypy services/duckdb-analytics/src/duck_analytics 2>&1 | tail -1
```

Expected: `Found 1 error in 1 file (checked 13 source files)` — the package resolves and reports its one pre-existing error (`patterns.py:142`). CI still hard-fails on it until Task 19 replaces the per-package mypy steps with the ratchet; that is deliberate, because a hard wall that cannot go green protects nothing.

- [ ] **Step 7: Commit**

```bash
git add tests/test_no_foreign_private_writes.py conftest.py .github/workflows/quality-gate.yml
git commit -m "test(wp0): derive scan roots from the workspace; fix dead CI mypy path"
```

---

### Task 2: WP0 — Public deadline seam for the parallel fetcher

**Files:**
- Modify: `market_data/src/tradex_market_data/parallel_fetcher.py`
- Test: `market_data/tests/test_parallel_fetcher_deadline.py` (create)

**Interfaces:**
- Produces: `ParallelHistoryFetcher.set_deadline(deadline: float | None) -> None`; `_deadline` is initialised in `__init__`. `fetch_with_backoff(fetcher, ...)` calls `set_deadline` when the fetcher exposes it.

- [ ] **Step 1: Write the failing test**

```python
"""The fetch deadline must cross the fetcher boundary through a public seam.

`fetch_with_backoff` used to assign `fetcher._deadline` directly. That is a
foreign private write: it silently does nothing for a double that never reads
the attribute, and the deadline was lost in exactly those tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from tradex_domain.enums import Timeframe
from tradex_market_data.parallel_fetcher import fetch_with_backoff


class _RecordingFetcher:
    def __init__(self) -> None:
        self.deadlines: list[float | None] = []

    def set_deadline(self, deadline: float | None) -> None:
        self.deadlines.append(deadline)

    def fetch(self, instruments, timeframe, start, end, ranges=None):
        return {str(i.instrument_id): object() for i in instruments}, []


def test_fetch_with_backoff_sets_the_deadline_through_the_public_method() -> None:
    fetcher = _RecordingFetcher()
    batch = [SimpleNamespace(instrument_id="NSE:TEST")]
    stamp = datetime(2026, 10, 22, tzinfo=UTC)

    fetch_with_backoff(fetcher, batch, Timeframe.D1, stamp, stamp, deadline=123.5)

    assert fetcher.deadlines == [123.5]
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run --no-sync python -m pytest market_data/tests/test_parallel_fetcher_deadline.py -q -p no:cacheprovider
```

Expected: FAIL — `_RecordingFetcher` has no `_deadline` attribute set and the list stays empty (or the attribute assignment creates one, proving the seam is private).

- [ ] **Step 3: Implement the seam**

In `fetch_with_backoff`, replace:

```python
        # Attribute, not a kwarg: test doubles of fetch() do not take it.
        fetcher._deadline = deadline  # type: ignore[attr-defined]
```

with:

```python
        # Public seam: doubles of fetch() need not know a private attribute.
        set_deadline = getattr(fetcher, "set_deadline", None)
        if callable(set_deadline):
            set_deadline(deadline)
```

In `ParallelHistoryFetcher.__init__`, after `self._prefetch_gate: dict[str, bool] = {}`, add:

```python
        self._deadline: float | None = None
```

Add after `_maybe_acquire`:

```python
    def set_deadline(self, deadline: float | None) -> None:
        """Set the wall-clock deadline used by this fetch (public seam)."""
        self._deadline = deadline
```

In `fetch()`, delete:

```python
        if not hasattr(self, "_deadline"):
            self._deadline = None
```

- [ ] **Step 4: Run the test and the module's existing tests**

```bash
uv run --no-sync python -m pytest market_data/tests/test_parallel_fetcher_deadline.py market_data/tests -q -p no:cacheprovider
```

Expected: the new test passes; no new failures in `market_data/tests`.

- [ ] **Step 5: Commit**

```bash
git add market_data/src/tradex_market_data/parallel_fetcher.py market_data/tests/test_parallel_fetcher_deadline.py
git commit -m "refactor(wp0): replace foreign private deadline write with a public setter"
```

---

### Task 3: WP1 — Fold `tradex_shared` into `tradex_domain`

**Files:**
- Move: `shared/src/tradex_shared/rate_limiting.py` → `domain/src/tradex_domain/rate_limiting.py`
- Move: `shared/src/tradex_shared/market_builder.py` → `domain/src/tradex_domain/market_builder.py`
- Delete: `shared/`
- Modify: `domain/src/tradex_domain/__init__.py`, `market_builder.py`, `conftest.py`, `pyproject.toml:152`, every `tradex_shared` importer
- Test: `tests/test_packaging_authority.py` (create)

**Interfaces:**
- Consumes: `tradex_domain.utils.as_decimal`, `tradex_domain.value_objects.Price`.
- Produces: `tradex_domain.rate_limiting` and `tradex_domain.market_builder`; top-level exports `MultiBucketRateLimiter, RateLimitConfig, RollingWindowCounter, TokenBucketRateLimiter, as_decimal, as_price, bucket_for_path, limiter_from_table, build_depth, candles_from_dataframe, make_candle`.

- [ ] **Step 1: Write the failing guard test**

```python
"""One package tree: no un-installable `tradex_shared` shim."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# This file must state the banned name, so the scan below skips itself.
_SELF = Path(__file__).resolve()


def test_the_shared_tree_is_gone() -> None:
    assert not (_ROOT / "shared").exists(), (
        "shared/src was never a workspace member; it moved into tradex_domain"
    )


def test_nothing_imports_tradex_shared() -> None:
    offenders: list[str] = []
    for path in _ROOT.rglob("*.py"):
        if "__pycache__" in path.parts or ".venv" in path.parts:
            continue
        if path.resolve() == _SELF:
            continue
        if "tradex_shared" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(path.relative_to(_ROOT)))
    assert not offenders, f"tradex_shared still referenced by: {offenders}"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run --no-sync python -m pytest tests/test_packaging_authority.py -q -p no:cacheprovider
```

Expected: FAIL — `shared/` exists and dozens of files reference `tradex_shared`.

- [ ] **Step 3: Move the modules**

```bash
git mv shared/src/tradex_shared/rate_limiting.py domain/src/tradex_domain/rate_limiting.py
git mv shared/src/tradex_shared/market_builder.py domain/src/tradex_domain/market_builder.py
```

In `domain/src/tradex_domain/rate_limiting.py`:
- Replace `from tradex_domain.utils import as_decimal as _domain_as_decimal` with `from tradex_domain.utils import as_decimal`.
- Delete the wrapper:

```python
def as_decimal(value): return _domain_as_decimal(value)
```

- Add `as_decimal` and `as_price` to `__all__` (keep the existing names).

In `domain/src/tradex_domain/market_builder.py`:
- Replace `from tradex_shared.rate_limiting import as_price` with `from tradex_domain.rate_limiting import as_price`.

In `domain/src/tradex_domain/__init__.py`, append imports next to the existing domain imports:

```python
from tradex_domain.market_builder import build_depth, candles_from_dataframe, make_candle
from tradex_domain.rate_limiting import (
    MultiBucketRateLimiter,
    RateLimitConfig,
    RollingWindowCounter,
    TokenBucketRateLimiter,
    as_decimal,
    as_price,
    bucket_for_path,
    limiter_from_table,
)
```

Add the same names to `__all__`.

- [ ] **Step 4: Rewrite every importer mechanically**

```bash
grep -rl 'tradex_shared' --include='*.py' --include='*.toml' --include='*.yml' \
  --exclude-dir=.venv --exclude-dir=__pycache__ --exclude-dir=.git . \
  | grep -v 'tests/test_packaging_authority.py' \
  | grep -v '^\./pyproject.toml$' \
  | xargs perl -pi -e 's/tradex_shared/tradex_domain/g'
```

`tests/test_packaging_authority.py` is excluded on purpose: it must keep the
literal `tradex_shared` to prove the name is gone. Rewriting it would turn the
guard into a check for `tradex_domain` and fail it. The root `pyproject.toml`
is excluded because Step 5 deletes its `root_packages` entry by hand; a blind
rewrite would turn it into a duplicate `"tradex_domain",` line instead.

This covers brokers, market_data, `test_parquet_history_seam.py` and every test that patched the package by name.

- [ ] **Step 5: Delete the tree and the harness hack**

```bash
git rm -r shared
```

Remove `"tradex_shared",` from `[tool.importlinter].root_packages` in `pyproject.toml`.

Replace `conftest.py` with:

```python
"""Root test bootstrap. All workspace packages are installed editable."""
```

- [ ] **Step 6: Run the guard and the affected suites**

```bash
uv run --no-sync python -m pytest tests/test_packaging_authority.py \
  domain/tests brokers/tests market_data/tests tests/test_parquet_history_seam.py \
  -q -p no:cacheprovider
```

Expected: guard passes; no import errors; no new failures.

- [ ] **Step 7: Commit**

```bash
git add -A shared domain/src/tradex_domain conftest.py pyproject.toml \
  brokers market_data tests/test_packaging_authority.py
git commit -m "refactor(wp1): fold tradex_shared into tradex_domain"
```

---

### Task 4: WP1 — Make the container buildable and secret-safe

**Files:**
- Modify: `deploy/Dockerfile:34-63`
- Create: `.dockerignore`
- Modify: `observability/tests/test_deploy_artifacts.py`

**Interfaces:**
- Produces: a Dockerfile whose every `COPY <dir>/pyproject.toml` names an existing path, whose `ENV` puts `/app/.venv/bin` on `PATH`, and a `.dockerignore` that excludes secrets, runtime data and VCS state.

- [ ] **Step 1: Write the failing artifact tests**

Append to `observability/tests/test_deploy_artifacts.py`:

```python
import re

_ROOT = Path(__file__).resolve().parents[2]


def test_every_copied_workspace_manifest_exists() -> None:
    instructions = _dockerfile_instructions()
    targets = re.findall(r"^COPY\s+(\S+)/pyproject\.toml\s", instructions, re.MULTILINE)
    assert targets, "the Dockerfile no longer copies any workspace manifest"
    missing = [
        target
        for target in targets
        if not (_ROOT / target / "pyproject.toml").is_file()
    ]
    assert not missing, f"COPY names a manifest that does not exist: {missing}"


def test_the_container_puts_the_venv_on_path() -> None:
    assert 'PATH="/app/.venv/bin:${PATH}"' in _dockerfile_instructions()


def test_dockerignore_excludes_secrets_and_runtime_state() -> None:
    dockerignore = _ROOT / ".dockerignore"
    assert dockerignore.is_file(), "a Docker build must not send the whole tree"
    patterns = set(dockerignore.read_text(encoding="utf-8").splitlines())
    required = {
        ".git",
        ".venv",
        "**/.venv",
        ".env",
        ".env.*",
        "**/__pycache__",
        "**/*.pyc",
        "runtime/",
        "**/*.sqlite",
        "**/*.db",
    }
    assert required <= patterns, f"missing ignore patterns: {sorted(required - patterns)}"
```

- [ ] **Step 2: Run them to verify they fail**

```bash
uv run --no-sync python -m pytest observability/tests/test_deploy_artifacts.py -q -p no:cacheprovider
```

Expected: FAIL — `operations/` and `persistence/` manifests do not exist; `PATH` is absent; `.dockerignore` is absent. The `research/` manifest does still exist at this point (Task 5 deletes the tree), but the stale `COPY research/…` line is removed now so the Dockerfile never names a deleted path.

- [ ] **Step 3: Fix the Dockerfile**

Delete these three lines from the manifest block:

```dockerfile
COPY research/pyproject.toml ./research/
COPY operations/pyproject.toml ./operations/
COPY persistence/pyproject.toml ./persistence/
```

Make the `ENV` block start with `PATH`:

```dockerfile
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
```

- [ ] **Step 4: Create `.dockerignore`**

```text
.git
.gitignore
.github
.venv
**/.venv
**/__pycache__
**/*.pyc
.pytest_cache
.mypy_cache
.ruff_cache
.env
.env.*
!.env.example
runtime/
**/*.sqlite
**/*.sqlite3
**/*.db
graphify-out/
docs/superpowers/
```

- [ ] **Step 5: Run the tests**

```bash
uv run --no-sync python -m pytest observability/tests/test_deploy_artifacts.py -q -p no:cacheprovider
```

Expected: PASS.

- [ ] **Step 6: Build and smoke-run the image (if Docker is available)**

```bash
docker build -f deploy/Dockerfile -t tradex-v4:predeploy .
docker run --rm --entrypoint python tradex-v4:predeploy -c \
  "import tradex_runtime, tradex_domain, tradex_market_data; print('imports ok')"
docker run --rm -i -e TRADEX_RUNTIME_DIR=/tmp/tradex-runtime \
  --entrypoint python tradex-v4:predeploy - <<'PY'
from decimal import Decimal

from tradex_config import AppConfig, RiskConfig
from tradex_runtime import TradingSession

config = AppConfig(
    risk=RiskConfig(
        max_order_value=Decimal("100000"),
        max_position_value=Decimal("500000"),
        max_orders_per_minute=10,
        max_daily_loss_amt=Decimal("5000"),
        max_drawdown_pct=Decimal("5"),
    )
)
session = TradingSession.paper(config=config)
try:
    assert str(session.state) == "READY", session.state
    print("paper boot: READY")
finally:
    session.stop()
PY
```

Expected: build succeeds; the container prints `imports ok` and `paper boot: READY`. Together they prove the venv is on `PATH`, the workspace install is complete, and a container actually reaches a READY paper session (the spec's acceptance criterion). `--entrypoint python` is required: the image's entrypoint is `tradex serve`, so a bare `docker run … python -c …` would hand `python` to the CLI as a serve argument. If Docker is not installed or its daemon is not running on the executor host, record that fact and continue; T20 re-runs this check through the gate.

- [ ] **Step 7: Commit**

```bash
git add deploy/Dockerfile .dockerignore observability/tests/test_deploy_artifacts.py
git commit -m "build(wp1): copy only real manifests, put the venv on PATH, add .dockerignore"
```

### Task 5: WP5 — Delete `research/` and every reference to it

**Files:**
- Delete: `research/`, `trading/tests/research/`, `market_data/tests/test_pit_universe_artifacts.py`
- Modify: `pyproject.toml`, `trading/pyproject.toml`, `.github/workflows/quality-gate.yml`, `.pre-commit-config.yaml`, `scripts/coverage_floors.py`, `tests/test_import_boundaries.py`, `observability/tests/test_alert_rules.py`
- Test: `tests/test_removed_surfaces.py` (create)

**Interfaces:**
- Produces: no package, workspace member, test path, CI job or guard entry named `research`/`tradex_research`. The guard file becomes the growing record of deleted surfaces.

- [ ] **Step 1: Write the failing guard**

```python
"""Deleted packages must stay deleted.

Every path here was removed because it had no production caller. The guard
fails if one comes back, and it is the single place to add the next deletion.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def test_removed_trees_stay_removed() -> None:
    removed = [
        "research",
        "trading/tests/research",
        "market_data/tests/test_pit_universe_artifacts.py",
    ]
    assert [path for path in removed if (_ROOT / path).exists()] == []


def test_pyproject_names_no_removed_member() -> None:
    for pyproject in ("pyproject.toml", "trading/pyproject.toml"):
        text = (_ROOT / pyproject).read_text(encoding="utf-8")
        for needle in ("research/tests", "research/src", "tradex-research", "tradex_research"):
            assert needle not in text, f"{pyproject} still names {needle}"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run --no-sync python -m pytest tests/test_removed_surfaces.py -q -p no:cacheprovider
```

Expected: FAIL — both trees and every pyproject reference exist.

- [ ] **Step 3: Delete the package and its tests**

```bash
git rm -r research trading/tests/research
git rm market_data/tests/test_pit_universe_artifacts.py
```

- [ ] **Step 4: Remove every build/test reference**

In `pyproject.toml`:
- Delete `"research/tests",` from `testpaths`.
- Delete `"research/src",` from `pythonpath`.
- Delete `"research",` from `[tool.uv.workspace].members`.
- Delete `tradex-research = { workspace = true }` from `[tool.uv.sources]`.
- Delete `"tradex-research",` from the dev dependency group.
- Delete the whole `# tradex_research is deliberately absent here...` comment block above the `market_data` import-linter contract (lines ~205-218); the package it explains no longer exists.

In `trading/pyproject.toml` (dependency bundle + local test config for a directory that is not a workspace member):
- Delete `"tradex-research>=0.1.0,<0.2",` from `[project].dependencies`.
- Delete `"../research/src"` from the pytest `pythonpath` list.

In `.github/workflows/quality-gate.yml`:
- Delete the `Mypy (research)` step (lines 95-96).
- Delete `--cov=tradex_research` (line 129).

In `.pre-commit-config.yaml:21`, remove the three dead scan roots:

```text
... observability/src/ | grep -v ...
```

In `scripts/coverage_floors.py`, delete `"tradex_research",` from the `tradex_core` member list.

In `tests/test_import_boundaries.py`:
- Delete `"research": _ROOT / "research/src",` from `_SRC`.
- Delete `"research": {"tradex_domain"},` from `_ALLOWED`.
- Delete `test_research_package_never_imports_trading_brokers_or_ops`.
- In `_INTERNAL`, keep `"tradex_research"` as a deleted-package marker, and update the comment to say it was deleted and must never be imported again.
- Delete `test_research_does_not_import_fastapi_brokers_or_sqlite`, `test_research_shim_only_imports_tradex_research`, `_RESEARCH_PACKAGE_SRC`, `_RESEARCH_SHIM_SRC` and `_RESEARCH_FORBIDDEN_TOPLEVEL`.
- Keep `_RESEARCH_DOTTED_PREFIXES` and `test_interface_routes_do_not_import_research`: they now prove routes never re-import the deleted package.
- Remove the `"research"` string from the set at line ~236 (`if pkg in {"domain", "brokers", "research", "config"}`).

In `observability/tests/test_alert_rules.py:49`, delete `"research/src",` from the scanned root list.

- [ ] **Step 5: Refresh the lock and run the guards**

```bash
uv lock
uv run --no-sync python -m pytest tests/test_removed_surfaces.py tests/test_import_boundaries.py \
  observability/tests/test_alert_rules.py -q -p no:cacheprovider
```

Expected: PASS. `uv lock` removes the `research` member from the lock.

- [ ] **Step 6: Commit**

```bash
git add -A research trading/tests/research market_data/tests/test_pit_universe_artifacts.py \
  pyproject.toml uv.lock .github/workflows/quality-gate.yml .pre-commit-config.yaml \
  scripts/coverage_floors.py tests/test_import_boundaries.py tests/test_removed_surfaces.py \
  observability/tests/test_alert_rules.py
git commit -m "chore(wp5): delete the research package and all build/test references"
```

---

### Task 6: WP2 — Strict environment parsing and declared strategy ids

**Files:**
- Modify: `config/src/tradex_config/env.py`, `config/src/tradex_config/schema.py`
- Test: `config/tests/test_env_strict.py` (create)

**Interfaces:**
- Produces: `AppConfig.live_enabled`, `AppConfig.live_orders_enabled` and `AppConfig.persistence` read from `TRADEX_LIVE_ENABLED`, `TRADEX_LIVE_ORDERS_ENABLED`, `TRADEX_PERSISTENCE_PATH`; `ExecutionConfig.strategy_ids: tuple[str, ...]` read from `TRADEX_STRATEGY_IDS`; `serve_config(config, *, broker_id, mode) -> AppConfig`. Task 7 consumes `strategy_ids`; Task 8 consumes `serve_config`.

- [ ] **Step 1: Write the failing tests**

```python
"""`from_env` must refuse a typo and carry every live-control variable."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from tradex_config.env import from_env, serve_config
from tradex_config.schema import AppConfig, PersistenceConfig
from tradex_domain import BrokerId
from tradex_domain.errors import SDKError

_ENV_KEYS = (
    "TRADEX_BROKER",
    "TRADEX_MODE",
    "TRADEX_LIVE_ENABLED",
    "TRADEX_LIVE_ORDERS_ENABLED",
    "TRADEX_PERSISTENCE_PATH",
    "TRADEX_STRATEGY_IDS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_an_unknown_broker_refuses_to_boot() -> None:
    import os

    os.environ["TRADEX_BROKER"] = "dhann"
    with pytest.raises(SDKError, match="invalid TRADEX_BROKER"):
        from_env()


def test_strategy_ids_are_trimmed_and_deduplicated(monkeypatch) -> None:
    monkeypatch.setenv("TRADEX_STRATEGY_IDS", " alpha, beta ,alpha,, ")
    assert from_env().execution.strategy_ids == ("alpha", "beta")


def test_live_controls_are_read_from_the_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TRADEX_LIVE_ENABLED", "true")
    monkeypatch.setenv("TRADEX_LIVE_ORDERS_ENABLED", "true")
    monkeypatch.setenv("TRADEX_PERSISTENCE_PATH", str(tmp_path / "state.db"))

    cfg = from_env()

    assert cfg.live_enabled is True
    assert cfg.live_orders_enabled is True
    assert cfg.persistence.path == str(tmp_path / "state.db")


def test_serve_config_live_enables_the_gate_and_defaults_persistence(tmp_path) -> None:
    cfg = replace(from_env(), runtime_dir=str(tmp_path))

    live = serve_config(cfg, broker_id=BrokerId.DHAN, mode="live")

    assert live.broker_id is BrokerId.DHAN
    assert live.mode == "live"
    assert live.live_enabled is True
    assert live.persistence.path == str(tmp_path / "live" / "dhan.sqlite")


def test_serve_config_live_keeps_an_explicit_persistence_path(tmp_path) -> None:
    cfg = replace(
        from_env(),
        runtime_dir=str(tmp_path),
        persistence=PersistenceConfig(path=str(tmp_path / "explicit.db")),
    )

    live = serve_config(cfg, broker_id=BrokerId.DHAN, mode="live")

    assert live.persistence.path == str(tmp_path / "explicit.db")


def test_serve_config_paper_changes_nothing_but_identity(tmp_path) -> None:
    cfg = replace(from_env(), runtime_dir=str(tmp_path))

    paper = serve_config(cfg, broker_id=BrokerId.PAPER, mode="paper")

    assert paper.broker_id is BrokerId.PAPER
    assert paper.mode == "paper"
    assert paper.live_enabled is False
    assert paper.persistence.path is None


def test_execution_config_normalises_strategy_ids() -> None:
    from tradex_config.schema import ExecutionConfig
    from decimal import Decimal

    cfg = ExecutionConfig(strategy_ids=(" a ", "b", "a"))
    assert cfg.strategy_ids == ("a", "b")
```

- [ ] **Step 2: Run them to verify they fail**

```bash
uv run --no-sync python -m pytest config/tests/test_env_strict.py -q -p no:cacheprovider
```

Expected: FAIL — invalid broker silently becomes PAPER, `strategy_ids` does not exist, live flags/persistence are not read, `serve_config` is not importable.

- [ ] **Step 3: Implement strict parsing**

In `config/src/tradex_config/env.py`:
- Add `from dataclasses import replace`, `from typing import Any` and `PersistenceConfig` to the schema import.
- Replace the broker fallback:

```python
    broker_id_str = os.environ.get("TRADEX_BROKER", PAPER_DEFAULT)
    try:
        broker_id = BrokerId(broker_id_str)
    except ValueError as exc:
        raise SDKError(f"invalid TRADEX_BROKER {broker_id_str!r}") from exc
```

- Add, next to the execution block:

```python
    strategy_ids = tuple(
        dict.fromkeys(
            part.strip()
            for part in os.environ.get("TRADEX_STRATEGY_IDS", "").split(",")
            if part.strip()
        )
    )
    live_enabled = _parse_bool(os.environ.get("TRADEX_LIVE_ENABLED", "false"))
    live_orders_enabled = _parse_bool(
        os.environ.get("TRADEX_LIVE_ORDERS_ENABLED", "false")
    )
    persistence_path = os.environ.get("TRADEX_PERSISTENCE_PATH") or None
```

- Pass `strategy_ids=strategy_ids` into `ExecutionConfig(...)`.
- Pass to `AppConfig(...)`:

```python
        live_enabled=live_enabled,
        live_orders_enabled=live_orders_enabled,
        persistence=PersistenceConfig(path=persistence_path),
```

- Add `serve_config` and export it:

```python
def serve_config(config: AppConfig, *, broker_id: BrokerId, mode: str) -> AppConfig:
    """Apply the serve command's explicit broker/mode to a loaded config.

    A live serve is the operator's deliberate enablement, so it sets the live
    gate and gives the session a durable store when the operator did not name
    one. Paper/backtest never acquire a persistence path here.
    """
    changes: dict[str, Any] = {"broker_id": broker_id, "mode": mode}
    if mode == "live":
        changes["live_enabled"] = True
        if not config.persistence.path:
            changes["persistence"] = PersistenceConfig(
                path=str(
                    Path(config.runtime_dir)
                    / "live"
                    / f"{broker_id.value.lower()}.sqlite"
                )
            )
    return replace(config, **changes)


__all__ = ["_parse_bool", "from_env", "load_env_file", "serve_config"]
```

- [ ] **Step 4: Add `strategy_ids` to the schema**

In `ExecutionConfig`, after `pending_max_age_bars`:

```python
    #: Strategies this session may wire by name. Live requires at least one;
    #: paper/backtest ignore it. Discovered examples are never implied by it.
    strategy_ids: tuple[str, ...] = ()
```

At the top of the existing `ExecutionConfig.__post_init__`, add:

```python
        object.__setattr__(
            self,
            "strategy_ids",
            tuple(
                dict.fromkeys(
                    item.strip() for item in self.strategy_ids if item.strip()
                )
            ),
        )
```

- [ ] **Step 5: Run the tests**

```bash
uv run --no-sync python -m pytest config/tests -q -p no:cacheprovider
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add config/src/tradex_config/env.py config/src/tradex_config/schema.py config/tests/test_env_strict.py
git commit -m "feat(wp2): strict env parsing, live controls, strategy ids and serve_config"
```

---

### Task 7: WP2 — Live boots only the strategies the operator names

**Files:**
- Modify: `runtime/src/tradex_runtime/startup.py:868-1036`
- Test: `runtime/tests/test_strategy_selection.py` (create)
- Rewrite: `trading/tests/runtime/test_live_strategy_wiring.py`, `trading/tests/runtime/test_live_boots_no_example_strategies.py`

**Interfaces:**
- Consumes: `AppConfig.execution.strategy_ids` and `auto_register_examples` from Task 6.
- Produces: `_select_strategies(cfg, all_strategies, all_definitions) -> tuple[tuple[Any, ...], tuple[Any, ...]]`. Live refuses `auto_register_examples`, refuses zero ids, refuses unknown ids, and returns only named instances/definitions.

- [ ] **Step 1: Write the failing unit test**

```python
"""Live strategy selection is explicit; examples are never implied."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from tradex_config.schema import AppConfig
from tradex_runtime.startup import _select_strategies


class _Strategy:
    def __init__(self, strategy_id: str) -> None:
        self.strategy_id = strategy_id


class _Definition:
    def __init__(self, definition_id: str) -> None:
        self.spec = SimpleNamespace(id=definition_id)


def _cfg(mode: str, *, ids: tuple[str, ...] = (), auto: bool = False) -> AppConfig:
    cfg = AppConfig(mode=mode)
    return replace(
        cfg,
        execution=replace(
            cfg.execution, strategy_ids=ids, auto_register_examples=auto
        ),
    )


DEPLOYED = _Strategy("deployed")
EXAMPLE = _Strategy("sma_cross_example")
DEFINITION = _Definition("sma_cross")


def test_live_refuses_auto_registered_examples() -> None:
    with pytest.raises(ValueError, match="auto_register_examples"):
        _select_strategies(_cfg("live", auto=True), (EXAMPLE,), ())


def test_live_requires_at_least_one_named_strategy() -> None:
    with pytest.raises(ValueError, match="TRADEX_STRATEGY_IDS"):
        _select_strategies(_cfg("live"), (EXAMPLE,), ())


def test_live_refuses_an_unknown_strategy_id() -> None:
    with pytest.raises(ValueError, match="not_a_strategy"):
        _select_strategies(_cfg("live", ids=("not_a_strategy",)), (DEPLOYED,), ())


def test_live_returns_only_the_named_instance_and_definition() -> None:
    strategies, definitions = _select_strategies(
        _cfg("live", ids=("deployed", "sma_cross")),
        (DEPLOYED, EXAMPLE),
        (DEFINITION,),
    )

    assert strategies == (DEPLOYED,)
    assert definitions == (DEFINITION,)


def test_paper_keeps_the_auto_register_switch() -> None:
    strategies, definitions = _select_strategies(
        _cfg("paper", auto=True), (EXAMPLE,), (DEFINITION,)
    )
    assert strategies == (EXAMPLE,)
    assert definitions == (DEFINITION,)

    empty, no_defs = _select_strategies(_cfg("paper"), (EXAMPLE,), (DEFINITION,))
    assert empty == ()
    assert no_defs == ()
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run --no-sync python -m pytest runtime/tests/test_strategy_selection.py -q -p no:cacheprovider
```

Expected: FAIL with `ImportError: cannot import name '_select_strategies'`.

- [ ] **Step 3: Implement the selector**

Add above `boot()` in `runtime/src/tradex_runtime/startup.py`:

```python
def _select_strategies(
    cfg: AppConfig,
    all_strategies: Sequence[Any],
    all_definitions: Sequence[Any],
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Choose what this boot may wire.

    Discovery is not consent. Live mode wires exactly the ids the operator
    named, and refuses the example auto-register switch outright. Paper and
    backtest keep the historical switch.
    """
    if cfg.mode == "live":
        if cfg.execution.auto_register_examples:
            raise ValueError(
                "auto_register_examples is not allowed in live mode; name the "
                "strategies to trade with TRADEX_STRATEGY_IDS"
            )
        selected = set(cfg.execution.strategy_ids)
        if not selected:
            raise ValueError(
                "live mode requires TRADEX_STRATEGY_IDS naming the strategies "
                "to trade; discovery is not consent"
            )
        known = {strategy.strategy_id for strategy in all_strategies}
        known |= {definition.spec.id for definition in all_definitions}
        unknown = sorted(selected - known)
        if unknown:
            raise ValueError(
                f"TRADEX_STRATEGY_IDS names unknown strategies: {', '.join(unknown)}"
            )
        strategies = tuple(
            strategy
            for strategy in all_strategies
            if strategy.strategy_id in selected
        )
        definitions = tuple(
            definition
            for definition in all_definitions
            if definition.spec.id in selected
        )
        return strategies, definitions

    if not cfg.execution.auto_register_examples:
        return (), ()
    return tuple(all_strategies), tuple(all_definitions)
```

- [ ] **Step 4: Wire the selector into `boot`**

Replace:

```python
        # The discovered examples are bound to a hardcoded instrument and place
        # real orders. They stay discoverable for the chart/backtest route, but
        # a session only trades them when explicitly asked — otherwise a live
        # account would run `sma_cross_example` on RELIANCE out of the box, and
        # SELL (never cash-gated) would put on a real short.
        strategies = all_strategies if cfg.execution.auto_register_examples else ()
```

with:

```python
        # Live wires only the ids the operator named; paper/backtest keep the
        # auto-register switch. Discovery is not consent.
        strategies, selected_definitions = _select_strategies(
            cfg, all_strategies, all_definitions
        )
```

Then:
- `per_binding_active = bool(selected_definitions and universe)`.
- In the suppression loop, iterate `selected_definitions` instead of `all_definitions`.
- In the per-binding registration loop, iterate `selected_definitions` instead of `all_definitions`.
- Leave both validation loops iterating the **full** `all_strategies` / `all_definitions`: a strategy that was not selected can still refuse the boot if its declared spec is impossible.
- The final live zero-strategy check stays as a second line of defence.

- [ ] **Step 5: Rewrite the live wiring tests**

In `trading/tests/runtime/test_live_strategy_wiring.py`:
- `_live_cfg` gains `strategy_ids: tuple[str, ...] = ()`, passed as `execution=replace(cfg.execution, strategy_ids=strategy_ids)`.
- `test_live_boot_without_strategies_raises` matches `"TRADEX_STRATEGY_IDS"`.
- Delete `test_live_boot_with_strategies_succeeds` (it caught `RuntimeError` and asserted nothing) and add:

```python
def test_live_boot_refuses_the_example_auto_register_switch(named_tape, tmp_path) -> None:
    config = _live_cfg(tmp_path, auto_register_examples=True)
    with pytest.raises(ValueError, match="auto_register_examples"):
        boot(config, wire_strategies=True, broker=PaperBroker())
```

In `trading/tests/runtime/test_live_boots_no_example_strategies.py`:
- Default live boot now refuses instead of silently wiring nothing:

```python
def test_a_live_boot_refuses_with_no_named_strategy(named_tape, tmp_path) -> None:
    from tradex_runtime.startup import boot

    with pytest.raises(ValueError, match="TRADEX_STRATEGY_IDS"):
        boot(_live_cfg(tmp_path))
```

- `test_a_deliberately_enabled_strategy_is_still_registered`: set `strategy_ids=("deployed",)` and `auto_register_examples=False`.
- `test_per_binding_definition_suppresses_module_twin`: set `strategy_ids=("sma_cross",)` and `auto_register_examples=False`.
- Add:

```python
def test_a_live_boot_wires_no_unnamed_example(
    named_tape, monkeypatch, tmp_path,
) -> None:
    from dataclasses import replace

    from tradex_runtime import startup as sm

    class _Deployed:
        strategy_id = "deployed"
        version = "1.0.0"

        def on_bar(self, context, candle):
            return None

        def on_quote(self, context, quote):
            return None

        def on_depth(self, context, depth):
            return None

        def on_fill(self, context, fill):
            return None

    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env", lambda *a, **k: _live_broker()
    )
    monkeypatch.setattr("tradex_runtime.startup.all_strategies", (_Deployed(), *_all_examples()))
    cfg = _live_cfg(tmp_path)
    cfg = replace(cfg, execution=replace(cfg.execution, strategy_ids=("deployed",)))
    session = sm.boot(cfg)
    try:
        engine = session.strategy_engine
        wired = set(getattr(engine, "_strategies", {}) or {})
        assert wired == {"deployed"}
    finally:
        session.stop()


def _all_examples():
    from tradex_strategy.extensions import all_strategies

    return tuple(all_strategies)
```

- Paper tests keep `auto_register_examples=True`.

- [ ] **Step 6: Run the affected suites**

```bash
uv run --no-sync python -m pytest runtime/tests/test_strategy_selection.py \
  trading/tests/runtime/test_live_strategy_wiring.py \
  trading/tests/runtime/test_live_boots_no_example_strategies.py \
  -q -p no:cacheprovider
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add runtime/src/tradex_runtime/startup.py runtime/tests/test_strategy_selection.py \
  trading/tests/runtime/test_live_strategy_wiring.py \
  trading/tests/runtime/test_live_boots_no_example_strategies.py
git commit -m "feat(wp2): live wires only operator-named strategies and refuses examples"
```

---

### Task 8: WP2 — Connect the live order gate and the serve entrypoints

**Files:**
- Modify: `runtime/src/tradex_runtime/startup.py:458-467`, `interfaces/src/tradex_interfaces/fastapi_app.py:452-506`, `interfaces/src/tradex_interfaces/cli.py:437-508`
- Test: `trading/tests/runtime/test_live_order_gate.py` (create), `trading/tests/interface/test_serve_strategy_wiring.py` (create)

**Interfaces:**
- Consumes: `AppConfig.live_orders_enabled` (Task 6), `serve_config` (Task 6), `strategy_ids` (Task 7).
- Produces: `build_broker_from_env(..., allow_order_operations=cfg.live_orders_enabled)`; `serve_app`/`cmd_serve` boot through `serve_config` and pass `wire_strategies=bool(strategy_ids)` for live, `False` for paper.

- [ ] **Step 1: Write the failing broker-gate test**

```python
"""The adapter's own order wall must follow `live_orders_enabled`."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from unittest.mock import MagicMock

from tradex_config.schema import AppConfig, PersistenceConfig
from tradex_domain import BrokerId
from tradex_runtime.startup import boot
from trading.tests.risk_support import permissive_risk


class _Deployed:
    strategy_id = "deployed"
    version = "1.0.0"

    def on_bar(self, context, candle):
        return None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        return None


def _broker() -> MagicMock:
    broker = MagicMock()
    backend = MagicMock()
    backend.subscribe_orders = MagicMock()
    broker.stream_backend.return_value = backend
    broker.get_orderbook.return_value = []
    broker.get_positions.return_value = []
    broker.master_loader = None
    account = MagicMock()
    account.balance.amount = Decimal("500000")
    account.account_id.value = "acct-1"
    broker.get_account.return_value = account
    return broker


def _cfg(tmp_path, *, orders: bool) -> AppConfig:
    cfg = AppConfig(
        risk=permissive_risk(),
        mode="live",
        broker_id=BrokerId.DHAN,
        live_enabled=True,
        live_orders_enabled=orders,
        persistence=PersistenceConfig(path=str(tmp_path / "state.db")),
        execution=replace(
            AppConfig().execution, strategy_ids=("deployed",)
        ),
    )
    return cfg


def _boot_and_capture(monkeypatch, named_tape, tmp_path, *, orders: bool):
    seen: dict = {}

    def fake_build(provider, **kwargs):
        seen.update(kwargs)
        return _broker()

    monkeypatch.setattr(
        "tradex_runtime.live.build_broker_from_env", fake_build
    )
    monkeypatch.setattr("tradex_runtime.startup.all_strategies", (_Deployed(),))
    session = boot(_cfg(tmp_path, orders=orders))
    session.stop()
    return seen


def test_the_order_gate_is_closed_by_default(monkeypatch, named_tape, tmp_path) -> None:
    seen = _boot_and_capture(monkeypatch, named_tape, tmp_path, orders=False)
    assert seen["allow_order_operations"] is False


def test_the_order_gate_opens_only_when_configured(monkeypatch, named_tape, tmp_path) -> None:
    seen = _boot_and_capture(monkeypatch, named_tape, tmp_path, orders=True)
    assert seen["allow_order_operations"] is True
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run --no-sync python -m pytest trading/tests/runtime/test_live_order_gate.py -q -p no:cacheprovider
```

Expected: FAIL — `seen` has no `allow_order_operations`, because `startup` never passes it.

- [ ] **Step 3: Pass the gate to the builder**

In `runtime/src/tradex_runtime/startup.py`:

```python
        if cfg.mode == "live":
            from tradex_runtime.live import build_broker_from_env
            broker = build_broker_from_env(
                cfg.broker_id.value,
                allow_order_operations=cfg.live_orders_enabled,
            )
```

- [ ] **Step 4: Write the failing serve-wiring test**

```python
"""`tradex serve` must boot through the same config and strategy gate."""

from __future__ import annotations

import pytest
from tradex_interfaces import fastapi_app as fa


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(fa._SERVE_ENV_BROKER, "DHAN")
    monkeypatch.setenv(fa._SERVE_ENV_MODE, "live")
    monkeypatch.setenv(fa._SERVE_ENV_API_KEY, "test-key")
    monkeypatch.setenv(fa._SERVE_ENV_HOST, "127.0.0.1")
    monkeypatch.delenv("TRADEX_STRATEGY_IDS", raising=False)
    monkeypatch.delenv("TRADEX_PERSISTENCE_PATH", raising=False)


def _capture_boot(monkeypatch):
    import tradex_runtime.startup as startup

    captured: dict = {}

    def fake_boot(config, **kwargs):
        captured["config"] = config
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(startup, "boot", fake_boot)
    monkeypatch.setattr(fa, "create_app", lambda session, api_key=None: "app")
    return captured


def test_live_serve_enables_live_and_passes_named_strategies(monkeypatch) -> None:
    captured = _capture_boot(monkeypatch)
    monkeypatch.setenv("TRADEX_STRATEGY_IDS", "deployed")

    assert fa.serve_app() == "app"

    config = captured["config"]
    assert config.live_enabled is True
    assert config.persistence.path
    assert captured["kwargs"]["wire_strategies"] is True


def test_live_serve_without_named_strategies_does_not_wire_discovery(monkeypatch) -> None:
    captured = _capture_boot(monkeypatch)

    assert fa.serve_app() == "app"

    assert captured["kwargs"]["wire_strategies"] is False
```

- [ ] **Step 5: Run it to verify it fails**

```bash
uv run --no-sync python -m pytest trading/tests/interface/test_serve_strategy_wiring.py -q -p no:cacheprovider
```

Expected: FAIL — `serve_app` passes `wire_strategies=True` unconditionally for live and builds config with `replace`, not `serve_config` (so `persistence.path` stays empty in the no-path case).

- [ ] **Step 6: Rewrite both serve entrypoints**

In `interfaces/src/tradex_interfaces/fastapi_app.py`:

```python
    from tradex_config.env import from_env, serve_config
    from tradex_domain import BrokerId

    config = serve_config(from_env(), broker_id=BrokerId(broker), mode=mode)
    wire = bool(config.execution.strategy_ids) if mode == "live" else False
    if mode == "paper":
        session = boot(
            config, wire_strategies=False,
            reconciliation_monitor_factory=build_reconciliation_monitor,
        )
    elif mode == "live":
        session = boot(
            config, wire_strategies=wire,
            reconciliation_monitor_factory=build_reconciliation_monitor,
        )
    else:
        session = boot(
            config, wire_strategies=False,
            reconciliation_monitor_factory=build_reconciliation_monitor,
        )
```

Delete the now-unused `from dataclasses import replace` inside `serve_app` if nothing else in the function uses it.

In `interfaces/src/tradex_interfaces/cli.py`, inside `cmd_serve`:

```python
            from tradex_config.env import from_env, serve_config

            config = serve_config(from_env(), broker_id=broker_id, mode=mode)
            session = boot(
                config,
                wire_strategies=(
                    bool(config.execution.strategy_ids) if mode == "live" else False
                ),
                reconciliation_monitor_factory=build_reconciliation_monitor,
            )
```

Remove the `replace(...)` construction and the `from tradex_domain import BrokerId` import if it becomes unused (it is still used for `broker_id = BrokerId(args.broker)`).

- [ ] **Step 7: Run the affected suites**

```bash
uv run --no-sync python -m pytest trading/tests/runtime/test_live_order_gate.py \
  trading/tests/interface/test_serve_strategy_wiring.py \
  trading/tests/interface -q -p no:cacheprovider
```

Expected: PASS, with no new failures in `trading/tests/interface`.

- [ ] **Step 8: Commit**

```bash
git add runtime/src/tradex_runtime/startup.py interfaces/src/tradex_interfaces/fastapi_app.py \
  interfaces/src/tradex_interfaces/cli.py trading/tests/runtime/test_live_order_gate.py \
  trading/tests/interface/test_serve_strategy_wiring.py
git commit -m "feat(wp2): wire the live order gate and serve strategy selection"
```

---

### Task 9: WP2 — Session lifecycle: READY only after monitors, engine shut down on stop

**Files:**
- Modify: `runtime/src/tradex_runtime/session_impl.py:214-335`
- Test: `trading/tests/runtime/test_session_feed_lifecycle.py`
- Update comments only: `tests/regression/test_sigterm_lifespan_teardown.py`

**Interfaces:**
- Produces: `TradingSession.start()` starts every configured monitor, unwinds the ones it started if one fails, and sets `READY` only after all succeed. `TradingSession.stop()` calls `engine.shutdown()` exactly once, including when the session never left `NEW`, before the bus is disposed.

- [ ] **Step 1: Write the failing lifecycle tests**

Extend `trading/tests/runtime/test_session_feed_lifecycle.py`:

```python
class _FailingMonitor:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1
        raise RuntimeError("monitor down")

    def stop(self) -> None:
        self.stops += 1


def test_a_failing_monitor_keeps_the_session_new_and_unwinds_started_ones() -> None:
    feed = _FakeMonitor()
    reconciliation = _FailingMonitor()
    session = _session(
        feed_monitor=feed, reconciliation_monitor=reconciliation
    )

    with pytest.raises(RuntimeError, match="monitor down"):
        session.start()

    assert session.state is SessionState.NEW
    assert feed.starts == 1
    assert feed.stops == 1, "a monitor this start started must be stopped again"
    assert reconciliation.stops == 0, "a monitor that never started is not stopped"


def test_stop_shuts_down_the_execution_engine() -> None:
    engine = MagicMock()
    session = _session(engine=engine)
    session.start()

    session.stop()

    engine.shutdown.assert_called_once_with()


def test_stop_after_a_failed_start_still_shuts_down_the_engine() -> None:
    engine = MagicMock()
    session = _session(engine=engine, feed_monitor=_FailingMonitor())

    with pytest.raises(RuntimeError, match="monitor down"):
        session.start()

    session.stop()

    engine.shutdown.assert_called_once_with()
    assert session.state is SessionState.STOPPED


def test_a_shutdown_error_does_not_skip_the_rest_of_teardown() -> None:
    engine = MagicMock()
    engine.shutdown.side_effect = RuntimeError("engine teardown failed")
    broker = MagicMock()
    session = _session(engine=engine, broker=broker)
    session.start()

    session.stop()

    assert session.state is SessionState.STOPPED
    broker.close.assert_called()
```

- [ ] **Step 2: Run them to verify they fail**

```bash
uv run --no-sync python -m pytest trading/tests/runtime/test_session_feed_lifecycle.py -q -p no:cacheprovider
```

Expected: FAIL — `start()` sets READY before monitors and swallows the failure; `stop()` never calls `engine.shutdown()`.

- [ ] **Step 3: Rewrite `start()`**

```python
    def start(self) -> None:
        """Transition to READY only after every owned monitor has started."""
        if self._state == SessionState.READY:
            return
        if self._state not in (SessionState.NEW,):
            raise SessionStateError(
                f"Cannot start session in {self._state} state (must be NEW)"
            )
        sid = getattr(self, "_session_id", id(self))
        log.info("Session %s starting", sid)

        started: list[object] = []
        try:
            for monitor in (self._feed_monitor, self._reconciliation_monitor):
                if monitor is None:
                    continue
                monitor.start()  # type: ignore[attr-defined]
                started.append(monitor)
        except Exception:
            for monitor in reversed(started):
                try:
                    monitor.stop()  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover – defensive unwind
                    log.warning("monitor unwind failed", exc_info=True)
            raise

        self._state = SessionState.READY
```

- [ ] **Step 4: Shut the engine down in `stop()`**

Add a helper next to `_run_stop_hooks`:

```python
    def _shutdown_engine(self) -> None:
        """Halt order intake and release the engine's durable handles."""
        shutdown = getattr(self._engine, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:  # pragma: no cover – defensive teardown
                log.warning("execution engine shutdown failed", exc_info=True)
```

In `stop()`, on the `NEW` path:

```python
            if self._state == SessionState.NEW:
                self._shutdown_engine()
                return  # never started, nothing else to stop
```

Call it again on the normal path immediately before the bus is disposed:

```python
            self._shutdown_engine()
            self._bus.dispose()
```

The engine is shut down before the bus disappears so no subscriber can publish after the kill switch trips. `stop()` is already guarded by the `STOPPED` early return, so the engine sees exactly one shutdown per session.

- [ ] **Step 5: Update stale comments in the lifespan regression test**

In `tests/regression/test_sigterm_lifespan_teardown.py`, the module docstring and `test_engine_shutdown_sets_kill_switch_and_closes_the_durable_guard` say `session.stop()` does not shut the engine down. Change those sentences to state that `session.stop()` now calls `engine.shutdown()`; keep the test body (it calls `engine.shutdown()` directly and remains valid). Do not add new assertions there.

- [ ] **Step 6: Run the lifecycle and lifespan tests**

```bash
uv run --no-sync python -m pytest trading/tests/runtime/test_session_feed_lifecycle.py \
  tests/regression/test_sigterm_lifespan_teardown.py -q -p no:cacheprovider
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add runtime/src/tradex_runtime/session_impl.py \
  trading/tests/runtime/test_session_feed_lifecycle.py \
  tests/regression/test_sigterm_lifespan_teardown.py
git commit -m "fix(wp2): READY only after monitors; stop shuts down the engine"
```

---

### Task 10: WP2 — Correlation ids are scoped to a run, not to the tape

**Files:**
- Modify: `strategy/src/tradex_strategy/core/engine.py:5-17` (imports), `:79-90` (constructor), `:124-127` (docstring), `:160-163` (state), `:250-262` (`_correlation_id`)
- Test: `trading/tests/contracts/test_strategy_correlation_scope.py` (create)

**Interfaces:**
- Consumes: `ExecutionEngine.submit` handed to `ReactiveStrategyEngine` by `_boot_tail` (Task 8).
- Produces: keyword-only constructor argument `run_id: str | None = None`. Omitted means a fresh `uuid4().hex` per engine, which is what every production boot does. Later tasks do not depend on the parameter.

**Why:** the seed was `strategy_id:event_type:timestamp:seq`. Two separate processes replaying the same tape — a paper session restarted after a crash, two backtests, or a replay after live — produced the *same* correlation id for the same signal. The durable idempotency guard keys on it, so the second process's first signal replays the first process's receipt and never reaches the venue. Scope the id to a run.

- [ ] **Step 1: Write the failing test**

```python
"""A run's correlation ids must be reproducible inside the run and unique
across runs.

The id used to be seeded by ``strategy_id:event_type:timestamp:seq`` only, so
two processes replaying the same tape emitted the same id for the same signal.
The durable idempotency guard keys on that id: the second process replayed the
first process's receipt and never reached the venue.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import Candle, OHLC, OrderRequest, OrderSide, Signal
from tradex_domain.enums import ExchangeId, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_reactive.bus import ReactiveBus
from tradex_strategy.core.engine import ReactiveStrategyEngine
from tradex_strategy.core.sizing import PositionSizer

INSTRUMENT = Equity.of(ExchangeId.NSE, "RELIANCE")


class _Buyer:
    strategy_id = "corr_test"
    version = "1.0.0"

    def on_start(self, context) -> None:
        """No-op."""

    def on_stop(self, context) -> None:
        """No-op."""

    def on_bar(self, context, bar) -> Signal:
        return Signal(
            instrument=INSTRUMENT,
            direction=OrderSide.BUY,
            strength=1.0,
            reason="corr_scope",
        )

    def on_quote(self, context, quote):
        return None

    def on_fill(self, context, fill) -> None:
        """No-op."""

    def on_event(self, event) -> None:
        """No-op."""


def _candle(minute: int) -> Candle:
    price = Price(value=Decimal("100"))
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.M1,
        ohlc=OHLC(open=price, high=price, low=price, close=price),
        volume=Quantity(value=Decimal("10")),
        timestamp=datetime(2026, 8, 7, 9, minute, tzinfo=UTC),
    )


def _sizer() -> PositionSizer:
    return PositionSizer(
        max_order_value=Decimal("10000"),
        fallback_notional=Decimal("10000"),
    )


def _submitted(run_id: str | None, minute: int) -> list[OrderRequest]:
    bus = ReactiveBus()
    requests: list[OrderRequest] = []
    engine = ReactiveStrategyEngine(
        bus,
        requests.append,
        fill_reference="signal_close",
        sizer=_sizer(),
        run_id=run_id,
    )
    engine.register(_Buyer())
    try:
        bus.publish(_candle(minute))
    finally:
        engine.dispose_all()
    return requests


def test_same_run_id_reproduces_the_same_correlation_id() -> None:
    first = _submitted("run-a", 0)
    second = _submitted("run-a", 0)
    assert len(first) == len(second) == 1
    assert first[0].correlation_id == second[0].correlation_id


def test_different_run_ids_never_collide() -> None:
    first = _submitted("run-a", 0)
    second = _submitted("run-b", 0)
    assert len(first) == len(second) == 1
    assert first[0].correlation_id != second[0].correlation_id


def test_default_runs_do_not_collide() -> None:
    first = _submitted(None, 0)
    second = _submitted(None, 0)
    assert len(first) == len(second) == 1
    assert first[0].correlation_id != second[0].correlation_id


def test_distinct_events_in_one_run_still_differ() -> None:
    bus = ReactiveBus()
    requests: list[OrderRequest] = []
    engine = ReactiveStrategyEngine(
        bus,
        requests.append,
        fill_reference="signal_close",
        sizer=_sizer(),
        run_id="run-a",
    )
    engine.register(_Buyer())
    try:
        bus.publish(_candle(0))
        bus.publish(_candle(1))
    finally:
        engine.dispose_all()
    assert len(requests) == 2
    assert requests[0].correlation_id != requests[1].correlation_id
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run --no-sync python -m pytest trading/tests/contracts/test_strategy_correlation_scope.py \
  -q -p no:cacheprovider
```

Expected: FAIL — `TypeError: ... __init__() got an unexpected keyword argument 'run_id'`.

- [ ] **Step 3: Implement the run scope**

Add `import uuid` after `import logging`.

Add the parameter after `balance_provider` in the constructor:

```python
        balance_provider: Callable[[], Money] | None = None,
        run_id: str | None = None,
```

Add its docstring entry after the `balance_provider` entry:

```
            run_id: names this engine's replay/live run. Correlation ids are
                scoped to it, so the same tape in two runs yields distinct
                ids while one run stays reproducible. ``None`` (default)
                generates a fresh id per engine.
```

Initialise the id next to `_signal_seq`:

```python
        # Correlation-id scope. Stable for one engine (a run is reproducible),
        # different for every process/engine (two runs never share a key).
        self._run_id = run_id if run_id is not None else uuid.uuid4().hex
```

Replace `_correlation_id`:

```python
    def _correlation_id(self, strategy_id: str, event: Any) -> CorrelationId:
        """Deterministic correlation id for a signal-triggered order.

        Derived from the run id + strategy id + event identity (timestamp +
        type) plus a per-signal nonce: the same replay stream reproduces
        identical ids *within its run* (idempotency-safe on restart of the
        same run), while a different run — a restarted paper session, a
        second backtest over the same tape — can never collide with it.
        """
        self._signal_seq += 1
        ts = getattr(event, "timestamp", None)
        seed = (
            f"{self._run_id}:{strategy_id}:{type(event).__name__}:"
            f"{ts}:{self._signal_seq}"
        )
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
        return CorrelationId(value=f"strat-{digest}")
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run --no-sync python -m pytest trading/tests/contracts/test_strategy_correlation_scope.py \
  trading/tests/contracts/test_ac22_single_path_to_venue.py \
  trading/tests/strategy -q -p no:cacheprovider
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add strategy/src/tradex_strategy/core/engine.py \
  trading/tests/contracts/test_strategy_correlation_scope.py
git commit -m "fix(wp2): scope strategy correlation ids to a run"
```

---

### Task 11: WP3 — Idempotency transactions close before a duplicate is read

**Files:**
- Modify: `execution/src/tradex_execution/sqlite_store.py:431-553`
- Test: `execution/tests/test_sqlite_idempotency_transaction.py` (create)

**Interfaces:**
- Consumes: `SQLiteIdempotencyGuard`, `IdempotencyInflight` (existing).
- Produces: no new API. The guard's connection is left with `in_transaction is False` on every return and every raise.

**Why:** `check_and_reserve` INSERTs (which opens a transaction), catches `IntegrityError`, and then SELECTs on the same connection without rolling back. The connection remains `in_transaction`, holding a read snapshot; on a shared file the next writer gets "database is locked". It only shows up when a client retries — the exact path idempotency exists for.

- [ ] **Step 1: Write the failing test**

```python
"""The idempotency guard must leave no transaction open when it refuses.

A duplicate reserve used to SELECT on the connection that still held the
failed INSERT's implicit transaction, so the connection stayed
``in_transaction``. On a shared file that pins a read snapshot and blocks the
next writer — the "database is locked" failure that only appears on retry.
"""

from __future__ import annotations

import sqlite3

import pytest
from tradex_domain.value_objects import CorrelationId
from tradex_execution.engine import IdempotencyInflight
from tradex_execution.sqlite_store import SQLiteIdempotencyGuard


def _visible_statuses(db_path, key: str) -> list[str]:
    """Read through a SECOND connection: only committed rows are visible."""
    with sqlite3.connect(str(db_path)) as other:
        rows = other.execute(
            "SELECT status FROM idempotency WHERE correlation_id = ?",
            (key,),
        ).fetchall()
    return [row[0] for row in rows]


def test_duplicate_reserve_leaves_no_open_transaction(tmp_path) -> None:
    db = tmp_path / "guard.db"
    key = CorrelationId(value="tx-1")
    guard = SQLiteIdempotencyGuard(db)
    try:
        assert guard.check_and_reserve(key) is None
        guard.record_result(key, "done")

        duplicate = guard.check_and_reserve(key)

        assert duplicate.result == "done"
        assert guard._conn.in_transaction is False  # noqa: SLF001
        assert _visible_statuses(db, "tx-1") == ["completed"]
    finally:
        guard.close()


def test_inflight_reserve_leaves_no_open_transaction(tmp_path) -> None:
    db = tmp_path / "guard.db"
    key = CorrelationId(value="tx-2")
    guard = SQLiteIdempotencyGuard(db)
    try:
        assert guard.check_and_reserve(key) is None

        with pytest.raises(IdempotencyInflight):
            guard.check_and_reserve(key)

        assert guard._conn.in_transaction is False  # noqa: SLF001
        assert _visible_statuses(db, "tx-2") == ["reserved"]
    finally:
        guard.close()


def test_fresh_reservation_is_committed_for_another_connection(tmp_path) -> None:
    db = tmp_path / "guard.db"
    guard = SQLiteIdempotencyGuard(db)
    try:
        assert guard.check_and_reserve(CorrelationId(value="tx-3")) is None
        assert guard._conn.in_transaction is False  # noqa: SLF001
        assert _visible_statuses(db, "tx-3") == ["reserved"]
    finally:
        guard.close()


def test_release_leaves_no_row_and_no_transaction(tmp_path) -> None:
    db = tmp_path / "guard.db"
    key = CorrelationId(value="tx-4")
    guard = SQLiteIdempotencyGuard(db)
    try:
        assert guard.check_and_reserve(key) is None
        guard.release(key)
        assert guard._conn.in_transaction is False  # noqa: SLF001
        assert _visible_statuses(db, "tx-4") == []
    finally:
        guard.close()
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run --no-sync python -m pytest execution/tests/test_sqlite_idempotency_transaction.py \
  -q -p no:cacheprovider
```

Expected: FAIL on `duplicate_reserve` and `inflight_reserve` — `assert True is False` for `in_transaction` (the failed INSERT's transaction is still open).

- [ ] **Step 3: Scope each write to a committed transaction**

In `check_and_reserve`, wrap only the INSERT — not the duplicate branch — so `IntegrityError` leaves the `with` by the exceptional path and rolls back before the SELECTs:

```python
        key = str(correlation_id.value)
        with self._write_lock:
            try:
                with self._conn:  # commit on success, ROLLBACK on IntegrityError
                    if request_hash is not None:
                        self._conn.execute(
                            "INSERT INTO idempotency (correlation_id, status, request_hash) "
                            "VALUES (?, 'reserved', ?)",
                            (key, request_hash),
                        )
                    else:
                        self._conn.execute(
                            "INSERT INTO idempotency (correlation_id, status) VALUES (?, 'reserved')",
                            (key,),
                        )
                return None  # fresh reservation
            except sqlite3.IntegrityError:
```

Everything from the `row = self._conn.execute("SELECT ...")` line onward is unchanged.

In `record_result`, replace the explicit commit with the same transaction scope:

```python
        with self._write_lock, self._conn:
            self._conn.execute(
                "UPDATE idempotency SET status = 'completed', result = ? "
                "WHERE correlation_id = ?",
                (encoded, key),
            )
```

In `release`:

```python
        with self._write_lock, self._conn:
            self._conn.execute(
                "DELETE FROM idempotency WHERE correlation_id = ? AND status = 'reserved'",
                (key,),
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run --no-sync python -m pytest execution/tests/test_sqlite_idempotency_transaction.py \
  execution/tests/test_idempotency_completion_order.py \
  trading/tests/execution/test_sqlite_store.py -q -p no:cacheprovider
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add execution/src/tradex_execution/sqlite_store.py \
  execution/tests/test_sqlite_idempotency_transaction.py
git commit -m "fix(wp3): commit or roll back each idempotency write before reading"
```

---

### Task 12: WP3 — One receipt type for a submission and its replay

**Files:**
- Modify: `execution/src/tradex_execution/sqlite_store.py:24-35` (imports), `:43-73` (allowlist), `:496-516` (decode), `:523-543` (`record_result`)
- Modify: `execution/src/tradex_execution/engine.py:836-838` (ACK completion), `:938-939` (fill completion)
- Modify: `interfaces/src/tradex_interfaces/routes/orders.py:33-51` (imports), `:100-120` (`_map_receipt`)
- Test: `execution/tests/test_sqlite_receipt_replay.py` (create)
- Test: `trading/tests/execution/test_sqlite_store.py:149-192`, `trading/tests/execution/test_bracket_engine_pipeline.py:73-92`, `trading/tests/application/test_orders.py:111-125`, `trading/tests/interface/test_bracket_order.py:231-243`, `execution/tests/test_idempotency_completion_order.py:102-113`

**Interfaces:**
- Consumes: `OrderReceipt` (frozen slots dataclass, `order_id`, `status=SUBMITTED`, `message="submitted"`; picklable).
- Produces: `check_and_reserve(...).result` is always an `OrderReceipt` for completed keys — never a bare `OrderId`. `_map_receipt` accepts only `OrderReceipt`-shaped objects.

**Why:** an ACK-only submission recorded a bare `OrderId`; every caller then had to branch on "receipt or replay id". Recording the receipt itself removes the branch and makes the replay report the same status and message.

- [ ] **Step 1: Write the failing test**

```python
"""An idempotency replay must return the same typed receipt as the first call."""

from __future__ import annotations

import json
import sqlite3

from tradex_domain.enums import OrderStatus
from tradex_domain.execution import OrderReceipt
from tradex_domain.value_objects import CorrelationId, OrderId
from tradex_execution.sqlite_store import SQLiteIdempotencyGuard


def test_receipt_round_trips_through_the_guard(tmp_path) -> None:
    db = tmp_path / "guard.db"
    key = CorrelationId(value="receipt-1")
    receipt = OrderReceipt(
        order_id=OrderId(value="o-1"),
        status=OrderStatus.ACK,
        message="submitted",
    )

    first = SQLiteIdempotencyGuard(db)
    try:
        assert first.check_and_reserve(key) is None
        first.record_result(key, receipt)
    finally:
        first.close()

    reopened = SQLiteIdempotencyGuard(db)
    try:
        duplicate = reopened.check_and_reserve(key)
        assert isinstance(duplicate.result, OrderReceipt)
        assert duplicate.result == receipt
    finally:
        reopened.close()


def test_legacy_order_id_marker_decodes_to_a_receipt(tmp_path) -> None:
    """A row written by the previous release still replays, as a receipt."""
    db = tmp_path / "guard.db"
    guard = SQLiteIdempotencyGuard(db)
    try:
        with sqlite3.connect(str(db)) as other:
            other.execute(
                "INSERT INTO idempotency (correlation_id, status, result) "
                "VALUES (?, 'completed', ?)",
                ("legacy-1", json.dumps({"__tradex_order_id__": "old-7"})),
            )

        duplicate = guard.check_and_reserve(CorrelationId(value="legacy-1"))
        assert isinstance(duplicate.result, OrderReceipt)
        assert duplicate.result.order_id == OrderId(value="old-7")
        assert duplicate.result.status is OrderStatus.SUBMITTED
        assert duplicate.result.message == "idempotency_replay"
    finally:
        guard.close()
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run --no-sync python -m pytest execution/tests/test_sqlite_receipt_replay.py \
  -q -p no:cacheprovider
```

Expected: FAIL — `assert isinstance(duplicate.result, OrderReceipt)` is False; the stored result is an `Order`.

- [ ] **Step 3: Store the receipt whole**

In `sqlite_store.py`:

- Import `OrderReceipt`: `from tradex_domain.execution import Order, OrderReceipt`.
- Widen the allowlist:

```python
_IDEMPOTENCY_EXTRA_CLASSES: frozenset[tuple[str, str]] = frozenset({
    ("tradex_domain.execution", "Order"),
    ("tradex_domain.execution", "OrderReceipt"),
})
```

- Update the module comment at line 59 from "an ``Order``" to "an ``Order`` or ``OrderReceipt``".
- In `record_result`, replace the `OrderId` branch:

```python
        if isinstance(result, (Order, OrderReceipt)):
            # Full fidelity: the domain object is pickled (local trusted store)
            # so a restart replays the exact record the first call returned.
            encoded = _PICKLE_PREFIX + base64.b64encode(
                pickle.dumps(result)
            ).decode("ascii")
        else:
            encoded = json.dumps(result, default=str)
```

- In the duplicate decode branch, change the legacy marker (line 514) to:

```python
                    if isinstance(decoded, dict) and "__tradex_order_id__" in decoded:
                        decoded = OrderReceipt(
                            order_id=OrderId(value=str(decoded["__tradex_order_id__"])),
                            status=OrderStatus.SUBMITTED,
                            message="idempotency_replay",
                        )
```

In `engine.py`, both completion sites record the receipt:

```python
                    self._guard.record_result(
                        reserved_cid,
                        OrderReceipt(
                            order_id=order.order_id,
                            status=order.status,
                            message="submitted",
                        ),
                    )
```

and in `_apply_fill_to_book`:

```python
        if self._guard is not None and cid is not None:
            self._guard.record_result(
                cid,
                OrderReceipt(
                    order_id=order.order_id,
                    status=order.status,
                    message="submitted",
                ),
            )
```

In `routes/orders.py`, delete the bare-`OrderId` branch and both now-unused constants:

```python
def _map_receipt(receipt: Any) -> OrderResponse:
    """Map an engine ``OrderReceipt`` → ``OrderResponse``.

    ``submit()`` returns one type for a first submission and for an
    idempotency replay (the recorded receipt); an in-flight reservation is
    surfaced as 409 before reaching this helper.
    """
    if getattr(receipt, "message", None) == IDEMPOTENCY_IN_FLIGHT:
        raise HTTPException(status_code=409, detail="idempotency key is being processed")
    return OrderResponse(
        order_id=receipt.order_id.value,
        status=str(receipt.status),
        message=receipt.message,
    )
```

Remove `IDEMPOTENCY_REPLAY,` and `SUBMITTED_STATUS,` from the `tradex_domain.constants` import. Keep `OrderId` (used by the modify/cancel handlers).

- [ ] **Step 4: Update the tests that asserted the bare id**

`trading/tests/execution/test_sqlite_store.py` — import `OrderReceipt` from `tradex_domain.execution` inside the test, and replace line 189:

```python
            assert isinstance(second, OrderReceipt)
            assert second.order_id.value == engine1.all_orders()[0].order_id.value
```

`trading/tests/execution/test_bracket_engine_pipeline.py:87-89`:

```python
    replay = engine.submit(_bracket_request("bracket-cid-1"))
    assert isinstance(replay, OrderReceipt)
    assert replay.order_id.value == "super-1"
    assert replay.status is OrderStatus.ACK
```

`trading/tests/application/test_orders.py:111-125` — rename and assert the receipt:

```python
def test_submit_order_idempotency_replay_returns_the_recorded_receipt():
    """Second call with the same correlation id returns the first receipt."""
    from tradex_domain.execution import OrderReceipt
    from tradex_execution.idempotency import MemoryIdempotencyGuard

    guard = MemoryIdempotencyGuard()
    engine = ExecutionEngine(
        bus=ReactiveBus(), fill_source=SimulatedFillSource(), idempotency_guard=guard
    )
    cid = "submit-idem-1"
    first = submit_order(engine, _submit_request(cid))
    second = submit_order(engine, _submit_request(cid))
    assert isinstance(first, OrderReceipt)
    assert isinstance(second, OrderReceipt)
    assert second.order_id == first.order_id
```

`trading/tests/interface/test_bracket_order.py:231-243`:

```python
def test_bracket_idempotent_replay_returns_original_id() -> None:
    """A completed-key replay surfaces the recorded receipt — the same
    contract as POST /orders."""
    session = _FakeSession(
        engine=_FakeEngine(
            result=OrderReceipt(
                order_id=OrderId(value="bracket-original"),
                message="idempotency_replay",
            )
        )
    )
    client = TestClient(create_app(session=session))
    resp = _post_bracket(client, "bracket-replay")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "order_id": "bracket-original",
        "status": "SUBMITTED",
        "message": "idempotency_replay",
    }
    assert len(session.engine.submitted) == 1
```

`execution/tests/test_idempotency_completion_order.py:111-113` — replace the comment:

```python
    # Fund the account and retry: the request must actually execute, not
    # replay a receipt for an order that never took effect. A replay comes
    # back with the recorded status (SUBMITTED) rather than FILLED, so the
    # status is the tell.
```

- [ ] **Step 5: Run the affected suites**

```bash
uv run --no-sync python -m pytest execution/tests/test_sqlite_receipt_replay.py \
  execution/tests/test_idempotency_completion_order.py \
  execution/tests/test_sqlite_idempotency_transaction.py \
  trading/tests/application/test_orders.py \
  trading/tests/interface/test_bracket_order.py \
  trading/tests/execution/test_sqlite_store.py \
  trading/tests/execution/test_bracket_engine_pipeline.py \
  -q -p no:cacheprovider
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add execution/src/tradex_execution/sqlite_store.py \
  execution/src/tradex_execution/engine.py \
  interfaces/src/tradex_interfaces/routes/orders.py \
  execution/tests/test_sqlite_receipt_replay.py \
  execution/tests/test_idempotency_completion_order.py \
  trading/tests/application/test_orders.py \
  trading/tests/interface/test_bracket_order.py \
  trading/tests/execution/test_sqlite_store.py \
  trading/tests/execution/test_bracket_engine_pipeline.py
git commit -m "fix(wp3): persist and replay the typed OrderReceipt"
```

### Task 13: No fabricated receipts — a durability failure raises

**Files:**
- Modify: `execution/src/tradex_execution/engine.py:621-629` (risk approval), `:678-683` (dispatch intent), `:910-919` (`_apply_fill_to_book`), delete `_no_receipt` `:1043-1058`, `_append` docstring `:338-355`
- Test: `trading/tests/execution/test_fail_closed_event_store.py` (rewrite)

**Interfaces:**
- Consumes: `ExecutionEngine(..., idempotency_guard=...)`, `MemoryIdempotencyGuard`, `InMemoryEventStore`, `SimulatedFillSource`.
- Produces: a durability failure at risk-approval, dispatch-intent, or fill-persist raises `RuntimeError`; the idempotency reservation is released; no receipt is fabricated. `_no_receipt` no longer exists.

Today the three pre-venue durability gates return a synthetic `REJECTED` receipt whose `order_id` (`uuid4()`) exists nowhere in the event log, and the fill gate returns an `ACK` order for a fill that was not persisted. A synchronous API caller then sees a successful HTTP 200 for an order the durable log cannot account for. There is no partial fix here: either the write is durable or the caller gets an error.

- [ ] **Step 1: Rewrite the failing test**

Replace `trading/tests/execution/test_fail_closed_event_store.py` entirely:

```python
"""Fail-closed: a durability failure raises instead of faking a receipt."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradex_domain import (
    CorrelationId,
    Equity,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
)
from tradex_domain.events import (
    BrokerOrderRequested,
    DomainEvent,
    ErrorOccurred,
    OrderFilled,
    RiskDecision,
)
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_execution.idempotency import MemoryIdempotencyGuard
from tradex_execution.recovery import InMemoryEventStore
from tradex_reactive.bus import ReactiveBus

CID = CorrelationId(value="fail-closed-1")


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
        correlation_id=CID,
    )


class _FailOnType(InMemoryEventStore):
    """In-memory log that raises for one event type only."""

    def __init__(self, fail_type: type[DomainEvent]) -> None:
        super().__init__()
        self._fail_type = fail_type

    def append(self, event: DomainEvent) -> int:
        if isinstance(event, self._fail_type):
            raise RuntimeError("disk full")
        return super().append(event)


class _PassRisk:
    """Minimal passing risk gate; KillSwitch.trip flips its live gate."""

    live_orders_enabled = True

    def check(self, request: OrderRequest) -> bool:
        return True


def test_dispatch_intent_failure_raises_and_releases_the_key() -> None:
    """No durable dispatch intent → no venue call, no fabricated receipt."""
    bus = ReactiveBus()
    errors: list[ErrorOccurred] = []
    bus.of_type(ErrorOccurred).subscribe(errors.append)
    guard = MemoryIdempotencyGuard()
    source = SimulatedFillSource()
    engine = ExecutionEngine(
        bus=bus,
        fill_source=source,
        idempotency_guard=guard,
        event_store=_FailOnType(BrokerOrderRequested),
        require_durable_events=True,
    )

    with pytest.raises(RuntimeError, match="dispatch_intent_not_durable"):
        engine.submit(_request())

    assert engine.kill_switch is True
    assert errors
    assert source.submit_calls == 0
    assert engine.cache.all_orders() == []
    # Released, not completed: the client may retry the same key.
    assert guard.check_and_reserve(CID) is None


def test_risk_approval_failure_raises_instead_of_a_fake_rejection() -> None:
    """Risk passed but its approval was not durable → stop before the venue."""
    guard = MemoryIdempotencyGuard()
    source = SimulatedFillSource()
    engine = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=source,
        risk_manager=_PassRisk(),
        idempotency_guard=guard,
        event_store=_FailOnType(RiskDecision),
        require_durable_events=True,
    )

    with pytest.raises(RuntimeError, match="risk_decision_not_durable"):
        engine.submit(_request())

    assert source.submit_calls == 0
    assert engine.cache.all_orders() == []
    assert guard.check_and_reserve(CID) is None


def test_dispatch_failure_without_the_durable_gate_still_raises() -> None:
    """Best-effort logging does not turn a missing dispatch record into an order."""
    engine = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=SimulatedFillSource(),
        event_store=_FailOnType(BrokerOrderRequested),
        require_durable_events=False,
    )

    with pytest.raises(RuntimeError, match="dispatch_intent_not_durable"):
        engine.submit(_request())

    assert engine.kill_switch is False


def test_fill_persist_failure_raises_and_leaves_the_book_unchanged() -> None:
    """The fill is authoritative: if it cannot be stored, nothing moves."""
    guard = MemoryIdempotencyGuard()
    engine = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=SimulatedFillSource(),
        idempotency_guard=guard,
        event_store=_FailOnType(OrderFilled),
        require_durable_events=True,
    )

    with pytest.raises(RuntimeError, match="not durable"):
        engine.submit(_request())

    assert engine.cache.all_positions() == []
    # The order was acknowledged; it was never filled.
    assert engine.cache.all_orders()[0].status is OrderStatus.ACK
    assert guard.check_and_reserve(CID) is None
```

- [ ] **Step 2: Run it to confirm the old behavior fails it**

```bash
uv run --no-sync python -m pytest trading/tests/execution/test_fail_closed_event_store.py -q -p no:cacheprovider
```

Expected: FAIL — `engine.submit` returns a synthetic `OrderReceipt` instead of raising for the first, second and fourth tests.

- [ ] **Step 3: Raise at all three durability gates**

Risk-approval failure (`engine.py:621-629`):

```python
        elif self._risk is not None:
            # Risk passed — persist the approval. A failed write means the log
            # cannot account for this order, so it must not reach the venue.
            if not self._append(RiskDecision(request=request, approved=True)):
                log.error(
                    "Risk approval not durable for %s; order not sent to venue",
                    request.correlation_id,
                )
                if reserved_cid is not None and self._guard is not None:
                    with self._guard_lock:
                        self._guard.release(reserved_cid)
                raise RuntimeError("risk_decision_not_durable")
```

Dispatch-intent failure (`engine.py:678-683`):

```python
        if not self._append(BrokerOrderRequested(request=request)):
            log.error(
                "Dispatch intent not durable for %s; order not sent to venue",
                request.correlation_id,
            )
            if reserved_cid is not None and self._guard is not None:
                with self._guard_lock:
                    self._guard.release(reserved_cid)
            raise RuntimeError("dispatch_intent_not_durable")
```

Fill-persist failure (`engine.py:910-919`) — replace the `return order`:

```python
        if not self._append(_filled_event):
            log.error(
                "Fill not durable; leaving the book unchanged for %s",
                fill.order_id,
            )
            raise RuntimeError(f"fill {fill.order_id.value} not durable")
```

The existing caller (`engine.py:812-823`) already releases the reservation on any exception and re-raises, so the fill path needs no further change. Delete `_no_receipt` (`engine.py:1043-1058`). In `_append`'s docstring, replace the sentence about a false return with: "A caller that must persist before acting treats ``False`` as fatal and raises; no receipt is fabricated for an order the log cannot account for."

- [ ] **Step 4: Run the file and the neighboring fail-closed suites**

```bash
uv run --no-sync python -m pytest trading/tests/execution/test_fail_closed_event_store.py \
  trading/tests/execution/test_fail_closed_risk.py \
  trading/tests/execution/test_fail_closed_feed.py \
  trading/tests/execution/test_engine_fee_schedule_gate.py \
  execution/tests/test_idempotency_completion_order.py \
  -q -p no:cacheprovider
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add execution/src/tradex_execution/engine.py \
  trading/tests/execution/test_fail_closed_event_store.py
git commit -m "fix(wp4): raise on a non-durable gate instead of faking a receipt"
```

### Task 14: One persistence authority — delete the order stores

**Files:**
- Modify: `runtime/src/tradex_runtime/startup.py:501-539` (step 4b), `:774-785` (step 6a), `_boot_tail` signature and both call sites, baseline-store comment; `execution/src/tradex_execution/engine.py:93-96` (re-export block)
- Modify: `execution/src/tradex_execution/sqlite_store.py` (delete `_order_to_row`, `_row_to_order`, `SQLiteOrderStore`, `OrderStore`, `InMemoryOrderStore`, `attach_order_persistence`, rewrite `__all__` and imports)
- Delete: `execution/tests/test_order_store_thread_safety.py`, `trading/tests/execution/test_persistence_seam.py`, `trading/tests/execution/test_sqlite_order_store_live.py`
- Modify: `trading/tests/execution/test_bracket_engine_pipeline.py`, `trading/tests/execution/test_sqlite_store.py`, `execution/tests/test_engine_v3_port.py`, `trading/tests/parity/test_live_tape_parity.py`, `trading/tests/execution/test_public_module_surface.py`, `trading/tests/runtime/test_durable_provider_integration.py:6`, `execution/src/tradex_execution/risk_baseline_store.py:12,53`
- Test: `trading/tests/execution/test_sqlite_store.py` (restart round-trip rewritten against `SQLiteEventStore`)

**Interfaces:**
- Consumes: `SQLiteEventStore` (already the live and paper durable log), `recover_trading_cache`.
- Produces: `tradex_execution.sqlite_store` exports only `SQLiteIdempotencyGuard`. No order rows are written outside the event log; restart reconstruction comes from `replay("orders")` only.

Two persistence authorities exist for the same facts: the hash-chained event log (written by `ExecutionEngine._append`) and a SQLite order table (written by `SQLiteOrderStore`). Live boot wires both, so a crash can leave the table and the log disagreeing with no rule for which wins. The order store has no reader on the live path: recovery rebuilds from the log. Delete the second authority.

- [ ] **Step 1: Rewrite the restart test against the event log**

In `trading/tests/execution/test_bracket_engine_pipeline.py`, replace the two restart tests (`:94-142` and `:145-208`) with event-log round trips. The file's `_engine(broker)` helper does not take a store, so construct the engines inline exactly as below:

```python
def test_bracket_survives_restart_and_still_super_cancels(tmp_path) -> None:
    """The event log keeps the protective legs across a restart, so a
    recovered bracket still cancels through the super-order endpoint."""
    from tradex_execution.recovery import recover_trading_cache
    from tradex_execution.sqlite_event_store import SQLiteEventStore

    db = tmp_path / "bracket-restart.db"
    broker1 = _SuperBroker()
    store1 = SQLiteEventStore(db)
    engine1 = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=BrokerFillSource(broker1),
        idempotency_guard=MemoryIdempotencyGuard(),
        event_store=store1,
        require_durable_events=True,
    )
    try:
        receipt = engine1.submit(_bracket_request("bracket-cid-restart"))
        assert isinstance(receipt, OrderReceipt)
    finally:
        engine1.shutdown()
        store1.close()

    broker2 = _SuperBroker()
    store2 = SQLiteEventStore(db)
    engine2 = ExecutionEngine(bus=ReactiveBus(), fill_source=BrokerFillSource(broker2))
    try:
        recover_trading_cache(store2, engine2.cache)
        recovered = engine2.cache.get_order("super-1")
        assert recovered is not None
        # Legs survived the round trip -> the order is still a bracket.
        assert recovered.stop_loss_price is not None
        assert recovered.target_price is not None

        engine2.cancel(OrderId(value="super-1"))
        assert broker2.cancelled == [OrderId(value="super-1")]
        cached = engine2.cache.get_order("super-1")
        assert cached is not None
        assert cached.status is OrderStatus.CANCELLED
    finally:
        engine2.shutdown()
        store2.close()


def test_bracket_modify_after_restart_reaches_super_endpoint(tmp_path) -> None:
    """A bracket recovered from the event log keeps its protective legs, so
    engine.modify still dispatches the composite to the venue."""
    from tradex_execution.recovery import recover_trading_cache
    from tradex_execution.sqlite_event_store import SQLiteEventStore

    db = tmp_path / "bracket-modify-restart.db"
    broker1 = _SuperBroker()
    store1 = SQLiteEventStore(db)
    engine1 = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=BrokerFillSource(broker1),
        idempotency_guard=MemoryIdempotencyGuard(),
        event_store=store1,
        require_durable_events=True,
    )
    try:
        receipt = engine1.submit(_bracket_request("bracket-cid-modify-restart"))
        assert isinstance(receipt, OrderReceipt)
    finally:
        engine1.shutdown()
        store1.close()

    broker2 = _SuperBroker()
    store2 = SQLiteEventStore(db)
    engine2 = ExecutionEngine(bus=ReactiveBus(), fill_source=BrokerFillSource(broker2))
    try:
        recover_trading_cache(store2, engine2.cache)
        recovered = engine2.cache.get_order("super-1")
        assert recovered is not None
        assert recovered.stop_loss_price == Price(value=Decimal("2450.00"))
        assert recovered.target_price == Price(value=Decimal("2600.00"))

        updated = BracketOrderRequest(
            instrument=Equity.of("NSE", "RELIANCE"),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("2510.00")),
            stop_loss_price=Price(value=Decimal("2460.00")),
            target_price=Price(value=Decimal("2650.00")),
            time_in_force=TimeInForce.DAY,
            trailing_jump=Price(value=Decimal("10")),
        )
        engine2.modify(OrderId(value="super-1"), updated)

        assert broker2.modified == [(OrderId(value="super-1"), updated)]
        cached = engine2.cache.get_order("super-1")
        assert cached is not None
        assert cached.price == Price(value=Decimal("2510.00"))
        assert cached.stop_loss_price == Price(value=Decimal("2460.00"))
        assert cached.target_price == Price(value=Decimal("2650.00"))
    finally:
        engine2.shutdown()
        store2.close()
```

- [ ] **Step 2: Run it — the new test passes before the deletion**

```bash
uv run --no-sync python -m pytest trading/tests/execution/test_bracket_engine_pipeline.py -q -p no:cacheprovider
```

Expected: PASS (this proves the log alone reconstructs the book; the order store is not needed).

- [ ] **Step 3: Unwire the order store from boot**

In `startup.py` step 4b (`:501-539`), keep the guard and the event store and delete the order store. Replace the whole block with:

```python
    # 4b. Durability (R1/R2) — the event log is the single persistence
    # authority. When a persistence path is configured, the idempotency guard
    # and the event log share that file; paper and live always get a guard so
    # duplicate submissions are refused across process restarts. The order
    # table and its bus mirror are gone: OMS recovery replays the log alone.
    guard: Any = MemoryIdempotencyGuard()
    event_store: Any = None
    if cfg.persistence.path:
        from tradex_execution.sqlite_event_store import SQLiteEventStore
        from tradex_execution.sqlite_store import SQLiteIdempotencyGuard

        guard = SQLiteIdempotencyGuard(cfg.persistence.path)
        event_store = SQLiteEventStore(cfg.persistence.path)
    elif cfg.mode in ("paper", "live"):
        # No explicit path: paper still gets a durable guard under the runtime
        # dir. Live boot has already refused to reach here without a path
        # (`live mode requires persistence path`).
        from tradex_execution.sqlite_store import SQLiteIdempotencyGuard

        guard = SQLiteIdempotencyGuard(
            Path(cfg.runtime_dir) / f"{cfg.broker_id.value.lower()}.idempotency.db"
        )
```

Delete step 6a (`:774-785`) entirely, including the `attach_order_persistence` import, `load_into`, and the subscription. That was the only use of `order_store` inside `_boot_tail`; drop the `order_store: Any` parameter (`:640`) and remove `order_store=order_store` from both `_boot_tail(...)` call sites (`:556`, `:577`). The engine construction already receives `event_store=` and nothing else from this block.

Make durable writes fail-closed in paper as well as live (D6). Replace `:717` and the two flags:

```python
    live_fail_closed = cfg.mode == "live"
    durable_mode = cfg.mode in ("paper", "live") and event_store is not None
```

and change the engine construction's last flag to `require_durable_events=durable_mode`.

Update two comments that name the deleted store: step 6b (`:788-792`) now says recovery replays the log before broker reconciliation, with no snapshot restore to follow; the baseline-store comment (`:662-664`) says `SQLiteRiskBaselineStore` shares the file with the event log (`persistence.path`), and in-memory without a path.

Also in `engine.py`, delete the re-export block at `:93-96` (the `from tradex_execution.sqlite_store import ...` line and its comment) so no module imports the deleted classes through the engine.

- [ ] **Step 4: Delete the order-store implementation**

In `execution/src/tradex_execution/sqlite_store.py` delete:
- `_ORDER_LEG_COLUMNS`, `_order_to_row` (`:91-111`), `_row_to_order` (`:114-165`)
- `SQLiteOrderStore` (`:168-394`)
- `OrderStore` protocol (`:565-571`) and `InMemoryOrderStore` (`:574-588`)
- `attach_order_persistence` (`:617-679`)
- the whole `__all__` block (`:683-689`), replacing it with:

```python
__all__ = ["SQLiteIdempotencyGuard"]
```

Remove the imports that only those classes used: `logging`, `Any`, `Protocol`, `runtime_checkable`, `Sequence` (if unused elsewhere), `OrderRequest`, `Instrument`, `create_order`, `OrderSide`, `OrderType`, `ProductType`, `TimeInForce`, `Price`, `Quantity`, the event classes (`BrokerOrderAcknowledged`, `BrokerOrderRequested`, `CancelRequested`, `OrderPlaced`, `RiskDecision`), and `_log`. **Keep** `Order` and `OrderReceipt` — modify and cancel still `record_result(cid, Order)` / receipt at `engine.py:1480`, `:1579`. Keep `Path`, `configure_sqlite`, `ALLOWED_PICKLE_CLASSES`, `_restricted_unpickle`, `OrderId`, `OrderStatus`, `CorrelationId`. Run `uv run --no-sync ruff check execution/src/tradex_execution/sqlite_store.py` and delete exactly the imports it reports as unused.

- [ ] **Step 5: Delete the tests that only exercise the second authority**

```bash
git rm execution/tests/test_order_store_thread_safety.py \
  trading/tests/execution/test_persistence_seam.py \
  trading/tests/execution/test_sqlite_order_store_live.py
```

In `trading/tests/execution/test_sqlite_store.py`, keep only the cross-engine idempotency test from Task 12 and delete every test that imports `SQLiteOrderStore` / `InMemoryOrderStore` / `attach_order_persistence`. In `execution/tests/test_engine_v3_port.py`, keep only `TestRiskCheckResult` and delete the order-store port tests. In `trading/tests/parity/test_live_tape_parity.py`, delete `test_sqlite_order_store_round_trip`. In `trading/tests/execution/test_public_module_surface.py`, remove `"OrderStore"` and `"InMemoryOrderStore"` from the expected surface. In `trading/tests/runtime/test_durable_provider_integration.py:6`, delete the docstring bullet that says v4 has no `SQLiteOrderStore` or `SQLiteIdempotencyGuard`; the remaining notes stay. In `execution/src/tradex_execution/risk_baseline_store.py:12,53`, change the comments that name the order store to say the baseline is appended to / replayed from the event log.

- [ ] **Step 6: Prove nothing still reaches for the deleted authority**

```bash
grep -rn "SQLiteOrderStore\|InMemoryOrderStore\|attach_order_persistence\|OrderStore\b" \
  execution runtime interfaces trading tests --include='*.py'
```

Expected: no output (excluding the plan/spec documents).

```bash
uv run --no-sync python -m pytest execution/tests trading/tests/execution trading/tests/parity \
  trading/tests/runtime/test_durable_provider_integration.py -q -p no:cacheprovider
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add execution/src/tradex_execution/sqlite_store.py \
  execution/src/tradex_execution/engine.py \
  execution/src/tradex_execution/risk_baseline_store.py \
  runtime/src/tradex_runtime/startup.py \
  trading/tests/execution/test_bracket_engine_pipeline.py \
  trading/tests/execution/test_sqlite_store.py \
  trading/tests/execution/test_public_module_surface.py \
  trading/tests/runtime/test_durable_provider_integration.py \
  execution/tests/test_engine_v3_port.py \
  trading/tests/parity/test_live_tape_parity.py
git commit -m "refactor(wp4): make the event log the only persistence authority"
```

### Task 15: Delete RuntimeContext, boot_context, ingest_fill, FillIngestPort, ShutdownCoordinator

**Files:**
- Modify: `runtime/src/tradex_runtime/startup.py` (delete `RuntimeContext` `:258-306`, `boot_context` `:1748-1775`, `__all__`, two imports), `runtime/src/tradex_runtime/__init__.py`
- Delete: `runtime/src/tradex_runtime/shutdown.py`, `trading/tests/runtime/test_shutdown_coordinator.py`
- Modify: `runtime/src/tradex_runtime/session_impl.py:152`, `runtime/src/tradex_runtime/market_feed.py:40` (comments)
- Modify: `domain/src/tradex_domain/protocols.py:370-392` + `__all__`; `execution/src/tradex_execution/engine.py:1075-1091` (delete `ingest_fill`)
- Modify: `trading/tests/runtime/test_writer_lock_lifecycle.py:556-693`, `tests/runtime_audit/test_phase1_bootstrap.py`, `tests/regression/test_sigterm_lifespan_teardown.py:52,258-279`, `trading/tests/runtime/test_v3_port_gaps.py:28,128-135`, `trading/tests/integration/test_extensions_boot_wiring.py:21,181-190`, `trading/tests/sdk/test_session_readiness.py:281-288`
- Modify: `execution/tests/test_e2e_live_order.py` (`_compose` and 8 `ingest_fill` calls), `trading/tests/contracts/test_execution_boundary_contracts.py` (`TestFillIngestPort` → `TestInboundFillBus`)

**Interfaces:**
- Consumes: `boot(cfg)` returns a ready `TradingSession`; the inbound fill door is `bus.publish(OrderFilled(...))`.
- Produces: `tradex_runtime` exports `boot` only. `FillIngestPort` and `ExecutionEngine.ingest_fill` no longer exist. Broker sockets are closed by `TradingSession.stop()`.

`RuntimeContext` is a second composition root that duplicates what `boot()` already returns (a `TradingSession`). `ShutdownCoordinator` exists only to tear one down, so it dies with it. `ingest_fill` was a named alias for publishing `OrderFilled` — after AC-22 the bus is the one inbound path, and a second door with no caller is exactly the kind of surface this cleanup removes.

- [ ] **Step 1: Rewrite the lifecycle test against `boot()`**

In `trading/tests/runtime/test_writer_lock_lifecycle.py`, replace the two `RuntimeContext` tests (`:556-693`) with one test that proves each session owns and releases its own lock. Keep the file's existing `named_tape` fixture and the `_CountingLock` monkey-patch; construct the config inline as the current test does and pass `wire_strategies=False` (live manual-API mode, so the WP2 no-strategy gate does not apply):

```python
def test_each_boot_owns_its_writer_lock_and_releases_it_on_stop(
    named_tape, monkeypatch, tmp_path,
) -> None:
    """Closing one session releases its own lock and no other."""
    locks: list[SingleWriterLock] = []

    class _CountingLock:
        def __init__(self, path):
            self._lock = SingleWriterLock(path)
            self.released = False
            locks.append(self)

        def acquire(self):
            return self._lock.acquire()

        def release(self):
            self.released = True
            return self._lock.release()

    monkeypatch.setattr(
        "tradex_runtime.writer_lock.SingleWriterLock", _CountingLock,
    )

    def _boot_with(broker_id: BrokerId):
        broker = MagicMock()
        broker.capabilities = MagicMock()
        broker.capabilities.max_stream_instruments = 1000
        broker.capabilities.depth_levels = 0
        backend = MagicMock()
        backend.subscribe_orders = MagicMock()
        broker.stream_backend.return_value = backend
        broker.master_loader = None
        broker.connect = MagicMock()
        broker.close = MagicMock()
        broker.disconnect = MagicMock()
        broker.get_orderbook = MagicMock(return_value=[])
        broker.get_positions = MagicMock(return_value=[])
        monkeypatch.setattr(
            "tradex_runtime.live.build_broker_from_env",
            lambda _pid, **_kw: broker,
        )
        cfg = AppConfig(
            risk=permissive_risk(),
            mode="live",
            broker_id=broker_id,
            live_enabled=True,
            persistence=PersistenceConfig(
                path=f"{tmp_path}/test-orders-{broker_id.value}.db"
            ),
        )
        return startup_mod.boot(cfg, wire_strategies=False)

    session1 = _boot_with(BrokerId.DHAN)
    session2 = _boot_with(BrokerId.UPSTOX)

    assert len(locks) == 2
    assert all(not lock.released for lock in locks)

    session2.stop()
    assert not locks[0].released, (
        "closing the second session must not release the first session's lock"
    )
    assert locks[1].released

    session1.stop()
    assert locks[0].released
```

Delete the second test (`:635-693`) whose only subject was `RuntimeContext.writer_lock`, and remove `from tradex_runtime.startup import RuntimeContext` (`:48`).

- [ ] **Step 2: Rewrite the broker-close test as a session test**

Replace `trading/tests/runtime/test_broker_close_once.py` with a test that counts `session.broker.close`:

```python
"""M4 — broker.close() is called exactly once, by TradingSession.stop()."""

from __future__ import annotations

from tradex_config.schema import AppConfig
from tradex_runtime import startup as startup_mod
from trading.tests.risk_support import permissive_risk


def test_session_stop_closes_the_broker_exactly_once() -> None:
    """``session.stop()`` owns the broker lifecycle; nothing closes it twice."""
    cfg = AppConfig(risk=permissive_risk(), mode="paper")
    session = startup_mod.boot(cfg, wire_strategies=False)
    broker = session.broker
    original_close = broker.close
    close_calls: list[None] = []

    def counted_close() -> None:
        close_calls.append(None)
        original_close()

    broker.close = counted_close  # type: ignore[method-assign]
    session.stop()
    assert len(close_calls) == 1, (
        f"broker.close() must be called exactly once per teardown; got {len(close_calls)}"
    )
```

- [ ] **Step 3: Delete the two runtime symbols**

Delete `RuntimeContext` (`startup.py:258-306`) and `boot_context` (`:1748-1775`). Set `__all__ = ["boot"]`. Remove the `SingleWriterLock` entry from the `TYPE_CHECKING` block (line 40) and the `BrokerAdapter` import (line 34) if ruff reports them unused. In `runtime/src/tradex_runtime/__init__.py`, keep the dirty `clocks`/`environments` imports untouched and change only the startup import to:

```python
from tradex_runtime.startup import boot
```

Delete `ShutdownCoordinator`'s module and test:

```bash
git rm runtime/src/tradex_runtime/shutdown.py \
  trading/tests/runtime/test_shutdown_coordinator.py
```

Update the two stale comments: `session_impl.py:152` → "(context manager, explicit stop())"; `market_feed.py:40` → "torn down by ``broker.close()`` at session teardown".

- [ ] **Step 4: Delete `ingest_fill` and `FillIngestPort`**

In `domain/src/tradex_domain/protocols.py`, delete `FillIngestPort` (`:370-392`) and its `__all__` entry. In `engine.py`, delete `ingest_fill` (`:1075-1091`). In `execution/tests/test_e2e_live_order.py`, change `_compose` to return `(engine, bus, cache)` and replace each of the eight `engine.ingest_fill(x)` calls with:

```python
    bus.publish(OrderFilled(fill=x))
```

Add `from tradex_domain.events import OrderFilled` to the imports. In `trading/tests/contracts/test_execution_boundary_contracts.py`, remove `FillIngestPort` from the `tradex_domain.protocols` import, change the module docstring's claim 2 to "``OrderFilled`` on the bus is the one inbound-fill door", and replace `TestFillIngestPort` (`:88-150`) with:

```python
class TestInboundFillBus:
    """Claim 2: the bus is the one inbound fill door; the engine subscribes."""

    def test_an_unknown_order_fill_is_booked_from_the_bus(self) -> None:
        """A live fill for an order this process never saw still moves the book."""
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource(), cash=Decimal("100000"))
        try:
            fill = Fill(
                order_id=OrderId(value="external-1"),
                instrument=_instrument(),
                side=OrderSide.BUY,
                quantity=Quantity(value=Decimal("10")),
                price=Price(value=Decimal("100")),
            )
            bus.publish(OrderFilled(fill=fill))
            position = engine.cache.get_position(_instrument())
            assert position is not None, "the bus fill did not book a position"
            assert position.quantity.value == Decimal("10")
        finally:
            engine.shutdown()
            bus.dispose()

    def test_the_same_fill_twice_is_applied_once(self) -> None:
        """A re-published fill across a reconnect must not double-apply."""
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource(), cash=Decimal("100000"))
        try:
            fill = Fill(
                order_id=OrderId(value="external-2"),
                instrument=_instrument(),
                side=OrderSide.BUY,
                quantity=Quantity(value=Decimal("10")),
                price=Price(value=Decimal("100")),
            )
            bus.publish(OrderFilled(fill=fill))
            bus.publish(OrderFilled(fill=fill))
            position = engine.cache.get_position(_instrument())
            assert position is not None
            assert position.quantity.value == Decimal("10"), (
                "a re-delivered fill was applied twice"
            )
        finally:
            engine.shutdown()
            bus.dispose()
```

`OrderId` is already imported by this file; `_instrument()` is the file's helper.

- [ ] **Step 5: Fix the remaining importers of the deleted symbols**

- `tests/runtime_audit/test_phase1_bootstrap.py`: the first test builds a `RuntimeContext`; change it to `session = boot(cfg)` and read the same attributes from the session.
- `tests/regression/test_sigterm_lifespan_teardown.py`: remove the `ShutdownCoordinator` import (line 52) and its test (`:258-279`); the SIGTERM-lifespan assertions remain.
- `trading/tests/runtime/test_v3_port_gaps.py`: remove `test_runtime_context_is_dataclass` (lines 128-135) and the import at line 28.
- `trading/tests/integration/test_extensions_boot_wiring.py`: remove `test_boot_context_carries_strategy_engine` (lines 181-190) and the import at line 21.
- `trading/tests/sdk/test_session_readiness.py`: delete `test_boot_context_returns_ready_session` (lines 281-288).

- [ ] **Step 6: Prove the symbols are gone and the suites pass**

```bash
grep -rn "RuntimeContext\|boot_context\|ingest_fill\|FillIngestPort\|ShutdownCoordinator" \
  runtime domain execution interfaces trading tests scripts --include='*.py'
```

Expected: no output.

```bash
uv run --no-sync python -m pytest trading/tests/execution trading/tests/runtime trading/tests/sdk \
  trading/tests/contracts execution/tests/test_e2e_live_order.py \
  tests/runtime_audit/test_phase1_bootstrap.py tests/test_v3_port_gaps.py \
  tests/test_extensions_boot_wiring.py tests/regression/test_sigterm_lifespan_teardown.py \
  -q -p no:cacheprovider
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add runtime/src/tradex_runtime/startup.py runtime/src/tradex_runtime/__init__.py \
  runtime/src/tradex_runtime/session_impl.py runtime/src/tradex_runtime/market_feed.py \
  domain/src/tradex_domain/protocols.py execution/src/tradex_execution/engine.py \
  execution/tests/test_e2e_live_order.py \
  trading/tests/contracts/test_execution_boundary_contracts.py \
  trading/tests/runtime/test_writer_lock_lifecycle.py \
  trading/tests/runtime/test_broker_close_once.py \
  tests/runtime_audit/test_phase1_bootstrap.py \
  trading/tests/runtime/test_v3_port_gaps.py \
  trading/tests/integration/test_extensions_boot_wiring.py \
  tests/regression/test_sigterm_lifespan_teardown.py \
  trading/tests/sdk/test_session_readiness.py
git commit -m "refactor(wp4): one composition root and one inbound fill door"
```

### Task 16: WP5 — Delete the replay source, the slippage wrapper, and the pricing helpers

**Files:**
- Modify: `execution/src/tradex_execution/fill_sources.py` (delete `ReplayFillSource`, `:412-489`)
- Modify: `execution/src/tradex_execution/fill_model.py` (delete `restamp_fill` `:118-136`, `reprice_fill` `:138-151`, the `order_id_override` parameter)
- Modify: `execution/src/tradex_execution/slippage.py` (delete `SlippageAwareFillSource`, `:68-96`)
- Modify: `execution/src/tradex_execution/fee_processor.py` (delete `fee_for_fill`, `:52-65`)
- Modify: `execution/src/tradex_execution/fees.py` (delete `PricingService`, `:426-475`)
- Modify: `execution/src/tradex_execution/engine.py` (`:804-811`)
- Modify: `execution/src/tradex_execution/__init__.py`
- Modify: `execution/tests/test_fill_source_contract.py`
- Modify: `trading/tests/execution/test_fill_sources_edges.py`
- Modify: `trading/tests/execution/test_fill_sources_gaps.py`
- Modify: `trading/tests/execution/test_fill_sources_gaps_2.py`
- Modify: `trading/tests/execution/test_slippage.py`
- Modify: `trading/tests/execution/test_fill_product_type_backfill.py`
- Modify: `trading/tests/execution/test_fees_pricing.py`
- Modify: `trading/tests/execution/test_fees_gaps.py`
- Modify: `trading/tests/execution/test_fees_validation.py`
- Modify: `trading/tests/execution/test_fees_v3_port.py`
- Modify: `trading/tests/execution/test_fee_rounding_mode.py`
- Modify: `trading/tests/execution/test_event_store_reconstruction.py`
- Modify: `trading/tests/parity/test_mode_parity_across_paths.py`
- Delete: `trading/tests/parity/test_replay_parity.py`
- Delete: `trading/tests/parity/test_execution_projection_parity.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: the engine charges fees through `FeeProcessor.compute_fee(fill)`; `FillModel.make_fill(order, price, timestamp)` has no override parameter; `tradex_execution` exports no `ReplayFillSource`, `SlippageAwareFillSource`, or `PricingService`.

**Why these are dead.** `ReplayFillSource`, `SlippageAwareFillSource` and
`PricingService` are referenced only by their own definitions, the package
`__init__`, and tests. No runtime, interface, CLI or replay module constructs
them. `restamp_fill` / `reprice_fill` exist only for those two sources. The
replay package reconstructs state by replaying the event log
(`recover_trading_cache`), not by replaying fills through an engine.

- [ ] **Step 1: Delete `ReplayFillSource`**

In `fill_sources.py`, delete everything from `class ReplayFillSource(FillModel):`
(`:412`) through end of file. Delete `from decimal import Decimal` (`:9`) — the
class was its only user. The file now ends with `BrokerFillSource.modify`.

- [ ] **Step 2: Remove the two re-stamp helpers and the override parameter**

In `fill_model.py`, replace the module docstring with:

```python
"""FillModel — the single fill-resolution path shared by every execution mode.

CROSS-MODE PARITY: backtest (SimulatedFillSource), paper (PaperFillSource) and
live (BrokerFillSource) all build their Fill from the same price-resolution +
slippage + timestamp rules, so the same input events produce identical fill
prices and quantities in every mode (HIGH-6b).
"""
```

Replace the class docstring with:

```python
class FillModel:
    """Encapsulates the COMMON fill logic shared by all fill sources.

    Each fill source keeps only its mode-specific concerns — Paper's LTP
    cache lookup, Broker's ACK-only submission — and delegates price
    resolution, slippage, the ValueError-on-zero-price guard, and
    deterministic Fill construction here.
    """
```

Change `make_fill` to:

```python
    def make_fill(
        self,
        order: Order,
        price: Price,
        timestamp: datetime,
    ) -> Fill:
        """Build a Fill from an order + resolved price."""
        return Fill(
            order_id=order.order_id,
            instrument=order.instrument,
            side=order.side,
            quantity=order.quantity,
            price=price,
            timestamp=timestamp,
            # A fill is priced from its own product's schedule, and a null is
            # resolved to INTRADAY and counted, so the label has to be carried
            # here — the one place every fill source builds its Fill.
            product_type=order.product_type,
            # Same reason: attribution is only knowable from the order, so it
            # is stamped here rather than inferred downstream.
            strategy_id=order.strategy_id,
        )
```

Delete `restamp_fill` (`:118-136`) and `reprice_fill` (`:138-151`) in full.
Remove `OrderId` from the `tradex_domain.value_objects` import (keep `Price`).

- [ ] **Step 3: Delete the slippage wrapper**

In `slippage.py`, delete `class SlippageAwareFillSource:` through end of file.
Replace the imports above the models with exactly:

```python
from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from tradex_domain.enums import OrderSide
from tradex_domain.value_objects import Price, Quantity
```

- [ ] **Step 4: Delete `fee_for_fill` and inline the charge call**

In `fee_processor.py`, delete `fee_for_fill` (`:52-65`) in full. Keep the
`Any` import; the constructor still uses it.

In `engine.py`, replace `:808-811` with:

```python
            charged_fee = Decimal(0)
            if not getattr(self._fill, "position_projection_owned", False):
                charged_fee = self._fee_processor.compute_fee(fill)
```

- [ ] **Step 5: Delete `PricingService`**

In `fees.py`, delete `class PricingService:` through end of file (`:426-475`),
including the `# -- v3-ported static helpers` banner directly above it. The file
now ends with `FeeCalculator.breakdown_for`.

- [ ] **Step 6: Rewrite the execution package surface**

Replace `execution/src/tradex_execution/__init__.py` in full:

```python
"""Execution engine and OMS for the TradeX v4 trading platform.

Public intake interface: the engine, fill sources, fee calculation,
order/position management, and the trading cache.  Internal types
(idempotency guards, the sqlite persistence seam, risk internals) live
in their respective submodules — import from there directly.
"""

from tradex_execution.engine import ExecutionEngine, RiskManager
from tradex_execution.fees import FeeCalculator
from tradex_execution.fill_sources import (
    BrokerFillSource,
    FillSource,
    PaperFillSource,
    SimulatedFillSource,
)
from tradex_execution.order_manager import OrderManager
from tradex_execution.position_accountant import PositionAccountant
from tradex_execution.projection import execution_projection
from tradex_execution.slippage import (
    FixedSlippageModel,
    NoSlippageModel,
    PercentageSlippageModel,
    SlippageModel,
)
from tradex_execution.trading_cache import TradingCache

__all__ = [
    "BrokerFillSource",
    "ExecutionEngine",
    "FeeCalculator",
    "FillSource",
    "FixedSlippageModel",
    "NoSlippageModel",
    "OrderManager",
    "PaperFillSource",
    "PercentageSlippageModel",
    "PositionAccountant",
    "RiskManager",
    "SimulatedFillSource",
    "SlippageModel",
    "TradingCache",
    "execution_projection",
]
```

- [ ] **Step 7: Trim the tests of the deleted symbols**

- `execution/tests/test_fill_source_contract.py`: remove `ReplayFillSource`
  from the import and change `ALL_SOURCES` (`:51`) to
  `(SimulatedFillSource, PaperFillSource)`.
- `trading/tests/execution/test_fill_sources_edges.py`: delete the
  `ReplayFillSource` section (`:119` to EOF), the `_make_fill` helper
  (`:48-56`), and the now-unused imports `Fill`, `OrderId`, `UTC`, `datetime`,
  `ReplayFillSource`.
- `trading/tests/execution/test_fill_sources_gaps.py`: delete
  `test_replay_cancel_is_noop` (`:88-90`) and `ReplayFillSource` from the
  import.
- `trading/tests/execution/test_fill_sources_gaps_2.py`: delete
  `test_replay_fill_source_overrides_order_id` (`:59` to EOF), change the
  docstring to `"""Gap tests for fill sources — PaperFillSource cache,
  BrokerFillSource."""`, and remove the imports `ReplayFillSource`, `Fill`,
  `OrderId`, `UTC`, `datetime`.
- `trading/tests/execution/test_slippage.py`: delete
  `TestSlippageAwareFillSource` (`:88` to EOF); change the docstring to
  `"""Tests for slippage models."""`; remove `SimulatedFillSource`,
  `SlippageAwareFillSource`, and `OrderStatus` from the imports.
- `trading/tests/execution/test_fill_product_type_backfill.py`: change
  "five sites" to "three sites" in the docstring; delete the two table rows
  for `FillModel.restamp_fill` and `SlippageAwareFillSource.submit`; delete
  `test_restamp_preserves_a_recorded_products_label` and
  `test_restamp_does_not_invent_a_label_it_was_not_given` (`:135-148`);
  delete section 3 in full (`:151-189`); remove `FixedSlippageModel` and
  `SlippageAwareFillSource` from the imports.
- `trading/tests/execution/test_fees_pricing.py`: delete
  `test_pricing_service_total_cost_buy` and `test_pricing_service_total_cost_sell`
  (`:78-89`); remove `PricingService` from the import.
- `trading/tests/execution/test_fees_gaps.py`: change the docstring to
  `"""Gap tests for fees — STT delivery rates and the per-order brokerage cap."""`;
  delete `test_pricing_service_total_cost_with_custom_fees` (`:32-41`), the
  `_make_fill` helper (`:20-29`), and `PricingService` from the import.
- `trading/tests/execution/test_fees_validation.py`: delete
  `test_vwap_with_all_zero_quantities_raises` (`:56-61`) and the `vwap`
  assertion block inside `test_valid_inputs_still_work` (`:86-90`); remove
  `PricingService` from the import.
- `trading/tests/execution/test_fees_v3_port.py`: change the docstring to
  `"""Tests for v3-ported fee symbols (FeeBreakdown, equity_delivery,
  equity_intraday)."""`; delete `TestPricingServiceStatic` (`:142` to EOF);
  remove `PricingService` from the import.
- `trading/tests/execution/test_fee_rounding_mode.py`: change the class
  docstring to `"""equity_intraday / calculate / calculate_capped all use
  q2."""`; delete `test_total_cost_buy_half_paisa_rounds_up`,
  `test_total_cost_sell_half_paisa_rounds_away_from_zero` and
  `test_total_cost_matches_half_up_reference` (`:164-181`); remove
  `PricingService` from the import.
- `trading/tests/execution/test_event_store_reconstruction.py`: delete
  `test_reconstruction_matches_the_live_engine_exactly` (`:170` to EOF).
- `trading/tests/parity/test_mode_parity_across_paths.py`: rewrite the module
  docstring's path list to the two remaining paths (live book, event-log
  rebuild); delete `_replay_into` (`:115-137`), `test_replay_of_recorded_fills_matches_the_live_projection`
  (`:177-210`) and `test_all_three_paths_agree` (`:212-247`); remove
  `ReplayFillSource` from the import.
- Delete `trading/tests/parity/test_replay_parity.py` and
  `trading/tests/parity/test_execution_projection_parity.py` in full.

- [ ] **Step 8: Prove the symbols are gone and the tests pass**

```bash
grep -rn "ReplayFillSource\|SlippageAwareFillSource\|PricingService\|fee_for_fill\|restamp_fill\|reprice_fill" \
  execution domain runtime interfaces trading tests --include='*.py'
```

Expected: no output.

```bash
uv run --no-sync python -m pytest execution/tests trading/tests/execution trading/tests/parity \
  -q -p no:cacheprovider --timeout=300
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add execution/src/tradex_execution/fill_sources.py \
  execution/src/tradex_execution/fill_model.py \
  execution/src/tradex_execution/slippage.py \
  execution/src/tradex_execution/fee_processor.py \
  execution/src/tradex_execution/fees.py \
  execution/src/tradex_execution/engine.py \
  execution/src/tradex_execution/__init__.py \
  execution/tests/test_fill_source_contract.py \
  trading/tests/execution/test_fill_sources_edges.py \
  trading/tests/execution/test_fill_sources_gaps.py \
  trading/tests/execution/test_fill_sources_gaps_2.py \
  trading/tests/execution/test_slippage.py \
  trading/tests/execution/test_fill_product_type_backfill.py \
  trading/tests/execution/test_fees_pricing.py \
  trading/tests/execution/test_fees_gaps.py \
  trading/tests/execution/test_fees_validation.py \
  trading/tests/execution/test_fees_v3_port.py \
  trading/tests/execution/test_fee_rounding_mode.py \
  trading/tests/execution/test_event_store_reconstruction.py \
  trading/tests/parity/test_mode_parity_across_paths.py \
  trading/tests/parity/test_replay_parity.py \
  trading/tests/parity/test_execution_projection_parity.py
git commit -m "refactor(wp5): delete replay source, slippage wrapper, and pricing helpers"
```

---

### Task 17: WP5 — Delete the remaining dead modules, helpers, and duplicate constants

**Files:**
- Delete: `interfaces/src/tradex_interfaces/replay_run.py`
- Delete: `brokers/src/tradex_brokers/common/http_response.py`
- Delete: `brokers/tests/common/test_http_response.py`
- Delete: `scripts/extract_package.py`
- Delete: `market_data/src/tradex_market_data/universe.py`
- Delete: `Dependencies/nse_delistings.csv`
- Modify: `strategy/src/tradex_strategy/artifacts.py` (`:56-58`, `:112-123`)
- Modify: `replay/src/tradex_replay/backtest.py` (delete `run_with_loader`, `:1214-1255`)
- Modify: `brokers/src/tradex_brokers/common/rate_table.py` (delete `get_rate_limit_config`, `:183-202`, and the `RateLimitConfig` import)
- Modify: `runtime/src/tradex_runtime/paper_book_fill_source.py` (delete `:53-74`)
- Modify: `runtime/src/tradex_runtime/replay_controller.py:7` (docstring cell list)
- Modify: `interfaces/src/tradex_interfaces/routes/__init__.py`
- Modify: `market_data/src/tradex_market_data/__init__.py`
- Modify: `interfaces/src/tradex_interfaces/cli.py:559`
- Modify: `interfaces/src/tradex_interfaces/routes/chart.py:330`
- Modify: `trading/scripts/backfill_parquet.py:55`
- Modify: `trading/tests/datalake/test_universe.py:6`
- Modify: `trading/tests/datalake/test_symbol_resolve.py:12`
- Modify: `trading/tests/strategy/test_artifacts.py`
- Modify: `domain/src/tradex_domain/universe.py` (docstring `:13-29`)
- Modify: `tests/test_import_boundaries.py:838-842`
- Modify: `observability/tests/test_alert_rules.py:52-53` (drop the `operations/src` and `persistence/src` scan roots; neither directory exists)
- Modify: `trading/pyproject.toml` (`:16`, `:59`)

**Interfaces:**
- Consumes: `tradex_domain.universe.load_universe` / `available_universes` from Task 5's domain extraction (already present).
- Produces: no `tradex_market_data.universe`; `tradex_strategy.artifacts` exports only `StrategyArtifact`, `ExperimentManifest`, `StrategySnapshot`, `StrategyRestoreResult`; `tradex_brokers.common.rate_table` exports only the tables; `paper_book_fill_source` exports `BookAudit` and `PaperBookFillSource` only.

- [ ] **Step 1: Delete the modules nothing imports**

```bash
git rm interfaces/src/tradex_interfaces/replay_run.py \
  brokers/src/tradex_brokers/common/http_response.py \
  brokers/tests/common/test_http_response.py \
  scripts/extract_package.py \
  market_data/src/tradex_market_data/universe.py \
  Dependencies/nse_delistings.csv
```

`replay_run.py` has no importer (the hit in `replay_controller.py` is a
docstring cell name). `http_response.py` has only its own test.
`extract_package.py` copies from `trading/src/tradex_trading`, which was
deleted, so it cannot run. `nse_delistings.csv` lost its last reader and only
test when Task 5 deleted `tradex_research.pit`; grep finds no consumer, so the
438 data rows are dead weight (Task 18 removes the remaining doc mentions).

In `observability/tests/test_alert_rules.py:52-53`, delete `"operations/src",`
and `"persistence/src",` from the scanned-root list; neither directory exists,
and `.pre-commit-config.yaml:21` already dropped the same dead roots in Task 5.

In `runtime/src/tradex_runtime/replay_controller.py:7`, reword the parenthetical
list of shared cells so it reads `(run flag, stop-ack flag, step latch, last
closed bar index)` instead of naming `replay_run`. The old cell name collides
with the deleted `replay_run` module, and the Step 7 grep must stay clean.

- [ ] **Step 2: Delete the unused artifact store**

In `strategy/src/tradex_strategy/artifacts.py`, delete the
`StrategyArtifactStore` Protocol (`:56-58`) and
`InMemoryStrategyArtifactStore` (`:112-123`) in full. Remove `Protocol` from
the `typing` import; keep `Any`.

In `trading/tests/strategy/test_artifacts.py`, delete
`test_artifact_store_round_trips_versioned_artifact` (`:12-18`) and remove
`InMemoryStrategyArtifactStore` from the import. The remaining tests cover
`StrategyArtifact` and `ExperimentManifest`, which stay.

- [ ] **Step 3: Delete the three unused helper functions**

- `replay/src/tradex_replay/backtest.py`: delete `run_with_loader` (`:1214-1255`)
  through end of file. Keep the `cast` and `Any` imports; both are still used
  at `:519` and throughout the module.
- `brokers/src/tradex_brokers/common/rate_table.py`: delete
  `get_rate_limit_config` (`:183-202`); delete
  `from tradex_shared import RateLimitConfig` (`:13`). After Task 1 that import
  reads `from tradex_domain.rate_limiting import RateLimitConfig`; delete
  whichever form is present. `resilience.py` consumes `RATE_TABLES` directly.
- `runtime/src/tradex_runtime/paper_book_fill_source.py`: delete
  `BOOK_MATCHING`, `SLIPPAGE_MODEL`, `FILL_PATHS`, `DEFAULT_FILL_PATH` and
  `resolve_fill_path` (`:53-74`). `PaperBookFillSource` and `BookAudit` remain;
  `startup.py` reads `BOOK_MATCHING` from `tradex_domain.constants`.

- [ ] **Step 4: Delete the unused route list**

Replace `interfaces/src/tradex_interfaces/routes/__init__.py` in full:

```python
"""HTTP route modules for the TradeX API.

Each module owns one slice of routes. The contract is uniform:

- The module exposes a module-level ``router`` (a FastAPI ``APIRouter``).
- Route functions read the active session via :func:`get_session`, a
  FastAPI dependency that returns ``request.app.state.session``.

``create_app`` (in :mod:`tradex_interfaces.fastapi_app`) imports each router
module directly.
"""

from tradex_interfaces.routes import (
    account,
    extensions,
    health,
    market_data,
    orders,
    portfolio,
)

__all__ = ["account", "extensions", "health", "market_data", "orders", "portfolio"]
```

- [ ] **Step 5: Point every universe importer at the domain module**

Change the import in each file below to
`from tradex_domain.universe import ...` with the same names:

- `market_data/src/tradex_market_data/__init__.py:20` — also delete
  `"load_universe"` and `"available_universes"` from `__all__`.
- `interfaces/src/tradex_interfaces/cli.py:559`
- `interfaces/src/tradex_interfaces/routes/chart.py:330`
- `trading/scripts/backfill_parquet.py:55` (keep the `noqa: E402` comment)
- `trading/tests/datalake/test_universe.py:6`
- `trading/tests/datalake/test_symbol_resolve.py:12`

In `domain/src/tradex_domain/universe.py`, replace the shim paragraph
(`:15-29`) with:

```python
which closed the ring ``market_data -> replay -> strategy -> market_data``.
Extracting the module to domain removes the edge entirely; the
``tradex_market_data.universe`` shim was deleted on 2026-10-22, so this is the
only import path. This is the same reason ``NSETradingCalendar`` moved.

Do not move it back: any package that reaches across into
``tradex_market_data`` for a pure function re-opens the cycle, and
``tests/test_import_boundaries.py::test_no_unknown_import_cycles_between_packages``
will fail. Import from ``tradex_domain.universe`` instead.
```

In `tests/test_import_boundaries.py`, replace the comment at `:838-842` with:

```python
#:   * ``market_data -> replay -> strategy -> market_data`` — ``load_universe``
#:     / ``available_universes`` are pure domain functions and live in
#:     ``tradex_domain.universe``; ``tradex_strategy`` no longer imports the
#:     datalake package for them, and the market_data shim was deleted.
```

- [ ] **Step 6: Remove the last build config that names the deleted package**

Task 5 already removed the `"tradex-research>=0.1.0,<0.2",` dependency and
`"../research/src"` from `trading/pyproject.toml`; verify both are absent and
make no further edit there beyond the comment reword below.

Reword the comments that still name deleted packages, because Task 20 greps
TOML files:

- Root `pyproject.toml:395`: replace `# "trading" was removed with the tradex_trading package: trading/ now holds` with `# "trading" is not a workspace member: trading/ holds`.
- `trading/pyproject.toml:1-5`: delete the sentence naming the deleted package; keep "not a workspace member and not buildable" and the dependency-bundle explanation.
- `interfaces/pyproject.toml:28-36`: replace the entry-point history with two lines: the `tradex` script is declared here and nowhere else, and the retired duplicate claim was removed because two wheels owning `bin/tradex` made installs order-dependent.

- [ ] **Step 7: Prove the symbols are gone and the affected suites pass**

```bash
grep -rn "replay_run\|http_response\|StrategyArtifactStore\|InMemoryStrategyArtifactStore\|run_with_loader\|get_rate_limit_config\|resolve_fill_path\|ALL_ROUTES\|tradex_market_data.universe" \
  domain execution runtime interfaces replay strategy brokers market_data trading scripts tests \
  --include='*.py'
```

Expected: no output.

```bash
uv run --no-sync python -m pytest execution/tests trading/tests/execution trading/tests/strategy \
  trading/tests/datalake trading/tests/parity trading/tests/interface interfaces/tests \
  brokers/tests/common tests/test_import_boundaries.py \
  -q -p no:cacheprovider --timeout=300
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add interfaces/src/tradex_interfaces/replay_run.py \
  brokers/src/tradex_brokers/common/http_response.py \
  brokers/tests/common/test_http_response.py \
  scripts/extract_package.py \
  market_data/src/tradex_market_data/universe.py \
  Dependencies/nse_delistings.csv \
  strategy/src/tradex_strategy/artifacts.py \
  replay/src/tradex_replay/backtest.py \
  brokers/src/tradex_brokers/common/rate_table.py \
  runtime/src/tradex_runtime/paper_book_fill_source.py \
  interfaces/src/tradex_interfaces/routes/__init__.py \
  market_data/src/tradex_market_data/__init__.py \
  interfaces/src/tradex_interfaces/cli.py \
  interfaces/src/tradex_interfaces/routes/chart.py \
  trading/scripts/backfill_parquet.py \
  trading/tests/datalake/test_universe.py \
  trading/tests/datalake/test_symbol_resolve.py \
  trading/tests/strategy/test_artifacts.py \
  domain/src/tradex_domain/universe.py \
  runtime/src/tradex_runtime/replay_controller.py \
  tests/test_import_boundaries.py \
  observability/tests/test_alert_rules.py \
  pyproject.toml \
  interfaces/pyproject.toml \
  trading/pyproject.toml
git commit -m "refactor(wp5): delete dead modules, helpers, and duplicate constants"
```

### Task 18: WP5 — delete the duplicate recovery path, dead helpers, constants, and stale docs

**Files:**
- Modify: `analytics/src/tradex_analytics/profiles.py:522-548`, `:607-662`
- Modify: `execution/src/tradex_execution/recovery.py:17`, `:63-115`, `:290-291`
- Modify: `domain/src/tradex_domain/protocols.py:395-407`, `:427`
- Delete: `trading/tests/execution/test_oms_crash_recovery.py`, `trading/tests/execution/test_recovery.py`
- Modify: `trading/tests/runtime/test_event_store_crash_restart.py:87`
- Modify: `domain/src/tradex_domain/constants.py:194`, `:197`, `:462`, `__all__` at `:807`, `:810`, `:1033`
- Modify: `tests/domain/test_constants.py:487-488`, `:496-497`
- Delete: `PREDEPLOY_SYSTEM_REVIEW.md`, `trading-predeploy-review.md`, `MULTI_AGENT_CLEANUP_PLAN.md`, `docs/ARCHITECTURAL-AUDIT-2026-09-25.md`, `docs/architecture-review.md`, `docs/proposed-target-architecture.md`, `docs/target-architecture-design.md`
- Modify: `docs/ARCHITECTURE.md:519-525`, `:1400-1403`, and every remaining `research/`/`tradex_research` mention in its §2 trees and §2.2 tables/totals (`:741`, `:1445`, `:2256-2259`, `:2296`, `:2352`, `:2833`, `:2837`, `:2931`, `:3005`, `:3406`); `docs/operations/metrics-catalog.md:14`; `.claude/agents/dead-code-hunter.md:8`
- Modify: `trading/tests/execution/test_cash_ledger_lock_negative_control.py:79`, `:84` (drop `research/src` from the PYTHONPATH example)
- Modify: `CLAUDE.md:447` (drop `nse_delistings.csv`; Task 17 deleted it)
- Modify: `docs/operations/runbooks/database-failure.md:5`, `:19`, `:41`, `:55`, `:59`; `docs/operations/runbooks/oms-recovery.md:9`, `:11`, `:21`, `:22`, `:41`, `:42`, `:62-68`

**Interfaces:**
- Consumes: Task 12 removed the last production use of `IDEMPOTENCY_REPLAY`/`SUBMITTED_STATUS` and the bare-`OrderId` replay branch; Task 14 deleted every `SQLiteOrderStore` and left `recover_trading_cache` as the live rebuild path.
- Produces: `tradex_execution.recovery` exposes `recover_trading_cache`/`fold_cash` and the `CashState`/`RecoveryResult`/`RecoveryOutcome` types only — there is one event-to-OMS projection. `tradex_domain.protocols` has no `OrderRepository`. `tradex_analytics.profiles` keeps only the functions dispatched by `PROFILE_SPECS` and the footprint builder. No deleted doc is referenced from a tracked index.

Three leftovers survived the earlier waves. `SessionRecovery` is a second event→order-repository projection: its only repository implementation was `SQLiteOrderStore` (deleted in Task 14), live boot uses `recover_trading_cache`, and its only callers are two test modules. The `OrderRepository` protocol exists solely to type that constructor. Five analytics helpers have no caller in any package, route, CLI, script, or test. Three domain constants lost their last callers in Task 12. Delete all of them rather than keeping a parallel path that can drift from the one boot actually uses.

- [ ] **Step 1: Prove the analytics helpers have no callers**

```bash
grep -rn "naked_levels\|row_of\|diagonal_imbalances\|cumulative_delta\|stacked_imbalances" \
  analytics execution interfaces runtime trading domain market_data brokers strategy config replay scripts tests \
  --include='*.py'
```

Expected: no output. The only other matches in the repo are generated graphs under `graphify-out/`, which are build artifacts, not callers.

- [ ] **Step 2: Delete the five helpers**

In `analytics/src/tradex_analytics/profiles.py`, delete `naked_levels` (`:522-538`), `row_of` (`:541-546`), and the two blank lines that follow, so `compute_volume_profile` is followed directly by the footprint banner:

```python
    return {"sessions": sessions, "options": options}


# --- footprint --------------------------------------------------------------
```

Then delete `diagonal_imbalances` (`:607-617`), `cumulative_delta` (`:620-626`), `stacked_imbalances` (`:629-660`), and the two blank lines that follow, so `compute_footprint` is followed directly by the dispatcher banner:

```python
    return out


# --- dispatcher -------------------------------------------------------------
```

Keep `compute_footprint`, `bucket_price`, `math`, and `PROFILE_SPECS` unchanged — `compute_footprint` uses both `bucket_price` and `math.floor`, so the imports stay.

- [ ] **Step 3: Run the analytics suite**

```bash
uv run --no-sync python -m pytest analytics/tests -q -p no:cacheprovider
```

Expected: PASS. Nothing imported the deleted helpers.

- [ ] **Step 4: Delete the second recovery projection and its protocol**

In `execution/src/tradex_execution/recovery.py`, delete the whole `SessionRecovery` class (`:63-115`), including its docstring, and change the import to:

```python
from tradex_domain.protocols import EventStore
```

In `recover_trading_cache`'s docstring, replace the first sentence of the body:

```python
    """Rebuild orders, positions, and cash by replaying *event_store*.

    Order carriers are projected in stream order. Each ``OrderFilled`` is then
    re-applied through the same OMS helpers used at runtime, so
    ``filled_quantity`` and open lots match a live engine after a crash. Cash
    folds through the shared ledger rather than a parallel model.
```

In `domain/src/tradex_domain/protocols.py`, delete the `@runtime_checkable` decorator, the `OrderRepository` class, and its two blank separator lines (`:395-407`), and remove `"OrderRepository",` from `__all__` (`:427`). `Sequence` stays — `BrokerAdapter` uses it.

```bash
git rm trading/tests/execution/test_oms_crash_recovery.py \
  trading/tests/execution/test_recovery.py
```

In `trading/tests/runtime/test_event_store_crash_restart.py:87`, change the docstring to:

```python
    """Event-log replay into a fresh engine cache (direct API)."""
```

- [ ] **Step 5: Prove one recovery path remains and run its tests**

```bash
grep -rn "SessionRecovery\|OrderRepository" \
  execution runtime domain interfaces trading tests scripts --include='*.py'
```

Expected: no output.

```bash
uv run --no-sync python -m pytest execution/tests trading/tests/execution \
  trading/tests/runtime/test_event_store_crash_restart.py -q -p no:cacheprovider
```

Expected: PASS. The deleted modules only exercised the removed class; the surviving reconstruction tests cover the same facts through `recover_trading_cache`.

- [ ] **Step 6: Delete the unreferenced constants**

In `domain/src/tradex_domain/constants.py`, delete these three definitions:

```python
IDEMPOTENCY_REPLAY = "idempotency_replay"
SUBMITTED_STATUS = "SUBMITTED"
IDEMPOTENCY_REPLAY_MESSAGE = "idempotency_replay"
```

and their three entries in `__all__`. Keep `IDEMPOTENCY_IN_FLIGHT`, `MODIFIED_STATUS`, and `CANCELLED_STATUS` — `routes/orders.py` still uses all three.

In `tests/domain/test_constants.py`, delete the two methods that assert the removed names:

```python
    def test_idempotency_replay(self):
        assert C.IDEMPOTENCY_REPLAY == "idempotency_replay"

    def test_submitted_status(self):
        assert C.SUBMITTED_STATUS == "SUBMITTED"
```

There is no test for `IDEMPOTENCY_REPLAY_MESSAGE`; it was never exercised.

- [ ] **Step 7: Prove the constants are gone**

```bash
grep -rn "IDEMPOTENCY_REPLAY\b\|SUBMITTED_STATUS\|IDEMPOTENCY_REPLAY_MESSAGE" \
  domain execution runtime interfaces trading tests scripts --include='*.py'
```

Expected: no output. `IDEMPOTENCY_IN_FLIGHT` remains and is a different symbol.

```bash
uv run --no-sync python -m pytest tests/domain/test_constants.py \
  trading/tests/contracts/test_execution_boundary_contracts.py -q -p no:cacheprovider
```

Expected: PASS.

- [ ] **Step 8: Delete the stale review and architecture documents**

```bash
git rm PREDEPLOY_SYSTEM_REVIEW.md trading-predeploy-review.md \
  MULTI_AGENT_CLEANUP_PLAN.md \
  docs/ARCHITECTURAL-AUDIT-2026-09-25.md docs/architecture-review.md \
  docs/proposed-target-architecture.md docs/target-architecture-design.md
```

Fix the tracked indexes that point at them. In `docs/ARCHITECTURE.md`, replace the docs tree block (`:519-525`):

```text
│   ├── ARCHITECTURAL-AUDIT-2026-09-25.md
│   ├── ARCHITECTURE.md
│   ├── README.md
│   ├── architecture-review.md
│   ├── deep-refactor-plan.md
│   ├── proposed-target-architecture.md
│   └── target-architecture-design.md
```

with:

```text
│   ├── ARCHITECTURE.md
│   └── README.md
```

`deep-refactor-plan.md` is already deleted in the pre-existing working tree (it is one of the checkpointed changes), so its tree entry goes too. Then replace the root tree tail (`:1400-1403`):

```text
├── skills-lock.json
├── trading-predeploy-plan.md
├── trading-predeploy-review.md
└── uv.lock
```

with:

```text
├── skills-lock.json
└── uv.lock
```

`trading-predeploy-plan.md` is likewise already deleted in the working tree.

Then sweep the deleted `research` package out of the same document. It is described in the §1 tree (`:741`, the whole `tradex_research/` subtree), the duplicate tree (`:1445`), the layer listing and duplicate-basename table (`:2256-2259`, `:2296`, `:2352`), the per-file test lists (`:2833`, `:2837`), the layer-count table (`:2931`), the package/line table (`:3005`) and the flow diagram (`:3406`). Delete those entries/rows and recompute every total the doc states (leaf counts, `src` leaf count, file/line totals) from the current tree with the same measurements the header describes (`git ls-files`, `wc -l`). Leave historical records under `docs/audits/`, `docs/specs/` and `docs/superpowers/` untouched. Prove the sweep with:

```bash
grep -n "research/\|tradex_research" docs/ARCHITECTURE.md
```

Expected: no output.

`nse_delistings.csv` was deleted in Task 17. Remove its tree entry
`│   └── nse_delistings.csv` from the `Dependencies/` block of
`docs/ARCHITECTURE.md` (`:103`) and drop it from the `Dependencies/` line in
`CLAUDE.md:447`.

In `trading/tests/execution/test_cash_ledger_lock_negative_control.py`, drop `research/src` from the reproduction example: line `:79` lists the package roots and line `:84` exports `$W/research/src` on `PYTHONPATH`.

In `docs/operations/metrics-catalog.md:14`, replace the citation:

```markdown
**Checklist source:** Operations requirements and live gates in [`ARCHITECTURE.md`](../ARCHITECTURE.md), plus the Phase 10 observability list (feed age, gaps, drops, resync duration, order latency, rejection reasons, unknown outcomes, slippage, exposure, drawdown, reconciliation drift).
```

In `.claude/agents/dead-code-hunter.md:8`, replace the deleted review reference:

```markdown
- **Plan:** `docs/superpowers/specs/2026-10-22-v4-predeploy-remediation-design.md`
```

Historical records under `docs/audits/` and `docs/specs/` keep their original citations; they describe what was true when they were written and are not indexes. Do not rewrite them.

- [ ] **Step 9: Correct the two runbooks that describe the deleted mirror**

In `docs/operations/runbooks/database-failure.md`:

- `:5` → `**Scope:** Persistence SQLite (event log + idempotency, `PersistenceConfig.path`), workspace blob DB (`TRADEX_WORKSPACE_DB`), and runtime directory (`TRADEX_RUNTIME_DIR`).`
- `:19` → `| Logs | Exceptions from `SQLiteEventStore`, `SQLiteIdempotencyGuard`, workspace routes. |`
- `:41` → `1. **Persistence DB vs workspace DB** — separate concerns; identify which file errors.`
- `:55` → `5. Restart with verified `PersistenceConfig.path`; confirm the event log opens and boot logs `OMS event-sourced recovery complete` ([oms-recovery.md](./oms-recovery.md)).`
- `:59` heading → `### Persistence / idempotency SQLite`

In `docs/operations/runbooks/oms-recovery.md`:

- `:9` → `- Process restart with `PersistenceConfig.path` pointing at the SQLite event log.`
- `:11` → `- Boot replay through `recover_trading_cache()` (event log → `TradingCache`).`
- `:21` → `| Events | The engine appends every order-lifecycle event to `SQLiteEventStore` at `PersistenceConfig.path`. |`
- `:22` → `| Recovery result | `recover_trading_cache` → `events_replayed`, `orders_recovered` (boot log line `OMS event-sourced recovery complete`). |`
- `:41` → `3. **Event replay then broker reconcile:** the event log is the sole persistence authority; boot replays it before reconciling with the broker orderbook.`
- `:42` → `4. **Bracket legs:** Verify protective fields (`stop_loss_price`, `target_price`) restored from the event log.`
- `:62-68` section C becomes:

```markdown
### C. Event-backed rebuild (`recover_trading_cache`)

1. `recover_trading_cache(event_store, cache)` — boot calls this before broker reconciliation.
2. Verify the boot log's `events_replayed` / `orders_recovered` against the expected event count.
3. Still run broker reconcile before live admission — projection alone is not sufficient for money.
```

- [ ] **Step 10: Run the affected suites and prove the docs are unreferenced**

```bash
grep -rn "PREDEPLOY_SYSTEM_REVIEW\|trading-predeploy-review\|MULTI_AGENT_CLEANUP_PLAN\|ARCHITECTURAL-AUDIT-2026-09-25\|architecture-review\.md\|proposed-target-architecture\|target-architecture-design" \
  --include='*.py' --include='*.md' --include='*.toml' --include='*.yml' --include='*.yaml' \
  . 2>/dev/null | grep -v '^./docs/audits/' | grep -v '^./docs/specs/' | grep -v '^./docs/superpowers/'
```

Expected: no output.

```bash
uv run --no-sync python -m pytest analytics/tests execution/tests trading/tests/execution \
  trading/tests/runtime/test_event_store_crash_restart.py tests/domain/test_constants.py \
  -q -p no:cacheprovider
```

Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add analytics/src/tradex_analytics/profiles.py \
  execution/src/tradex_execution/recovery.py \
  domain/src/tradex_domain/protocols.py \
  domain/src/tradex_domain/constants.py \
  tests/domain/test_constants.py \
  trading/tests/runtime/test_event_store_crash_restart.py \
  trading/tests/execution/test_cash_ledger_lock_negative_control.py \
  docs/ARCHITECTURE.md docs/operations/metrics-catalog.md \
  docs/operations/runbooks/database-failure.md \
  docs/operations/runbooks/oms-recovery.md \
  CLAUDE.md \
  .claude/agents/dead-code-hunter.md
git commit -m "refactor(wp5): delete duplicate recovery, dead helpers, constants, and stale docs"
```

---

### Task 19: WP6 — hermetic runtime, honest type gate, CI and docs

**Files:**
- Modify: `conftest.py`
- Create: `tests/test_hermetic_runtime.py`
- Modify: `brokers/tests/common/test_runtime_root.py:22-28`
- Delete: `trading/tests/deployment/test_env_persistence.py`
- Create: `scripts/mypy_ratchet.py`, `.mypy_baseline`
- Modify: `.ruff_baseline` (re-measured; the committed `0` was never met)
- Modify: `.github/workflows/quality-gate.yml`
- Create: `scripts/predeploy_gate.sh`
- Modify: `README.md`, `docs/ARCHITECTURE.md`

**Interfaces:**
- Consumes: every prior task's deletions and fixes; `uv`-installed editable packages.
- Produces: a session-scoped `tradex_runtime_dir` fixture pinning `TRADEX_RUNTIME_DIR` to a temp directory for the whole suite; `scripts/mypy_ratchet.py` (same ratchet shape as `scripts/ruff_ratchet.py`); `bash scripts/predeploy_gate.sh` as the one deploy-blocking command.

`tests/test_hermetic_runtime.py` is the guard that the suite never writes
`<repo>/runtime` again. The fixture is session-scoped and autouse because the
default runtime directory is resolved per call by `AppConfig` and
`tradex_domain.paths`; a per-test fixture would still let a module-scoped
session write into the repository before any test ran.

The type gate is a ratchet, not a hard wall, for the same reason the lint gate
is. The tree carries 52 pre-existing mypy errors across 22 files, one of them
in untracked in-progress work this plan must not edit. A hard-fail that can
never go green makes every step after it unreachable, which is how the current
CI reports on style and never on behaviour. The ratchet counts all 14 packages
on every run and fails only when the count grows.

- [ ] **Step 1: Write the failing hermetic-runtime test**

Create `tests/test_hermetic_runtime.py`:

```python
"""The suite must never write into the repository's ``runtime/`` directory.

``TRADEX_RUNTIME_DIR`` is read per call by both ``AppConfig`` and
``tradex_domain.paths``, so an unset variable means every session, guard and
lock in the suite resolves to ``<repo>/runtime`` and leaves databases there.
The root conftest pins it to a pytest temp directory for the whole session;
this file proves the pin is in force and that the config layer sees it.
"""

from __future__ import annotations

import os
from pathlib import Path

from tradex_config.schema import AppConfig

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_suite_runtime_dir_is_a_temp_directory() -> None:
    runtime_dir = Path(os.environ["TRADEX_RUNTIME_DIR"]).resolve()
    assert runtime_dir.is_dir(), "the session fixture must create the directory"
    assert runtime_dir.name.startswith("tradex-runtime"), runtime_dir
    assert runtime_dir != (_REPO_ROOT / "runtime").resolve()
    assert _REPO_ROOT not in runtime_dir.parents, (
        "a test durable path resolved inside the repository"
    )


def test_app_config_reads_the_pinned_runtime_dir() -> None:
    pinned = Path(os.environ["TRADEX_RUNTIME_DIR"]).resolve()
    assert Path(AppConfig().runtime_dir).resolve() == pinned
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run --no-sync python -m pytest tests/test_hermetic_runtime.py -q -p no:cacheprovider
```

Expected: FAIL with `KeyError: 'TRADEX_RUNTIME_DIR'` — nothing pins the variable.

- [ ] **Step 3: Pin the runtime directory in the root conftest**

Replace `conftest.py` in full with:

```python
"""Root test bootstrap: pin every durable path to a temporary directory.

All workspace packages are installed editable by ``uv sync``, so no
``sys.path`` surgery is needed. ``TRADEX_RUNTIME_DIR`` is pinned once per
session because ``AppConfig`` and ``tradex_domain.paths`` resolve it per call;
a test that wants the repository-anchored default deletes the variable with
``monkeypatch.delenv``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def tradex_runtime_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    runtime_dir = tmp_path_factory.mktemp("tradex-runtime")
    previous = os.environ.get("TRADEX_RUNTIME_DIR")
    os.environ["TRADEX_RUNTIME_DIR"] = str(runtime_dir)
    try:
        yield str(runtime_dir)
    finally:
        if previous is None:
            os.environ.pop("TRADEX_RUNTIME_DIR", None)
        else:
            os.environ["TRADEX_RUNTIME_DIR"] = previous
```

- [ ] **Step 4: Run the new test and the environment-sensitive suites**

```bash
uv run --no-sync python -m pytest tests/test_hermetic_runtime.py \
  config/tests/test_runtime_dir_resolution.py brokers/tests/common/test_runtime_root.py \
  trading/tests/runtime/test_writer_lock_path.py -q -p no:cacheprovider
```

Expected: PASS except `brokers/tests/common/test_runtime_root.py::test_runtime_dir_is_repo_anchored_not_cwd`. That test asserts the repo-anchored default but never deletes the variable, so the pin makes it see the temp directory. Its subject is cwd-independence, not the unset default; add the deletion the file's other tests already use.

- [ ] **Step 5: Make the cwd-independence test unset the variable**

In `brokers/tests/common/test_runtime_root.py`, change the first test to:

```python
def test_runtime_dir_is_repo_anchored_not_cwd(tmp_path, monkeypatch) -> None:
    """chdir must not move the state root — the fork regression test."""
    monkeypatch.delenv("TRADEX_RUNTIME_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    root = default_runtime_dir()
    assert root == _REPO_ROOT / "runtime"
    assert not (tmp_path / "runtime").exists(), "cwd fork reappeared"
```

Re-run the Step 4 command. Expected: all PASS.

- [ ] **Step 6: Delete the test that reads the operator's `.env.local`**

```bash
git rm trading/tests/deployment/test_env_persistence.py
```

The test asserts that the repo-root `.env.local` contains `TRADEX_RUNTIME_DIR`. It passes only on a host that has that untracked file and cannot pass on a clean CI checkout; the hermetic fixture makes it meaningless as well. Nothing imports the module.

Verify the deletion leaves no collection error:

```bash
test ! -e trading/tests/deployment/test_env_persistence.py
uv run --no-sync python -m pytest trading/tests/runtime tests/test_hermetic_runtime.py \
  -q -p no:cacheprovider
```

Expected: PASS. Do not point pytest at `trading/tests/deployment` itself: with its only test gone the directory collects nothing, and pytest exits 5 for an empty selection even though nothing is wrong.

- [ ] **Step 7: Add the mypy ratchet**

Create `scripts/mypy_ratchet.py`:

```python
"""Fail when the repo-wide mypy error count grows past ``.mypy_baseline``.

The tree carries known type debt, and some of it lives in untracked work in
progress that this branch must not edit. A hard wall that can never go green
makes every later CI step unreachable, so this is the same ratchet shape as
``scripts/ruff_ratchet.py``: the count may fall, it may not rise.

    uv run --no-sync python scripts/mypy_ratchet.py
    uv run --no-sync python scripts/mypy_ratchet.py --set-baseline
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE = _ROOT / ".mypy_baseline"

_TARGETS = (
    "domain/src/tradex_domain",
    "brokers/src/tradex_brokers",
    "config/src/tradex_config",
    "observability/src/tradex_observability",
    "analytics/src/tradex_analytics",
    "reactive/src/tradex_reactive",
    "execution/src/tradex_execution",
    "strategy/src/tradex_strategy",
    "replay/src/tradex_replay",
    "application/src/tradex_application",
    "market_data/src/tradex_market_data",
    "interfaces/src/tradex_interfaces",
    "runtime/src/tradex_runtime",
    "services/duckdb-analytics/src/duck_analytics",
)

_ERRORS = re.compile(r"^Found (\d+) errors? in ", re.MULTILINE)


def _run_mypy() -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, "-m", "mypy", *_TARGETS],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    if "Success: no issues found" in output:
        return 0, output
    match = _ERRORS.search(output)
    if match is None:
        raise SystemExit(f"mypy emitted no summary line:\n{output}")
    return int(match.group(1)), output


def main() -> int:
    count, output = _run_mypy()
    print(output)
    if "--set-baseline" in sys.argv[1:]:
        _BASELINE.write_text(f"{count}\n", encoding="utf-8")
        print(f"mypy baseline set to {count}")
        return 0

    baseline = int(_BASELINE.read_text(encoding="utf-8").strip())
    if count > baseline:
        print(
            f"mypy errors grew: {baseline} -> {count}. "
            "Fix the new errors; do not raise the baseline.",
            file=sys.stderr,
        )
        return 1
    if count < baseline:
        print(
            f"mypy errors fell: {baseline} -> {count}. "
            "Record the improvement with --set-baseline.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Create the baseline from the tree as it stands after Task 18 (deletions can only lower the count):

```bash
touch .mypy_baseline
uv run --no-sync python scripts/mypy_ratchet.py --set-baseline
cat .mypy_baseline
uv run --no-sync python scripts/mypy_ratchet.py
```

Expected: the first run prints `mypy baseline set to <N>` and the second exits 0. `<N>` is the measured count on this tree (52 before the deletions in this plan; lower is fine and is the point of the ratchet).

Re-measure the lint baseline too. `.ruff_baseline` is committed as `0`, a
count no tree has met since the ratchet was introduced (the tracked tree
measured 148 errors before this plan began), so the ratchet step in the gate
below is permanently red until it is re-measured:

```bash
uv run --no-sync python scripts/ruff_ratchet.py --set-baseline
cat .ruff_baseline
uv run --no-sync python scripts/ruff_ratchet.py
```

Expected: `ruff ratchet: baseline set to <M>`, then exit 0. `<M>` is the
measured count after this plan's deletions. The gate runs on the working
tree, so untracked in-progress files are counted; that is the same tree the
full suite runs against.

- [ ] **Step 8: Replace the CI hard-fail with the ratchet and add the two packaging jobs**

In `.github/workflows/quality-gate.yml`, delete every step from `- name: Mypy (domain)` through the `run:` line of `Mypy (duckdb-analytics)`, and insert in their place, directly after the Ruff ratchet step:

```yaml
      # Type debt is a ratchet for the same reason lint is: the tree carries
      # known errors, including one in untracked work in progress, so a hard
      # wall can never go green and protects nothing. The count may fall; it
      # may not grow. After clearing debt on purpose:
      #   uv run --no-sync python scripts/mypy_ratchet.py --set-baseline
      - name: Mypy ratchet (no new type errors)
        run: uv run --no-sync python scripts/mypy_ratchet.py
```

In the coverage-step comment, replace the sentence naming the deleted package:

```yaml
      # per-package range 79.7% in the thinnest package to 100% in five). 85
```

Append these two jobs at the end of the file:

```yaml

  clean-install:
    # Proves the packaging contract on its own: a fresh `uv sync --frozen`
    # installs every workspace member with no PYTHONPATH and no in-place
    # conftest, and none of the deleted packages is resolvable.
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install uv
        run: pip install uv

      - name: Sync from the lock
        run: uv sync --frozen

      - name: Every workspace package imports
        run: |
          uv run --no-sync python - <<'PY'
          import importlib

          packages = [
              "tradex_domain",
              "tradex_brokers",
              "tradex_config",
              "tradex_observability",
              "tradex_analytics",
              "tradex_reactive",
              "tradex_execution",
              "tradex_strategy",
              "tradex_replay",
              "tradex_application",
              "tradex_market_data",
              "tradex_interfaces",
              "tradex_runtime",
              "duck_analytics",
          ]
          for name in packages:
              importlib.import_module(name)
          print(f"{len(packages)} packages import")
          PY

      - name: Deleted packages are absent
        run: |
          uv run --no-sync python - <<'PY'
          import importlib.util

          for name in ("tradex_shared", "tradex_research", "tradex_trading"):
              assert importlib.util.find_spec(name) is None, f"{name} is importable"
          print("deleted packages absent")
          PY

      - name: Packaging guards
        run: >-
          uv run --no-sync python -m pytest
          tests/test_packaging_authority.py tests/test_removed_surfaces.py -q

  docker:
    # The image is a deploy artifact. Build and smoke it on every PR so a
    # missing COPY target, a PATH without .venv/bin, or a lost console
    # script is caught before release.
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4

      - name: Build the image
        run: docker build -f deploy/Dockerfile -t tradex-v4:ci .

      - name: The container runs as the declared non-root uid
        run: test "$(docker run --rm --entrypoint id tradex-v4:ci -u)" = "10001"

      - name: Workspace packages import inside the image
        run: >-
          docker run --rm --entrypoint python tradex-v4:ci -c
          "import tradex_domain, tradex_runtime, tradex_interfaces; print('imports ok')"

      - name: The console script starts
        run: docker run --rm --entrypoint tradex tradex-v4:ci --help
```

- [ ] **Step 9: Create the one-command predeploy gate**

Create `scripts/predeploy_gate.sh`:

```bash
#!/usr/bin/env bash
# Every deploy-blocking check in one command. Safe to run from any directory.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v uv >/dev/null 2>&1; then
  echo "predeploy gate: uv is not on PATH" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "predeploy gate: docker is required to prove the deploy artifact" >&2
  exit 1
fi

echo "==> uv sync --frozen"
uv sync --frozen

echo "==> import every workspace package"
uv run --no-sync python - <<'PY'
import importlib

packages = [
    "tradex_domain",
    "tradex_brokers",
    "tradex_config",
    "tradex_observability",
    "tradex_analytics",
    "tradex_reactive",
    "tradex_execution",
    "tradex_strategy",
    "tradex_replay",
    "tradex_application",
    "tradex_market_data",
    "tradex_interfaces",
    "tradex_runtime",
    "duck_analytics",
]
for name in packages:
    importlib.import_module(name)
print(f"{len(packages)} packages import")
PY

echo "==> ruff ratchet"
uv run --no-sync python scripts/ruff_ratchet.py

echo "==> mypy ratchet"
uv run --no-sync python scripts/mypy_ratchet.py

echo "==> import-linter"
uv run --no-sync --with import-linter lint-imports

echo "==> full test suite"
uv run --no-sync python -m pytest -c pyproject.toml -q -p no:cacheprovider --timeout=300

echo "==> docker build"
docker build -f deploy/Dockerfile -t tradex-v4:predeploy .

echo "==> container smoke"
test "$(docker run --rm --entrypoint id tradex-v4:predeploy -u)" = "10001"
docker run --rm --entrypoint python tradex-v4:predeploy -c \
  "import tradex_domain, tradex_runtime, tradex_interfaces; print('imports ok')"
docker run --rm --entrypoint tradex tradex-v4:predeploy --help >/dev/null

echo "==> container paper boot"
docker run --rm -i -e TRADEX_RUNTIME_DIR=/tmp/tradex-runtime \
  --entrypoint python tradex-v4:predeploy - <<'PY'
from decimal import Decimal

from tradex_config import AppConfig, RiskConfig
from tradex_runtime import TradingSession

config = AppConfig(
    risk=RiskConfig(
        max_order_value=Decimal("100000"),
        max_position_value=Decimal("500000"),
        max_orders_per_minute=10,
        max_daily_loss_amt=Decimal("5000"),
        max_drawdown_pct=Decimal("5"),
    )
)
session = TradingSession.paper(config=config)
try:
    assert str(session.state) == "READY", session.state
    print("paper boot: READY")
finally:
    session.stop()
PY

echo "predeploy gate: PASS"
```

```bash
chmod +x scripts/predeploy_gate.sh
bash -n scripts/predeploy_gate.sh
```

Expected: `bash -n` prints nothing (syntax OK). The full gate is executed in Task 20.

- [ ] **Step 10: Rewrite the stale README sections**

Replace the block from `## Architecture` through the line before `### Key design decisions` with:

```markdown
## Architecture

A `uv` workspace of 14 installable packages. `tradex_domain` is the shared
kernel; no package may import upward past its declared boundary, enforced by
`tests/test_import_boundaries.py` and the import-linter contracts in the root
`pyproject.toml`.

| Package | Import path | Responsibility |
|---|---|---|
| `domain/` | `tradex_domain` | Enums, value objects, instruments, protocols, capabilities, events, paths |
| `brokers/` | `tradex_brokers` | Dhan, Upstox and Paper adapters: auth, tokens, rate limits, streams |
| `config/` | `tradex_config` | `AppConfig`, environment loading, secret-file checks |
| `observability/` | `tradex_observability` | Metrics registry |
| `analytics/` | `tradex_analytics` | Indicators, reports, order-flow profiles |
| `reactive/` | `tradex_reactive` | RxPY bus |
| `execution/` | `tradex_execution` | Execution engine, OMS, idempotency, event store |
| `strategy/` | `tradex_strategy` | Strategy runtime, artifacts, extensions |
| `replay/` | `tradex_replay` | Backtest engine, walk-forward, Monte Carlo |
| `application/` | `tradex_application` | Application command handlers |
| `market_data/` | `tradex_market_data` | Datalake, parquet storage, history fetch, window loading |
| `interfaces/` | `tradex_interfaces` | FastAPI/WS routes and the `tradex` CLI |
| `runtime/` | `tradex_runtime` | Composition root (`boot`), `TradingSession`, feed supervision |
| `services/duckdb-analytics/` | `duck_analytics` | Read-only DuckDB service over the parquet datalake |
```

Replace the block from `## Setup` through the line before `## Quick start — paper trading` with:

```markdown
## Setup

Python **3.12+** and [`uv`](https://docs.astral.sh/uv/) are required.

```bash
uv sync --frozen
```

`uv sync` installs every workspace member editable from `uv.lock`; no
`PYTHONPATH` setup is needed.

Live credentials are read from the environment (e.g. `DHAN_CLIENT_ID`,
`DHAN_ACCESS_TOKEN`, `UPSTOX_API_KEY`, ...). Keep real tokens in a local,
git-ignored `.env.local` and load them explicitly — they are never loaded
implicitly.
```

Replace the quick-start Python block (the fence that begins
`from tradex_trading.sdk.session import TradingSession`) with:

```python
from decimal import Decimal

from tradex_config import AppConfig, RiskConfig
from tradex_domain.enums import OrderSide, OrderType
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_runtime import TradingSession

config = AppConfig(
    risk=RiskConfig(
        max_order_value=Decimal("100000"),
        max_position_value=Decimal("500000"),
        max_orders_per_minute=10,
        max_daily_loss_amt=Decimal("5000"),
        max_drawdown_pct=Decimal("5"),
    )
)
session = TradingSession.paper(config=config)
try:
    request = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("2500.00")),
    )
    receipt = session.engine.submit(request)
    print(f"Order: {receipt.order_id}, Status: {receipt.status}")
finally:
    session.stop()
```

Add this paragraph directly below that block:

```markdown
`TradingSession.paper()` returns a READY session backed by `PaperBroker`.
`session.engine` is the single order door. Boot refuses to start until every
required risk limit is named or explicitly set to `UNBOUNDED`; the snippet
names all five. A marketable limit order fills immediately; outside the
simulated session it stays `ACK` until filled.
```

Replace the block from `### Live trading` through the line before `### HTTP API — tradex serve` with:

```markdown
### Live trading

Live mode is entered through `tradex serve`; the explicit CLI invocation is
the confirmation gate. It requires credentials, an API key and a complete risk
policy in the environment, plus the tape the session must stream:

```bash
export DHAN_CLIENT_ID=...
export DHAN_ACCESS_TOKEN=...
export TRADEX_SERVE_API_KEY=...
export TRADEX_FEED_INSTRUMENTS=NSE:RELIANCE,NSE:INFY
export TRADEX_RISK_MAX_ORDER_VALUE=100000
export TRADEX_RISK_MAX_POSITION_VALUE=500000
export TRADEX_RISK_MAX_ORDERS_PER_MINUTE=10
export TRADEX_RISK_MAX_DAILY_LOSS=5000
export TRADEX_RISK_MAX_DRAWDOWN_PCT=5

tradex serve --broker DHAN --api-key "$TRADEX_SERVE_API_KEY"
```

A limit left unset refuses the boot with `MissingRiskLimitError`; a live
broker on a non-loopback host is refused as well.

### Session surface

`TradingSession` exposes the composed components directly.

| Member | What it is |
|---|---|
| `session.engine` | The single `ExecutionEngine`; `submit(OrderRequest)` is the only order door |
| `session.cache` | The OMS projection (orders, positions, cash) |
| `session.broker` | The active `BrokerAdapter` |
| `session.bus` | The reactive bus |
| `session.strategy_engine` | The strategy runtime, when one was wired |
| `session.market_feed` | The feed handle, when one was cold-started |
| `session.feed_supervisor` | Feed liveness supervisor |
| `session.state` | `NEW` → `READY` → `STOPPED` |
| `session.capabilities` | Broker capability declaration |
```

Replace the block from `## Development` through the line before `### Benchmarks` with:

```markdown
## Development

```bash
uv sync --frozen

# Full suite (also what CI runs)
uv run --no-sync python -m pytest -c pyproject.toml -q -p no:cacheprovider

# Every deploy-blocking check: install, lint, types, imports, tests, image
bash scripts/predeploy_gate.sh
```
```

In `### Benchmarks`, change the command to:

```bash
uv run --no-sync python benchmarks/bench_order_path.py
```

- [ ] **Step 11: Replace the stale ARCHITECTURE gate and drift sections**

In `docs/ARCHITECTURE.md`, replace the whole of `## 9. Gates` (through its closing `---`) with:

```markdown
## 9. Gates

Every gate is one command: `bash scripts/predeploy_gate.sh`. It runs, in order:

| Gate | Mechanism |
|---|---|
| Packaging | `uv sync --frozen`; all 14 members import; deleted packages are absent |
| Lint | `scripts/ruff_ratchet.py` against `.ruff_baseline` |
| Types | `scripts/mypy_ratchet.py` against `.mypy_baseline` |
| Import boundaries | `lint-imports` plus `tests/test_import_boundaries.py` |
| Complexity | `tests/test_complexity_gate.py` |
| Behaviour | the full `pytest` suite |
| Deploy artifact | `docker build`, non-root import and `tradex --help` smoke |

Pass/fail counts live in CI output, not in this document; the table records
the mechanism, so it cannot go stale.

---
```

Rename `## 10. Measured state of the working tree` to
`## 10. Measured state at the 2026-09-29 audit` and insert directly below the
heading:

```markdown
The counts below are a historical measurement, not the current tree. For the
current state run `bash scripts/predeploy_gate.sh`.
```

Replace items 1–3 of `## 11. Known drift and dead weight` (through the end of
item 3) with:

```markdown
1. **`README.md` now describes the real tree.** The earlier version documented
   a removed `tradex_trading` package, `pip install -e` instructions and a
   `ParquetBacktestLoader` that never existed. It now names the 14 workspace
   members, installs with `uv sync --frozen`, and its quick start is executed
   before commit.

2. **`trading/pyproject.toml` is intentionally a non-member.** `trading/` holds
   tests and scripts only; the manifest is a dependency bundle plus local
   pytest/ruff/mypy config, with no `[build-system]` and no `src/` package. It
   is not installed, and that is deliberate rather than an orphan.

3. **The CI comments now match the gates.** The dead `tradex_research` mypy
   and coverage entries are gone, the duckdb path names `duck_analytics`,
   coverage no longer cites the deleted `tradex_trading`, and the per-package
   mypy hard-fail is a ratchet because the tree carries known type debt.
```

Leave items 4 and 5 unchanged.

- [ ] **Step 12: Run the guards and the affected suites**

```bash
uv run --no-sync python -m pytest tests/test_hermetic_runtime.py \
  tests/test_packaging_authority.py tests/test_removed_surfaces.py \
  config/tests/test_runtime_dir_resolution.py brokers/tests/common/test_runtime_root.py \
  trading/tests/runtime/test_writer_lock_path.py -q -p no:cacheprovider
```

Expected: PASS.

```bash
uv run --no-sync python scripts/mypy_ratchet.py
```

Expected: exit 0 with the count at or below the recorded baseline.

- [ ] **Step 13: Commit**

```bash
git add conftest.py tests/test_hermetic_runtime.py \
  brokers/tests/common/test_runtime_root.py \
  scripts/mypy_ratchet.py .mypy_baseline .ruff_baseline scripts/predeploy_gate.sh \
  .github/workflows/quality-gate.yml README.md docs/ARCHITECTURE.md
git commit -m "test(wp6): hermetic runtime dir, type ratchet, CI packaging jobs, honest docs"
```

---

### Task 20: Final verification — the tree is actually deployable

**Files:**
- No production edits. If a check fails, fix the owning task's files and re-run the whole gate; do not add a new task.

**Interfaces:**
- Consumes: Tasks 1–19.
- Produces: evidence that the full suite is green, every deleted symbol is absent, and the predeploy gate passes.

- [ ] **Step 1: Confirm the worktree contains only planned changes**

```bash
git status --porcelain
git diff --stat HEAD
```

Expected: only files named in this plan, plus the pre-existing untracked work listed in *Left untouched* (still untracked). No `.env.local`, no `runtime/*.db`, no `trading/.venv` staged. If a pre-existing tracked hunk is missing, stop: the executor touched operator work.

- [ ] **Step 2: Prove every deleted symbol is gone**

```bash
grep -rn "tradex_shared\|tradex_research\|tradex_trading" \
  --include='*.toml' --include='*.yml' --include='*.yaml' \
  pyproject.toml requirements*.txt .github \
  domain brokers config observability analytics reactive execution strategy \
  replay application market_data interfaces runtime services trading tests scripts \
  2>/dev/null | grep -v '__pycache__'
```

Expected: no output. Build config may not name a deleted package.

```bash
grep -rnE "^[[:space:]]*(from|import) (tradex_shared|tradex_research|tradex_trading)(\.[[:space:]]|[[:space:]]|$)" \
  --include='*.py' \
  domain brokers config observability analytics reactive execution strategy \
  replay application market_data interfaces runtime services trading tests scripts \
  2>/dev/null | grep -v '__pycache__'
```

Expected: no output. No module imports a deleted package. Historical prose in
docstrings is not checked here; the structural guards
(`tests/test_packaging_authority.py`, `tests/test_removed_surfaces.py`,
`tests/test_import_boundaries.py`) prove the trees and packages stay deleted.

```bash
grep -rn "SessionRecovery\|OrderRepository\|RuntimeContext\|boot_context\|ingest_fill\|FillIngestPort\|ShutdownCoordinator\|SQLiteOrderStore\|StrategyArtifactStore\|InMemoryStrategyArtifactStore\|run_with_loader\|get_rate_limit_config\|resolve_fill_path\|ALL_ROUTES\|PricingService\|ReplayFillSource\|SlippageAwareFillSource\|fee_for_fill\|IDEMPOTENCY_REPLAY\|SUBMITTED_STATUS\|replay_run\|http_response" \
  --include='*.py' domain brokers config observability analytics reactive execution \
  strategy replay application market_data interfaces runtime services trading tests scripts 2>/dev/null \
  | grep -v '__pycache__'
```

Expected: no output.

- [ ] **Step 3: Run the full suite**

```bash
uv run --no-sync python -m pytest -c pyproject.toml -q -p no:cacheprovider \
  --timeout=300 > /tmp/v4-predeploy/final.txt 2>&1 || true
tail -n 1 /tmp/v4-predeploy/final.txt
grep -cE "^(FAILED|ERROR) [^ ]" /tmp/v4-predeploy/final.txt
```

Expected: `grep -c` prints `0`; the summary ends `0 failed` (skipped/xfailed are acceptable). The baseline's 78 failures are gone.

- [ ] **Step 4: Run the predeploy gate end to end**

```bash
bash scripts/predeploy_gate.sh
```

Expected: final line `predeploy gate: PASS`. The gate runs `uv sync --frozen`, imports all 14 packages, the ruff and mypy ratchets, import-linter, the full suite, `docker build`, the non-root/import/CLI smoke, and a READY paper boot inside the container. This is the deploy decision: no PASS, no deploy.

- [ ] **Step 5: Record the result**

No commit. Report the final summary line, the gate result, the current `.mypy_baseline` count, and any check that could not run on the executor host (for example Docker absent). A skipped check is reported as skipped, never as passed.

