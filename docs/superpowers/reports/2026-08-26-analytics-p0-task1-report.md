# Analytics Debt Cleanup — Task 1 (P0: delete dead code) — Report

**Date:** 2026-08-26
**Status:** DONE

## Summary

Pure-deletion cleanup of `tradex_trading/analytics`. Zero behavior change — both
gates stayed green throughout every step:

- Golden parity gate: `91 passed` (baseline and after all edits)
- Analytics suite: `311 passed` (baseline and after each step)

## Steps executed (in brief order)

### Step 1 — Remove 3 dead `sys.path.insert` hacks
Removed `import pathlib`, `import sys`, and the `sys.path.insert(...)` line from
`volume_flow.py`, `volume_indices.py`, `volume_simple.py`.

```
$ ../.venv/bin/python -c "import tradex_trading.analytics.volume_flow, tradex_trading.analytics.volume_indices, tradex_trading.analytics.volume_simple; print('ok')"
ok
```

### Step 2 — Remove unused imports (ruff F401)
```
$ ../.venv/bin/ruff check trading/src/tradex_trading/analytics/ --select F401 --fix
Found 15 errors (15 fixed, 0 remaining).
```
Pre-check for the CRITICAL gate:
```
$ grep -n "^def sma\|^def ema\|^def atr\|def _sma_seeded_ema" trading/src/tradex_trading/analytics/oscillators_strength.py
(no output)
```
No definitions in-module — the four imported names are never referenced anywhere
in the body (only the string literal `"ema"` appears, as a dict key / plot label).
Confirmed dead.

All 15 names from the brief's verified list were removed (see table below).

### Step 3 — Delete dead test assertion
Removed line 550 of `trading/tests/analytics/test_indicator_registry.py`:
```python
assert result["ao"] == pytest.approx(expected, nan_ok=False) if False else True
```
Always-true no-op; the manual None-aware loop below it does the real check.

### Step 4 — Remove unused locals in volatility_stops
Removed the dead `highs`/`lows` list comprehensions in `volatility_stop(...)`
(formerly lines 141–142). Grep confirmed `highs`/`lows` are only referenced in
the *other* two functions (Keltner/Donchian-style stops) where they are live.

### Step 5 — Fix unsorted-import blocks (ruff I001)
```
$ ../.venv/bin/ruff check trading/src/tradex_trading/analytics/ --select I001 --fix
Found 8 errors (8 fixed, 0 remaining).
```
Fixed: `indicators.py` ×2 blocks, `oscillators_range_a.py`, `oscillators_range_b.py`,
`volatility_chop.py`, `volume_flow.py`. All changes are pure import sorting
(block reordering, alphabetical member sorting, blank-line collapse, merging the
`volatility_chop.py` split import — `# noqa: F401` preserved). `register_indicator`
calls in `indicators.py` run after all imports complete, so order is irrelevant.

### Step 6 — Verify
```
$ ../.venv/bin/python -m pytest trading/tests/analytics/ -q
311 passed in 0.74s
$ ../.venv/bin/python -m pytest trading/tests/analytics/test_golden_parity.py -q
91 passed in 0.54s
$ ../.venv/bin/ruff check trading/src/tradex_trading/analytics/
Found 8 errors.   # 5 × E501, 3 × E741 — the expected cosmetic residue, out of scope
```

## Unused-import removal ledger (brief's verified list — 15 names)

| File | Names removed | Status |
|---|---|---|
| `median_study.py` | `Any` | removed |
| `oscillators_range_b.py` | `Any` | removed |
| `oscillators_strength.py` | `_sma_seeded_ema`, `atr`, `ema`, `sma` | removed (all genuinely dead — never called in body) |
| `oscillators_trend.py` | `Any` | removed |
| `volatility_stops.py` | `Any` | removed |
| `volume_flow.py` | `_sma_seeded_ema`, `sma` | removed |
| `volume_indices.py` | `math`, `_cumulative`, `_highest`, `sma` | removed |
| `volume_simple.py` | `math` | removed |

**All 15 were removable.** None of the 17 listed imports had to be kept.

## Concerns

1. **Brief's "17 unused imports" count vs. verified list = 15.** The verified
   list itself totals 15 names, not 17. The extra 2 in the headline count appear
   to be the `pathlib`/`sys` imports that were already deleted in Step 1 of this
   same task (3 files × 2 = 6 lines, but only 2 would round the total if counting
   files). Either way, the *verified* list is what matters and every item on it
   was removed. No action needed.
2. Only cosmetic lint residue remains: 5 × E501 (line too long) and 3 × E741
   (ambiguous `l` variable) — explicitly out of scope per the brief.
3. Commit staged ONLY the 12 intended files (11 analytics modules +
   `test_indicator_registry.py`). No datalake files, plan files, or untracked
   scratch files touched.

## Commit
```
chore(analytics): remove dead sys.path hacks, unused imports, dead assertion
```