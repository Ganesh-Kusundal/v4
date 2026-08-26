# P1 Task 3 Report — POST /api/charts/transforms/{id} route

- **Date:** 2026-08-26
- **Commit:** `36b0cd4d3a02c27f9b8a8266b5c140934b155e7b` — `feat(api): POST /api/charts/transforms/{id} stateless endpoint`
- **Status:** DONE_WITH_CONCERNS

## What shipped

Exposed Task 2's pure `compute_transform` as a stateless HTTP endpoint:

- `trading/src/tradex_trading/interface/routes/chart.py` — added `POST /transforms/{transform_id}` inside `create_chart_router(session)`, directly after the indicator compute endpoint. Lazy-imports `compute_transform`, validates `bars` (non-empty list) and `params` (object), converts `ValueError` → HTTP 422, and returns `{"id": transform_id, "bars": result}`.
- `trading/tests/interface/test_transforms_route.py` — 3 tests using the shared 300-bar `fixtures.json` payload.

## Pattern deviation (create_app vs create_chart_router)

The plan's test skeleton built a bare `FastAPI()` and called `create_chart_router(None)` directly. Per the brief, I used the established codebase convention instead — `TestClient(create_app(session=None))` — matching `test_chart_indicators.py` exactly (including `fastapi = pytest.importorskip("fastapi")` + `# noqa: E402` imports). This is the working, maintained pattern and the transforms route is stateless (bars come in the body; no datalake/session needed), so `create_app(session=None)` exercises the full app wiring with zero added cost.

## Tests (TDD)

Wrote the 3 tests first; confirmed they failed with `404 Not Found` (route absent), then implemented the route and re-ran to green.

```
$ ../.venv/bin/python -m pytest trading/tests/interface/test_transforms_route.py -q
...                                                                    [100%]
3 passed in 0.36s
```

## Full suites

```
$ ../.venv/bin/python -m pytest trading/tests/interface/test_transforms_route.py trading/tests/analytics/ -q
.................................                                        [100%]
321 passed in 0.64s
```

(3 route + 318 analytics — the 318 target confirmed.)

## Ruff

```
$ ../.venv/bin/ruff check trading/src/tradex_trading/interface/routes/chart.py trading/tests/interface/test_transforms_route.py
All checks passed!
```

Note: the plan's test skeleton used `l` as a loop variable; `trading/tests/interface/test_transforms_route.py` is gated by ruff `E741` (ambiguous `l`), so I renamed it `low` — matching the existing `test_golden_parity_transforms.py` (Task 2), which already avoids E741 the same way.

## Concerns

1. **Full `trading/tests/interface/` suite is NOT green in this environment**, and this is pre-existing (verified by stashing my change and re-running — identical results on baseline `0fb5a38`):
   - `test_ws_bars_replay.py` hangs indefinitely. The repo-root `pyproject.toml` sets `addopts = "--timeout=120"` (requires pytest-timeout), but running `trading/tests/...` resolves the pytest rootdir to `trading/pyproject.toml`, whose `addopts` omits `--timeout`. So the WS test runs without a timeout and blocks.
   - `test_chart_indicators.py`'s `TestComputeEndpoint` (6 tests) errors at setup with `ModuleNotFoundError: No module named 'duckdb'` — the datalake dependency is not installed in `../.venv`.
   - `test_chart_history.py` / `test_chart_backtest.py` skip (datalake-dependent).
   - The non-datalake, non-WS interface files all pass (`test_fastapi_app 73`, `test_interface_modules 21`, `test_order_status_mapping 4`, `test_ui_mount 3`, `test_transforms_route 3`).
   - My change introduces **zero** regression here: baseline produced identical errors/skips, and the transforms route/tests are datalake-free.
2. **Venv path trap:** the repo-local `v4/.venv` lacks fastapi; the working venv is the parent `/Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/.venv`. Verification must run `../.venv/bin/python` from the repo root `v4/` (not from `v4/trading/`), which is what this report's commands assume.

## Files changed (only the two required)

- `trading/src/tradex_trading/interface/routes/chart.py`
- `trading/tests/interface/test_transforms_route.py`
