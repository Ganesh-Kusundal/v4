# Design Spec: `.env.local` Loading for v4 CLI

## Status
- **Date**: 2026-08-06
- **Owner**: v4 token-management alignment
- **Approver**: User

## Problem

v4's CLI entrypoints (`check_connection.main`, `cli.main`) never load `.env.local`.
All broker credentials (`DHAN_*`, `UPSTOX_*`) in `.env.local` are silently ignored.
The `check_connection` tool reports `PASS (no session — dry run)` without ever
reading credentials, creating a false-positive connection status.

v3 solves this with an explicit `--env-file` opt-in flag that calls
`load_env_file()` — a minimal KEY=VALUE parser with error handling.

## Solution: Port v3's `load_env_file()` pattern (Approach A)

Copy v3's `load_env_file()` function into v4's `config/env.py`, add `--env-file`
CLI flag to `check_connection.main()` and `cli.main()`.

### Why not `python-dotenv`?
- Not declared in `trading/pyproject.toml` `[project.dependencies]`.
- v3 deliberately avoided it for control over parsing and error handling.
- Adds an implicit dependency.

### Why not load-on-import?
- v3's docstring explicitly states: "runtime.boot never discovers or loads
  credential files implicitly."
- Import-time side-effects violate this safety contract.

## Changes

### 1. `trading/src/tradex_trading/config/env.py`
- Add `load_env_file(path, *, override=False) -> tuple[str, ...]` (ported from v3)
- Add `re` and `shlex` imports (already used by v3's implementation)
- Add `SDKError` re-export from `tradex_domain` (v3 uses `tracex_domain.errors.SDKError`)

### 2. `trading/src/tradex_trading/interface/check_connection.py`
- Add `--env-file` argparse argument (default: `None`)
- Call `load_env_file()` if `--env-file` is provided, before broker checks
- Print loaded entry count (matches v3 pattern)

### 3. `trading/src/tradex_trading/interface/cli.py`
- Add `--env-file` argparse argument
- Call `load_env_file()` if provided, before `boot()`

## Default Path

No default `.env.local` path in v4 (explicit opt-in only). User passes
`--env-file` when needed. This is stricter than v3 (which defaulted to
`v3/.env.local`) but follows v3's docstring guidance that env file loading
should be explicit.

## Testing

1. Run `check_connection --broker dhan --env-file .env.local` — should load
   all entries without error.
2. Confirm `DHAN_ACCESS_TOKEN` is visible in `os.environ` after load.
3. No mocked data — uses real `.env.local` file with real (non-secret)
   values.

## Out of Scope

- FastAPI app loading (`fastapi_app.py`) — not needed for CLI workflow.
- Auto-loading on import — explicitly rejected.
- Removing v3's `load_env_file` — v3 is legacy, untouched.
