# `tradex_http` shared API-key check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One constant-time API-key comparison, shared by the gateway and interfaces, that returns `False` instead of raising on a non-ASCII key.

**Architecture:** A new leaf workspace package `tradex_http` (directory `httputil/`) (no `tradex_*` imports) holds `keys_match(provided, expected) -> bool`. Both services call it at all 10 compare sites. Each service keeps its own HTTP adapter, error vocabulary and limiter.

**Tech Stack:** Python 3.13, stdlib `hmac`, hatchling workspace member, import-linter, pytest.

**Spec:** none. Evidence is the 2026-11-19 audit (candidate 5: "Auth and rate limiting exist twice") and the user's "Concrete fix plan", Priority 2. The decision "independent deployables that share primitives" was made earlier this session.

## Global Constraints

- The gateway must not import `tradex_interfaces`, so the shared code sits in a leaf both can import.
- `tradex_http` imports no `tradex_*` package and no third-party package (stdlib only).
- Do NOT merge gateway into interfaces. Do NOT touch brokers' `as_decimal`/`as_price`/`parse_timestamp`.
- The gateway's loopback-anon guard (`check_api_key` handling of `api_key is None` / `allow_anon`) stays in the gateway.
- Work in place on `integration/wave1` (editable venv, CLAUDE.md lesson 1). `git add` only each task's files.
- A new workspace member is not importable until `uv sync` (or an editable install) runs; the root `pythonpath` list in `pyproject.toml` is what pytest uses.

## Review Focus

- A non-ASCII key (`"é"`, from a latin-1-decoded header) returns 401/403, never a 500 (Task 1 test, Task 3 route tests).
- Empty-string provided or expected key never matches: an unset key must not turn into "any empty header passes" (Task 1).
- `check_api_key` still allows anon only when `api_key is None and allow_anon` (existing gateway tests must pass unchanged).
- Behaviour that must not move: wrong key keeps its current status per route (gateway 401, interfaces 401/403 as today).

## Findings that shaped this plan (re-derived, not trusted from the audit)

- The two **limiters are different algorithms**: gateway `TokenBucket` (continuous refill, `consume -> (ok, wait)`) and interfaces `InMemoryRateLimiter` (sliding window, `check -> int | None`). Brokers has a third, `TokenBucketRateLimiter`. Unifying them changes who gets throttled and when, so this plan does **not** share limiters. The audit's "share the token bucket" is dropped.
- The shareable primitive is the key comparison: 10 sites (`gateway/auth.py:21`, `interfaces/auth/deps.py:88,121,211`, `auth/rate_limit.py:119`, `auth/routes.py:147`, `fastapi_app.py:389`, `routes/stream.py:147`; `auth/csrf.py:34` compares CSRF tokens and is out of scope). They mix `hmac` and `secrets`, and all of them raise `TypeError` on a non-ASCII `str` (verified: `hmac.compare_digest('é','secret')` raises `TypeError`). A header can carry such a character.
- Cost/benefit: a package for one function is heavy, but the user chose a leaf package, and the workspace wiring is small (one member, one source, one linter contract, one pythonpath entry).

---

### Task 1: Create `tradex_http` with `keys_match`

**Files:**
- Create: `httputil/pyproject.toml`, `httputil/src/tradex_http/__init__.py`, `httputil/src/tradex_http/keys.py`
- Test: `httputil/tests/test_keys.py`

**Interfaces:**
- Produces: `tradex_http.keys_match(provided: str | None, expected: str | None) -> bool`. `False` when either side is `None` or empty; compares UTF-8 bytes with `hmac.compare_digest`; never raises on any `str`.

- [ ] **Step 1: Failing tests** in `httputil/tests/test_keys.py`: `test_match`, `test_mismatch`, `test_none_and_empty_never_match` (`(None,"k")`, `("k",None)`, `("","")`, `("","k")`), `test_non_ascii_returns_false_not_raises` (`keys_match("é","secret") is False`), `test_non_ascii_equal_matches` (`keys_match("é","é") is True`).
- [ ] **Step 2: Run** `.venv/bin/python -m pytest httputil/tests -q` with `httputil/src` on the path (`PYTHONPATH=httputil/src`). Expected: FAIL, `ModuleNotFoundError: tradex_http`.
- [ ] **Step 3: Implement** `keys_match` in `keys.py`, re-export from `__init__.py` with `__all__`. `pyproject.toml` copies `observability/pyproject.toml` with name `tradex-http`, package `src/tradex_http`, `dependencies = []`.
- [ ] **Step 4: Run** the same command. Expected: 5 passed.
- [ ] **Step 5: Commit** `feat(http): keys_match, a constant-time key check that never raises`.

### Task 2: Wire the package into the workspace

**Files:**
- Modify: `pyproject.toml` (`[tool.uv.workspace].members` add `"httputil"`; `[tool.uv.sources]` add `tradex-http = { workspace = true }`; pytest `pythonpath` add `"httputil/src"`; `[tool.importlinter] root_packages` add `"tradex_http"`; new contract)
- Modify: `tests/test_import_boundaries.py` (`_SRC` add `"http": _ROOT / "httputil/src"`)
- Modify: `gateway/pyproject.toml`, `interfaces/pyproject.toml` (add `"tradex-http>=0.1.0,<0.2"`)

**Interfaces:**
- Produces: contract "http is a leaf": `source_modules = ["tradex_http"]`, forbidden = every other root package. Consumes Task 1.

- [ ] **Step 1: Make the edits above.** Contract copies the `reactive layer` block's shape.
- [ ] **Step 2: Install** `uv sync --frozen 2>&1 | tail -3` (fall back to `uv pip install -e httputil` if the lock rejects). Expected: `tradex_http` imports without `PYTHONPATH`.
- [ ] **Step 3: Run** `.venv/bin/python -c "import tradex_http"`, `.venv/bin/lint-imports` (if present) and `.venv/bin/python -m pytest tests/test_import_boundaries.py httputil/tests -q`. Expected: all pass; the new contract is kept.
- [ ] **Step 4: Commit** `build: add tradex_http workspace member and leaf import contract`.

### Task 3: Adopt `keys_match` at every compare site

**Files:**
- Modify: `gateway/src/tradex_gateway/auth.py`; `interfaces/src/tradex_interfaces/auth/deps.py`, `auth/rate_limit.py`, `auth/routes.py`, `fastapi_app.py`, `routes/stream.py`
- Test: `gateway/tests/test_api.py` (non-ASCII header → 401), `interfaces/tests/test_api_key_non_ascii.py` (new: REST order route with `X-API-Key: é` → 401/403 not 500)

**Interfaces:**
- Consumes: `keys_match` from Task 1. Each site keeps its existing wrong-key outcome; only the comparison call changes. `expected is None` branches stay where they are (they encode the no-auth policy).

- [ ] **Step 1: Failing tests first**: one gateway test and one interfaces test sending `X-API-Key` with a non-ASCII value (`httpx` encodes headers latin-1). Run; Expected: both FAIL with a 500 (or raise) today. If either already returns 401, drop that site from the claim and say so in the commit.
- [ ] **Step 2: Replace** each `hmac.compare_digest(...)` / `secrets.compare_digest(...)` with `keys_match(provided, expected)`, removing `str()` coercions and unused `hmac`/`secrets` imports (`ruff --fix`). Leave `auth/csrf.py` alone.
- [ ] **Step 3: Run** `.venv/bin/python -m pytest gateway interfaces trading/tests/interface tests -q`. Expected: the new tests pass; the failing set equals the known baseline (10 order/auth tests returning 422 where 401 is expected, plus `test_cors_csrf_origin_parity`). Compare names, not counts.
- [ ] **Step 4: Commit** `fix(auth): non-ASCII API key is rejected, not a 500; one key check for gateway and interfaces`.

### Task 4: Docs

**Files:**
- Modify: `CLAUDE.md` (architecture quick-reference: add `httputil/` line), `gateway/README.md` (allowed imports list: add `tradex_http`)

- [ ] **Step 1: Edit** both. Step 2: `git grep -n "tradex_http"` shows docs, config and code agree. Step 3: Commit `docs: tradex_http in the package map`.

## Not planned (with the reason)

- **Shared rate limiter.** Different algorithms and APIs, plus a third in brokers; unifying changes throttling behaviour. Needs its own decision.
- **Shared error vocabulary / HTTP adapters.** Each service owns its contract (`/v1` policy in the gateway README).
- **`auth/csrf.py` compare.** CSRF token, not an API key; same TypeError risk is possible but out of this plan. Noted for a follow-up.

## Self-review

- Coverage: Priority 2 (new leaf package, workspace/sources/importlinter/pythonpath wiring) → Tasks 1-2; adoption → Task 3; docs → Task 4.
- Names consistent: `keys_match`, `tradex_http`, `httputil/src/tradex_http/keys.py`.
- Unverified assumptions the implementer must confirm: that `uv sync` works offline in the sandbox; that a non-ASCII header actually produces a 500 at the gateway and interfaces (Task 3 step 1 proves it or drops the claim); that the directory name `httputil` (not `http`, which would shadow the stdlib module on `sys.path`) is free; the importable package stays `tradex_http`.
