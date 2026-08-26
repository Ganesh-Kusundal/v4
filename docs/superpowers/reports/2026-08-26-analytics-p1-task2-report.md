# Analytics P1 — Task 2 Report: Dedup private helpers

- **Date:** 2026-08-26
- **Plan:** `docs/superpowers/plans/2026-08-26-analytics-debt-cleanup.md` (Task 2)
- **Brief:** `.superpowers/sdd/briefs/task-2-brief.md`
- **Status:** DONE_WITH_CONCERNS
- **Commit:** `d0ca77d` (refactor(analytics): dedup private helpers into indicators.py canonical set)
- **Baseline:** analytics suite `311 passed`, golden gate `91 passed`

## Summary

Promoted the canonical helper set (`_lowest`, `_shift`, `_rma`, `_sma_skip_none`,
`_bars_since`) into `indicators.py` (exactly as specified in the brief) and rewired
the sibling modules to import them, deleting ~30 local defs (`-298` / `+136` lines).

## Per-module changes

| Module | Deleted | Imported from `indicators` | Kept local (why) |
|---|---|---|---|
| `indicators.py` | — | — | Added canonical `_lowest`, `_shift`, `_rma`, `_sma_skip_none`, `_bars_since` (verbatim from brief, incl. the `# type: ignore[type-var]`). |
| `oscillators_strength.py` | `_highest`, `_lowest` | `_highest`, `_lowest` | **`_change` kept** — see Concern 1 (canonical `_change` crashes on the warmup Nones `trix` feeds; the local None-safe variant is the semantically-correct distinct version). Also kept `_f`, `_closes`, `_highs`, `_lows`, `_sma_gapped`, `_ppo_ma`, `_tsi_series`, `double_smooth`, `ema_ema`. |
| `oscillators_range_b.py` | `_highest`, `_lowest`, `_change`, `_rolling_sum` | same | `_percent_rank`, `connors_streak` (all callers feed finite inputs; verified). |
| `volatility_chop.py` | `_highest`, `_lowest`, `_rolling_sum`, `_shift`, `_to_float` | same | `_closes`, `_highs`, `_lows`, `_stdev_gapped` (canonical `_to_float` verified safe: callers pass Decimal/float `.value`). |
| `volatility_bands.py` | `_change`, `_rolling_sum`, `_to_float` | same | `_highest_skip`, `_lowest_skip` (distinct skip-semantics, per brief). |
| `volume_flow.py` | `_sma_skip_none` | `_sma_skip_none` | `_vol`, `_money_flow` (per brief; the three `_vol` variants untouched). |
| `volume_indices.py` | — | — | No merge (distinct NaN/None variants; untouched). |
| `studies_simple.py` | `_rma` | `_rma` | `_src_val`, `_source_values`, `_vol`, `_roc_ts`, `_vwma`, `_moving_average`, `_cci`. |
| `studies_trend.py` | `_rma`, `_lowest`, `_shift`, `_bars_since` | same | `_f`, `_vol`, `_extract`, `_true_ranges`, `_atr_arrays`, `_money_flow_index`. |
| `studies_complex.py` | — | `_rma` (import source switched from `studies_simple`) | everything else. |
| `studies_signals.py` | `_sma_skip_none`, `_shift`, `_bars_since` | same | `_sma_of_gapped`, `_stdev`, `_highest_skip`, `_lowest_skip`, `_shift_flags`, `_pivot*`, `_value_when`, `_correlation`, `_is_fractal`, `_hlc3` (distinct semantics). |
| `band_overlays.py` | `_to_float` | `_to_float` | `_highest`/`_lowest`/`_shift` — **not in the brief's rewire list**; left untouched (documented in a NOTE comment per the grep rule). |
| `oscillators_range_a.py` | — | — | `_rolling_sum` kept (not in rewire list); added a deliberate-keep note documenting the one-step vs canonical two-step FP accumulation difference. |
| `oscillators_trend.py` | — | — | `_rma` (NaN-propagating, distinct), `_highest_bars`/`_lowest_bars` (offset-to-extreme, distinct). Fixed a now-stale module docstring claim ("indicators.py does not export an rma"). |

## Verification (exact commands + outputs)

Baseline (before any change):
```
$ ../.venv/bin/python -m pytest trading/tests/analytics/ -q
311 passed in 0.49s
$ ../.venv/bin/python -m pytest trading/tests/analytics/test_golden_parity.py -q
91 passed in 0.29s
```

After each module swap, both suites re-ran green (311 / 91). Final run:
```
$ ../.venv/bin/python -m pytest trading/tests/analytics/ -q
311 passed in 0.45s
$ ../.venv/bin/python -m pytest trading/tests/analytics/test_golden_parity.py -q
91 passed in 0.30s
$ ../.venv/bin/ruff check trading/src/tradex_trading/analytics/ --output-format=concise
# 8 findings — ALL pre-existing (verified identical via `git stash` on the
# untouched baseline): band_overlays.py:112 E741, ma_vol.py:119 E741,
# oscillators_strength.py:131/259/352 E501, oscillators_trend.py:266 E501,
# volatility_chop.py:196 E741, volatility_stops.py:153 E501.
# No new F401/F811/I001 introduced by this task.
$ grep -rn "def _highest\|def _lowest\|def _rolling_sum\|def _shift\|def _rma\|def _sma_skip_none\|def _bars_since" trading/src/tradex_trading/analytics/*.py
# Every hit is indicators.py OR a deliberately-kept local, all documented.
```

## Golden failure mid-swap and resolution (semantic check)

**`oscillators_strength._change`** — the brief's rewire list says to import
`_change` from `indicators.py`, but this is the *reverse* of the trap described
in the brief: the local variant is **None-safe** (skips None windows, matching TS
`NaN` propagation), while the canonical `indicators._change` **crashes**
(`None - None` → `TypeError`). `trix()` feeds the triple-EMA's warmup Nones into
`_change` (verified: `e3 = _ema_of_gapped(e2, period)` carries leading Nones).

Empirically confirmed by temporarily swapping in the canonical import:
```
$ ../.venv/bin/python -m pytest "trading/tests/analytics/test_golden_parity.py::test_parity_against_ts_source[trix]" -q
E   TypeError: unsupported operand type(s) for -: 'NoneType' and 'NoneType'
FAILED ...test_parity_against_ts_source[trix]
1 failed in 0.25s
```
Resolution per the brief's rule ("NEVER assert a crash"): kept the local
None-safe `_change` in `oscillators_strength` and documented it as a deliberately
distinct variant. The golden then passes. This is the same "keep the distinct
variant, document it" pattern the brief itself applies to `_highest_skip` /
`_lowest_skip` / the three `_vol` variants.

No other golden test failed during any swap.

## Concerns

1. **`oscillators_strength._change` was not dedupable.** The brief's item 1
   explicitly lists `_change` for deletion, but the canonical `indicators._change`
   is not None-safe and crashes on `trix`'s warmup Nones (golden ERROR, confirmed
   above). The local None-safe `_change` was therefore kept and documented. If the
   canonical `_change` is ever made None-safe (blank-on-None, TS NaN parity), this
   local can be deleted. This is the only deviation from the brief's literal rewire
   list; everything else is exactly as specified.
2. **Pre-existing ruff findings (8) remain untouched** — all verified present on
   the clean baseline (`git stash`), none introduced by this task. They are E741/E501
   style nits out of this task's scope.
3. `oscillators_range_a._rolling_sum` and `oscillators_trend._rma` were not in the
   brief's rewire list; both are semantically distinct from the canonical helpers
   (one-step FP accumulation / NaN propagation) and are now explicitly documented
   as deliberate keeps. No behavior change.

## Fix round 1: root-cause `_change`

Concern 1 (kept local `_change` in `oscillators_strength`) is now resolved at the
root: the canonical `indicators._change` is None-safe (blank-on-None, matching TS
NaN propagation) instead of the local copy being kept. `oscillators_strength`
imports the canonical like everyone else.

**Changes:**
- `indicators.py:585` — `_change` now None-safe: iterates `_to_float` values and
  skips pairs where either side is None (`continue` → stays `None`). On finite
  inputs output is byte-identical to before (strict superset).
- `oscillators_strength.py` — deleted the local `_change` (and its docstring);
  `_change` added to the existing `from tradex_trading.analytics.indicators import (...)`
  block. Both usages (`_tsi_series` line 80, `trix` line 209) now resolve to the
  imported canonical.

**Commands + outputs:**
```
$ ../.venv/bin/python -m pytest trading/tests/analytics/ -q
311 passed in 0.41s
$ ../.venv/bin/python -m pytest trading/tests/analytics/test_golden_parity.py -q
91 passed in 0.27s
$ ../.venv/bin/ruff check trading/src/tradex_trading/analytics/oscillators_strength.py trading/src/tradex_trading/analytics/indicators.py
# 3 E501 findings on oscillators_strength.py:117/245/338 — ALL pre-existing
# (verified identical via `git stash` on the pre-fix baseline). None introduced.
$ grep -rn "def _change" trading/src/tradex_trading/analytics/*.py
trading/src/tradex_trading/analytics/indicators.py:585:def _change(values: list, n: int = 1) -> list[float | None]:
```

**Commit:** `08c3688` (refactor(analytics): make _change None-safe, drop local copy in oscillators_strength)