# TradeX v4 — Code Review

**Date:** 2026-08-29
**Branch:** `main` (working dir `v4`, on the `v2-cleanup-remove-dead-temp-legacy` tree)
**Head:** `0e976e8` — 18 files modified, uncommitted
**Scope:** Full-repo review — architecture, tests, security, CI, hygiene

---

## Verdict

A genuinely well-built system. The three-package dependency boundary is
*actually* enforced (verified, not assumed), the test suite is large and green,
duplication is low, and there are no committed secrets. Several design decisions
— capability-loud adapters, `Decimal` money, fail-closed live-broker gating,
constant-time API key comparison — reflect real engineering care.

The problems are concentrated in **process and tooling, not in the core design**.
Most importantly: **the quality-gate CI job is broken and has been silently
passing nothing**, which means the lint and type gates you believe are running
have not produced a result in some time. Everything else is secondary to that.

| Area | Rating |
|------|--------|
| Architecture & boundaries | Strong |
| Test suite | Strong |
| Security posture | Adequate, with two real gaps |
| CI / quality gates | **Broken** |
| Tooling reproducibility | Weak |
| Repo hygiene | Weak |

---

## Measured baseline

| Metric | Value |
|--------|-------|
| Python source | 82,199 LOC across 187 files |
| `domain` / `brokers` / `trading` | 4,852 / 22,858 / 54,489 LOC |
| Test files | 181 |
| Test result | **2,740 passed, 2 skipped, 0 failed** (39s) |
| TypeScript frontend | 4,154 LOC across 19 files |
| Duplication (jscpd) | 2.65% — 854 duplicated lines, 67 clones |
| `TODO` / `FIXME` markers | 0 |
| Committed secrets | 0 |
| Git objects | 8,769 loose, 216 MiB, **0 packfiles** |

---

## Critical

### C1. `quality-gate` CI is broken — it uninstalls the environment and installs nothing

`.github/workflows/quality-gate.yml` runs:

```yaml
- name: Sync dependencies
  run: uv sync --frozen
- name: Ruff lint
  run: ruff check
- name: Mypy (domain only)
  run: mypy domain/src/tradex_domain
- name: Pytest with coverage
  run: python -m pytest --cov=domain --cov=brokers --cov=trading ...
```

Two independent defects make this a guaranteed failure:

1. **`uv.lock` is a 4-line stub with zero package entries:**

   ```toml
   version = 1
   revision = 3
   requires-python = ">=3.13"
   ```

2. **Root `pyproject.toml` has no `[project]` table** — it contains only
   `[tool.pytest.ini_options]`. There is nothing to install.

Verified locally:

```
$ uv sync --frozen --dry-run
Would use project environment at: .venv
Would uninstall 76 packages
 - altair==6.2.2
 - annotated-doc==0.0.5
 ... (76 total)
# installs: 0
```

**76 packages uninstalled, 0 installed.** Every subsequent step fails — `ruff`,
`mypy`, and `pytest` will not exist. Compounding this, `uv.lock` declares
`requires-python = ">=3.13"` while all three packages declare `>=3.12` and CI
pins Python 3.12.

No package declares dev tooling anywhere — there is no dependency group
containing `pytest`, `ruff`, or `mypy` in any of the four `pyproject.toml`
files. `parity.yml` has the same class of problem: it `pip install`s the three
packages (which declare only `rx` and `protobuf`) and then calls
`python -m pytest`, which it never installs.

**Fix:** add a `[project]` table with a `[dependency-groups] dev = [...]`
(pytest, pytest-cov, pytest-timeout, ruff, mypy) at the repo root, regenerate a
real `uv.lock`, and align `requires-python`. Until then, treat every green CI
run on this workflow as meaningless.

### C2. Five POST endpoints are unauthenticated, two of them unbounded

`X-API-Key` protection is applied only to the four write routes in
`routes/orders.py` (`POST /orders`, `POST /orders/bracket`, `PUT`, `DELETE`).
`routes/chart.py` registers five POST endpoints with no auth dependency:

| Endpoint | Risk |
|----------|------|
| `POST /api/charts/indicators/compute` | bounded compute |
| `POST /api/charts/transforms/{id}` | bounded compute |
| `POST /api/charts/profiles/{id}` | bounded compute |
| `POST /api/charts/backtest` | **unbounded** — spins `BacktestEngine` |
| `POST /api/charts/scanner/run` | **unbounded** — scans every universe partition |

This matches the documented contract (README: *"X-API-Key is required on write
routes only (`POST/PUT/DELETE /orders/*`)"*), so it is intentional rather than
an oversight. But the contract itself is the problem: `POST /backtest` and
`POST /scanner/run` accept a caller-supplied window and universe and run
CPU/IO-bound work — the scanner's own docstring says it "grinds" over every
universe partition in a worker thread. There is no rate limiting, no request
size cap, and no concurrency limit anywhere in the app.

On `127.0.0.1` this is acceptable. The moment `serve` is pointed at a real
interface it is an unauthenticated resource-exhaustion vector.

**Fix:** apply `Depends(verify_api_key)` to the two heavy endpoints at minimum;
better, add a general rate-limiting middleware so the contract becomes
"reads are public, *all* POSTs are gated."

---

## High

### H1. Fee calculation silently switches to a different model

`execution/fees.py::FeeCalculator.calculate()` dispatches by comparing its own
constructor arguments against a hardcoded defaults dict:

```python
defaults = {"_brokerage_pct": Decimal("0.03"), ...}
if any(getattr(self, name) != default for name, default in defaults.items()):
    return self._calculate_legacy(fill)
```

The two paths **do not agree structurally**. From the source comment:

> GST base differs: canonical (brokerage+exchange+sebi)*0.18, legacy
> (brokerage+exchange)*0.18 — flagged for removal.

So `FeeCalculator()` and `FeeCalculator(brokerage_pct=Decimal("0.029"))` compute
GST on different bases — not a scaled difference, a structurally different
charge. A caller who overrides a single rate to a custom value silently
transitions to a different fee model.

Three things make this worse than a normal code smell:

- The docstring asserts **"one fee model, not two"** — the opposite of what the
  code does.
- The `sebi_charge_pct` default is `0.0001` and the canonical sentinel is
  `0.0001`, but they mean different things because the legacy path divides by
  100. The equality that selects the path is a coincidence of encoding.
- The divergence is **pinned by tests** (`test_fees_validation.py:66` — *"Canonical
  GST includes SEBI in base; legacy excludes — pinned for H1"*), so it is
  entrenched rather than incidental.

For a system where fees feed P&L, this is a correctness hazard. The legacy path
should be deleted and custom rates expressed in the canonical model, or the
dispatch made explicit via a named constructor argument.

### H2. Type checking covers 6% of the codebase

CI runs `mypy domain/src/tradex_domain` only — 4,852 of 82,199 LOC. `brokers`
and `trading` (77,347 LOC, **94%**) are never type-checked, despite the README
documenting per-package mypy commands for all three.

Aggravating this, **neither `ruff` nor `mypy` is installed anywhere** — not
globally, not in `.venv`. The lint and typecheck commands in the README cannot
be run by a developer on this machine at all.

### H3. Verified unused imports (lint debt invisible without ruff)

AST scan of all 187 source files, excluding legitimate `__init__.py`
re-exports, found **32 genuinely unused imports**. Confirmed by grep — each
symbol appears only on its import line:

- `interface/fastapi_app.py` — `asyncio`, `json`, `HTTPException`,
  `WebSocketDisconnect`, `CapabilityNotSupportedError`, `AccountResponse`,
  `ErrorResponse`, `HealthResponse`, `OrderResponse`, `PositionResponse`,
  `CONTROL_QUEUE_MAX`, `_enqueue_drop_oldest`, `_enqueue_control_drop_oldest`,
  `serialize_position`, `enrich_chain_live`, `resolve_underlying_instrument`,
  `serialize_option_chain`, `api_key_header`
- `common/provider_common.py:24` — all 9 names imported from `.instruments`
- `domain/wire.py:26` — `normalize_symbol`
- `runtime/live.py` — `load_env_file`, `curl_cffi`

`fastapi_app.py` is the notable one: it imports the entire response-model
surface and most of its helpers without using them.

---

## Medium

### M1. Half-finished auth refactor leaves dead wiring

The in-flight change correctly replaces a `!=` API-key comparison with
`secrets.compare_digest` (a real improvement — the old form was a timing
oracle). But it left the old path in place rather than removing it:

- `auth.py::make_verify_api_key` is marked *"Deprecated — kept for import
  compat"* and is now a byte-for-byte duplicate of `verify_api_key`.
- `fastapi_app.py:97` assigns `verify_api_key = make_verify_api_key(lambda: app.state)` —
  a **local variable that is never read**. Every route imports `verify_api_key`
  from `auth.py` directly. Grep confirms line 97 is its only occurrence in the file.
- `fastapi_app.py:21` imports `api_key_header`, also never read.
- `auth.py:50` has a mid-file `from fastapi.security import APIKeyHeader` with a
  `# noqa: E402` and the comment *"compat export only"*.

Net effect: three dead symbols and one dead local, from a refactor that is
otherwise correct. Delete `make_verify_api_key`, `api_key_header`, the bottom
import, and line 97.

### M2. Untracked scratch is not gitignored

`.gitignore` does not cover these, so they show up in every `git status`:

```
_frontend_files.txt   _git_log.txt   _git_status.txt   _pwd.txt   _todo.txt
candles_app.py        smoke_market_feed.py   smoke_mcx_stream.py
smoke_synthetic_ticks.py
DataAnalysisExpert/   .complexipy_cache/   .jscpd-out/
```

The `_*.txt` files are shell scratch from a previous session (`_pwd.txt`
literally contains a `pwd` result). The three `smoke_*.py` scripts may have
value — if so, move them under `scripts/`; otherwise delete them.

Also committed: `.poolside/settings.local.yaml`, a machine-local tool allowlist
that should not be shared.

### M3. Repository has never been packed

8,769 loose objects totalling 216 MiB, **zero packfiles**. No large blobs are
tracked (largest is 47 KB), so this is accumulated history rather than a
mistake — but `git gc` is well overdue, and clone/fetch will be slow until it runs.

### M4. Global `DeprecationWarning` suppression

Root `pyproject.toml`:

```toml
filterwarnings = ["ignore::DeprecationWarning"]
```

This blanket-suppresses deprecation warnings from your own code as well as
dependencies — including the `DurableTokenManager` deprecation notice, which is
therefore never actually seen by anyone. Prefer targeted `ignore` entries for
specific third-party warnings.

### M5. `DurableTokenManager` is deprecated with no production consumer

Marked `.. deprecated::` and emits a warning on construction. Grep shows
references only in its own module, the `common/__init__.py` re-export, and five
test files. Nothing in `brokers/src` or `trading/src` uses it. It is dead
production code kept alive only as public API.

### M6. No frontend CI

Neither workflow touches `frontend/`. `package.json` exposes `typecheck`
(`tsc --noEmit`), `build`, `dev`, `preview` — but nothing runs them, and there
is no eslint and no test setup for 4,154 lines of TypeScript driving the
trading UI.

### M7. Python version drift

Four different answers to "what Python is this?":

| Source | Version |
|--------|---------|
| Local `.venv` | 3.13.5 |
| `uv.lock` | `>=3.13` |
| All three packages | `>=3.12` |
| Both CI workflows | 3.12 |

---

## Low

- **19 `except Exception: pass` sites** — mostly justified on inspection
  (defensive teardown in `session.py`, annotated `# pragma: no cover –
  defensive`; `base.py:186` carries a deliberate `# noqa: BLE001` with a
  rationale). Six cluster in `interface/routes/stream.py`; worth a second look,
  but not a systemic problem.
- **Coverage is unmeasured.** No `coverage` or `pytest-cov` installed, so the
  `--cov` flags in CI (and the `.coverage` file at root, last touched Aug 6)
  reflect no current signal. There is no coverage gate.
- **11 modules have no direct test reference** — `brokers`: `dhan._marketdata`
  (369), `upstox._marketdata` (385), `dhan._facade`, `upstox._facade`,
  `dhan._admin`, `upstox._admin`, `upstox._alerts`; `trading`:
  `execution.order_persistence`, `interface.routes._market_helpers` (154),
  `interface.routes.deps`, `sdk.services._helpers`. The `_marketdata` modules
  are likely exercised indirectly through the facades — worth confirming rather
  than assuming.
- **`data/` is 334 MB** and `data/ohlcv/` is ignored, but the `data/` parent is
  not, so sibling directories would be picked up by `git add`.

---

## What's genuinely good

Worth being explicit about, because these are the things that are easy to lose
in a cleanup:

- **Boundaries are really enforced.** Verified: `domain` imports no internal
  package; `brokers` never imports `tradex_trading`. Not just documented — true.
- **2,740 tests, all passing, in 39 seconds.** Fast enough to run constantly.
- **Duplication of 2.65%** on 82k LOC is genuinely good. The largest files
  (`resilience.py` 1,213, `indicators.py` 1,159) are cohesive rather than
  accreted.
- **Zero `TODO`/`FIXME`.** Remarkable at this size.
- **No secrets committed.** `.env.local` is untracked, and a pattern scan for
  API keys / AWS keys / private keys across all tracked files returns nothing.
- **CORS is locked to a single origin** with an explicit comment explaining why
  `*` was abandoned. No wildcard anywhere.
- **Constant-time API key comparison**, and `_require_api_key_for_live` refuses
  to start a live-broker server without a key configured. Fail-closed by default.
- **The `parity-gate` workflow is a genuinely sophisticated idea** — asserting
  that backtest, replay, paper, and live produce identical signals, fills,
  positions, and P&L from the same event stream. That is the right invariant for
  a trading platform.
- **The in-flight risk work is correct and valuable.** `RiskManager` was letting
  MARKET orders with no price collapse their notional to zero, silently
  bypassing the order-value gate. The new `_mark_for_trade` plus
  `reject_unknown_market_value` fixes a real hole, and the daily-loss/drawdown
  guards correctly allow reductions so a breached limit can never lock a
  position. This change is well-reasoned — ship it.
- **Immutability and `Decimal` money** applied consistently, which is what makes
  the parity gate feasible at all.

---

## Recommended order of work

1. **Repair CI** (C1). Add a root `[project]` + dev dependency group, regenerate
   a real `uv.lock`, align `requires-python` to one version. Nothing else in
   this list is verifiable until CI actually runs.
2. **Install `ruff` and `mypy`** into the dev environment and fix what they
   report (H2, H3). Expect the unused-import list above to be a subset.
3. **Gate the two heavy POST endpoints** and add rate limiting (C2).
4. **Decide on the fee model** (H1) — collapse to one path and update the
   docstring that currently claims there is only one.
5. **Finish the auth refactor** (M1) — delete the deprecated duplicate and its
   dead wiring.
6. **Hygiene pass** (M2, M3, M4) — gitignore or delete the scratch, drop
   `.poolside/settings.local.yaml`, run `git gc`, narrow the warning filter.
7. **Extend mypy** to `brokers` and `trading` incrementally, and add frontend
   `tsc --noEmit` to CI (H2, M6).
