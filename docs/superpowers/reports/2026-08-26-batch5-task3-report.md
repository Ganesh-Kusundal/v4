# Batch 5 · Task 3 — Complex Studies (studies_complex.py)

**Status:** DONE

**Date:** 2026-08-26

## File created

- `trading/src/tradex_trading/analytics/studies_complex.py` — 5 indicators:
  `cpr`, `range_analysis`, `vortex`, `relative_vigor_index`,
  `relative_volatility_index`, plus their `SPEC_*` dicts and `SPECS` list
  (registered by the Task 5 merge, not here).

## Verification

The golden parity harness skips these 5 ids today because `PARAM_MAP` /
`PLOT_MAP` are wired only in Task 5 (merge wiring). Per the brief I did **not**
edit `test_golden_parity.py`; instead I ran the harness's exact comparison
logic (same `_Candle` fixtures, `Decimal(str(v))` OHLC, `compute_indicator`,
`pytest.approx(abs=1e-9)` + None-alignment) in a throwaway script, using the
PARAM_MAP/PLOT_MAP Task 5 will add (snake_case mirrors — see Concerns).

Result — **all 5 bit-exact (worst absolute diff 0.0, 0 mismatches):**

```
cpr: PASS  mismatches=0  worst_abs_diff=0.000e+00
range-analysis: PASS  mismatches=0  worst_abs_diff=0.000e+00
vortex: PASS  mismatches=0  worst_abs_diff=0.000e+00
relative-vigor-index: PASS  mismatches=0  worst_abs_diff=0.000e+00
relative-volatility-index: PASS  mismatches=0  worst_abs_diff=0.000e+00
TOTAL mismatches: 0
```

Mission pytest command (currently skips — wiring is Task 5's job):

```
../.venv/bin/python -m pytest trading/tests/analytics/test_golden_parity.py \
  -k "cpr or range_analysis or vortex or relative_vigor_index or relative_volatility_index" -v
→ 2 skipped, 89 deselected   (cpr/vortex are the only ids the -k tokenizer hits;
  the hyphenated ids need Task 5's PARAM_MAP entries to run)
```

Regression check — full golden suite unchanged:

```
../.venv/bin/python -m pytest trading/tests/analytics/test_golden_parity.py -q
→ 71 passed, 20 skipped, 0 failed
```

Lint: `ruff check trading/src/tradex_trading/analytics/studies_complex.py` → All checks passed!

## Formulas implemented (matching the TS `calc()` bodies)

**cpr** (`studies.ts CPR`) — floor pivot, three d/w/m frames, 27 plots
`dPivot..mTc` exactly as `cpr.json` names them. Period detection reproduces
`feed/time.ts` bit for bit: median gap → `autoPivotPeriod` (intraday→daily),
`sessionStartFlags` via `sessionStartIndices` (threshold `max(4*gap, 4h)`,
daily-cadence gate, IST-calendar fallback) for the daily frame, and
`calendarPeriodFlags` with `istWeek`/IST-year-month boundaries for
weekly/monthly. Frame state: extremes accumulate within a period and are handed
over whole at the boundary (first period null). Levels:

```
p      = (prevHigh + prevLow + prevClose) / 3
width  = prevHigh - prevLow
S1     = 2p - prevHigh        R1 = 2p - prevLow
S2     = p - width            R2 = p + width
S3     = S1 - width           R3 = R1 + width
Bc     = (prevHigh + prevLow) / 2
Tc     = 2p - Bc
```

S1/R1 are null unless `display_s1r1` (the published script's plot calls are
commented out). All 7 daily level values reproduced to the exact golden bit
(e.g. `dPivot=106.72403333333334`, `dS2=87.91553333333334`, `dBc=109.25835000000001`,
`dTc=104.18971666666667`, all starting at index 240 / 60 values).

**range-analysis** (`studies.ts RANGE_ANALYSIS`) — `range = high - low`;
`avg_range = sma(range, avgLength)` only when `show_average` is true (else all
null). First index for avgRange: `avgLength - 1`.

**vortex** (`signals.ts VORTEX`) — bar-boundary movement terms
`upTerm=|H[i]-L[i-1]|`, `downTerm=|L[i]-H[i-1]|` (bar 0 has no term; rolling
sums shifted forward one bar, so the first value lands at index `length`):

```
vip[i] = rollingSum(upTerm, length)[i-1] / rollingSum(trueRange, length)[i]
vim[i] = rollingSum(downTerm, length)[i-1] / rollingSum(trueRange, length)[i]
```

**relative-vigor-index** (`ranges.ts RELATIVE_VIGOR_INDEX`) — `swma` (1/2/2/1
over 6) of `close-open` and of `high-low`, each `fromFirstValue`-sliced then
`rollingSum(length)`; `rvgi = num/den` (den==0 → null); `signal = swma(rvgi)`;
`offset` folded into both columns. First rvgi at index `length+2` (=12), signal
at 15 — matches the golden.

**relative-volatility-index** (`ranges.ts RELATIVE_VOLATILITY_INDEX`) — `length`
is the stdev window only; the up/down EMA is the hard-coded 14. Population
stdev `sd = stdev(close, length)` (calc.ts `stdev`: means from NaN-strict sma,
`sqrt(Σd²/period)`); `delta = change(close,1)`:

```
upSource   = (delta finite && delta <= 0) ? 0 : sd
downSource = (delta finite && delta >  0) ? 0 : sd
upper = seededEma(upSource, 14)   lower = seededEma(downSource, 14)
rvi   = total == 0 ? null : (upper / (upper+lower)) * 100
```

`seededEma` mirrors ranges.ts `seededEma` (NaN-strict SMA seed, recurse only
over finite, re-seed on a hole) — first RVI at index `length+12` (=22) on a
one-way market, exactly the golden. `ma` follows `ma_type`
(None/SMA/EMA/SMMA/WMA/VWMA via `fromFirstValue`); Bollinger bands
`ma ± bb_mult * stdev` exist only for `SMA + Bollinger Bands` (all null in the
golden). Only `rvi` is shifted by `offset`.

## Concerns

1. **`test_golden_parity.py` skips these 5 until Task 5.** The mission command
   reports `skipped`, not `passed`, because `PARAM_MAP`/`PLOT_MAP` have no
   entries yet (that is Task 5's explicit job). Bit-exactness was proven with a
   throwaway script implementing the harness's exact comparison. Task 5 must
   add the mappings below; I chose snake_case params so the mapping is a pure
   rename of the TS settings keys.

   - PARAM_MAP additions (TS key → backend param):
     - `cpr`: `pivotMode→pivot_mode`, `showDaily→show_daily`,
       `showWeekly→show_weekly`, `showMonthly→show_monthly`,
       `displaypivots→display_pivots`, `displaysupport→display_support`,
       `displayresistance→display_resistance`, `displaycpr→display_cpr`,
       `displayS1R1→display_s1r1`
     - `range-analysis`: `showAverage→show_average`, `avgLength→avg_length`
     - `vortex`: `length→length`
     - `relative-vigor-index`: `length→length`, `offset→offset`
     - `relative-volatility-index`: `length→length`, `offset→offset`,
       `maType→ma_type`, `maLength→ma_length`, `bbMult→bb_mult`
   - PLOT_MAP additions: `range-analysis`: `avg_range→avgRange`;
     `relative-volatility-index`: `bb_upper→bbUpper`, `bb_lower→bbLower`.
     All other plot keys are identity with the goldens (cpr's 27 keys match
     `cpr.json` exactly).

2. **Task brief diverges from the actual TS source on CPR.** The brief's
   "classic vs trad / floor/pivot/ceiling / 3-level TC/BC variants" and the
   params `pivot_source`/`pivot_length`/`pivot_periods` do **not** exist in the
   reference `studies.ts` CPR. The real descriptor is the floor-pivot
   `(H+L+C)/3` with auto/manual mode, d/w/m frames and the 9 plot toggles; the
   golden matches that. I followed the golden + TS (ground truth) and mirrored
   the actual TS settings in snake_case.

3. **Brief defaults differ from TS/golden defaults** for `range-analysis`
   (`show_average=True, avg_length=14` in the brief vs `showAverage=false,
   avgLength=3` in both TS and the golden) and `relative-volatility-index`
   (`length=14` in the brief vs `10` in TS and the golden). I used the TS/golden
   defaults (the golden test always passes explicit params from
   `{id}.json`, so defaults only affect the catalogue). `relative-volatility-index`
   `length` default is 10, matching TS.

4. **CPR needs timestamps**, which the harness candles carry as tz-naive IST
   datetimes (`c.timestamp`); `_bar_time` recovers the exact UTC epoch (IST is
   a fixed offset) and dict candles are supported via `time`/`timestamp` keys.

5. **Helpers reused, not redefined:** `sma`, `wma`, `_change`, `_rolling_sum`,
   `_to_float` from `indicators.py`; `_sma_skip_none` from `volume_flow.py`;
   `_rma`, `_vwma`, `_src_val` from `studies_simple.py`. New local helpers
   mirror calc.ts/ranges.ts primitives not present in the shared set:
   `_stdev_ts`, `_swma`, `_seeded_ema`, `_from_first_value`, `_shifted`,
   `_true_range_series`, and the feed/time.ts session/calendar helpers.