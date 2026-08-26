# P2 Task 3 Report — POST /api/charts/profiles/{id} route

**Date:** 2026-08-26
**Status:** DONE

## Summary

Exposed the P2 Task 2 compute functions (`compute_profile`, `compute_seasonality`) as a stateless HTTP endpoint `POST /api/charts/profiles/{profile_id}`, mirroring the P1 transforms route (`POST /api/charts/transforms/{transform_id}`) in `trading/src/tradex_trading/interface/routes/chart.py`. Added `trading/tests/interface/test_profiles_route.py` (3 tests) following the `TestClient(create_app(session=None))` convention.

## Files

- `trading/src/tradex_trading/interface/routes/chart.py` (modified) — new `compute_profile_endpoint` added after the transforms endpoint.
- `trading/tests/interface/test_profiles_route.py` (created) — 3 tests.

## Seasonality-dispatch decision

The seasonality study goes through the SAME route. Inside the endpoint the dispatcher branches on the path param:

```python
if profile_id == "seasonality":
    result = compute_seasonality(bars, params)
else:
    result = compute_profile(profile_id, bars, params)
```

- `compute_profile("no-such", ...)` and `compute_seasonality` both raise `ValueError` on unknown ids, caught by one `except ValueError` → HTTP 422 (same detail-passing style as the transforms route).
- No special-casing of the seasonality branch beyond the dispatch: it still requires a non-empty `bars` list (422 otherwise) and honors `params` as-is (seasonality builds its own snake_case settings from the params dict).
- `profile_id` itself is NOT re-read from the body — the path param is authoritative; the body `id` is echoed back verbatim in the response (contract: `{ "id": str, "result": {...} }`).

## Route contract

`POST /api/charts/profiles/{profile_id}`
- body: `{ "id": str, "params": {...}, "bars": [{"time", "open", "high", "low", "close", "volume"}] }`
- response: `{ "id": profile_id, "result": {...} }`
- 422 on: missing/empty/non-list `bars` (`"profile requires a non-empty bars array"`), unknown id (`ValueError` detail).
- Stateless — no datalake or session access.

## Tests

Written first, confirmed failing (route missing → 404), then implemented to green.

1. `test_profiles_endpoint_volume_profile` — POST volume-profile with the 300-bar fixture; asserts 200, `body["id"]`, and `body["result"] == compute_profile("volume-profile", BARS, {})`.
2. `test_profiles_endpoint_seasonality` — POST seasonality with the synthetic 13-month series (`_seasonality_bars()` copied from `test_golden_parity_profiles.py`: Jan 2025..Jan 2026, one bar/month, close = 100 + 3·m); asserts 200, `body["id"]`, and `body["result"]["rows"][0][0]["text"] == "Year"`.
3. `test_profiles_endpoint_unknown_id_422` — POST unknown id; asserts 422.

## Verification (exact output)

New route tests:

```
$ ../.venv/bin/python -m pytest trading/tests/interface/test_profiles_route.py -q
...                                                                      [100%]
3 passed in 0.34s
```

Analytics suite (no regression):

```
$ ../.venv/bin/python -m pytest trading/tests/analytics/ -q
...
324 passed in 0.41s
```

Sibling interface regression (transforms route + profiles route):

```
$ ../.venv/bin/python -m pytest trading/tests/interface/test_transforms_route.py trading/tests/interface/test_profiles_route.py -q
......                                                                   [100%]
6 passed in 0.41s
```

Ruff on both changed files:

```
$ ../.venv/bin/ruff check trading/src/tradex_trading/interface/routes/chart.py trading/tests/interface/test_profiles_route.py
All checks passed!
```

(One fixable `W292` no-newline-at-EOF on the new test file was auto-fixed by appending the trailing newline before the final ruff run.)

## Commit

```
feat(api): POST /api/charts/profiles/{id} stateless endpoint
```

Staged only `trading/src/tradex_trading/interface/routes/chart.py` and `trading/tests/interface/test_profiles_route.py`.

## Concerns

None. The full `tests/interface/` suite was not run to completion (it exceeds the 120 s shell timeout due to datalake-heavy tests), but the route-local and analytics gates per the brief are green.