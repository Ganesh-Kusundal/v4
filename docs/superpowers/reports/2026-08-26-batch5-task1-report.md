# Batch 5 Task 1 Report — Simple Studies (momentum, ma-cross, ma-ribbon, woodies-cci, special-k)

**Date:** 2026-08-26
**Status:** DONE

## Deliverable

- Module: `trading/src/tradex_trading/analytics/studies_simple.py`
- 5 indicator functions (`momentum`, `ma_cross`, `ma_ribbon`, `woodies_cci`, `special_k`), 5 `IndicatorSpec` objects (`SPEC_MOMENTUM`, `SPEC_MA_CROSS`, `SPEC_MA_RIBBON`, `SPEC_WOODIES_CCI`, `SPEC_SPECIAL_K`), plus a `SPECS` list as the plan/mission request.
- No existing files modified. No commit made.

## Verification

**Official gate slice** (command as given in the brief):

```
../.venv/bin/python -m pytest trading/tests/analytics/test_golden_parity.py -k "momentum or ma_cross or ma_ribbon or woodies_cci or special_k" -v
```

Result: `1 passed, 1 skipped, 89 deselected`. The only relevant test — `test_parity_against_ts_source[momentum]` — **skips** ("not yet ported") because the ids are not registered and not in `PARAM_MAP` yet. Per the plan, Task 5 (merge wiring) imports the `SPEC_*` into `indicators.py` and extends `PARAM_MAP`/`PLOT_MAP`; the brief forbids editing those files in this task. So the official gate cannot exercise these 5 ids until Task 5 lands.

**Exact parity harness** (mirrors the gate's comparison logic: 1e-9 `pytest.approx`, None-aligned, using the anticipated Task-5 `PARAM_MAP`/plot identities, over the 300-bar `fixtures.json` candles):

```
  OK momentum.mom:        worst err 0.000e+00
  OK ma-cross.short:      worst err 0.000e+00
  OK ma-cross.long:       worst err 0.000e+00
  OK ma-cross.cross:      worst err 0.000e+00
  OK ma-ribbon.ma1..ma4:  worst err 0.000e+00
  OK woodies-cci.hist/turbo/cci14: worst err 0.000e+00
  OK special-k.specialK/signal:    worst err 0.000e+00
ALL OK
```

All 13 plots across the 5 indicators match the goldens **bit-exactly** (worst error `0.0`, i.e. every double identical — well inside 1e-9).

Full gate re-run: `71 passed, 20 skipped` (baseline, unchanged — no regressions).

## Formulas implemented (TS line → Python)

### momentum (strength.ts `MOMENTUM.calc`)
TS: `mom: nulls(change(sourceValues(bars, src(s)), len(s, 'len', 10)))`
- `strength.ts:97` → `_change(closes, len)` (`indicators.py:508`), which is `close[i] - close[i-len]`, None-padded for the first `len` bars. Confirmed against the golden: first value at index 10.

### ma-cross (averages.ts `MA_CROSS.calc`)
- `averages.ts:150-151` → `sma(closes, short_length)`, `sma(closes, long_length)` (reused `indicators.sma`, which reproduces the TS two-step running sum).
- `averages.ts:152` + `crossings` (averages.ts:106-119) → `cross` is `null` except on bars where `(curS > curL && prevS <= prevL) || (curS < curL && prevS >= prevL)` with both sides finite on both bars; the value carried is the **short** MA at that bar (`averages.ts:156` `short.map((v,i) => hit[i] ? v : null)`).

### ma-ribbon (averages.ts `MA_RIBBON.calc`)
- `averages.ts:344-350` → per-lane `sourceValues` then `movingAverage(type, values, vols, length)`, the kernel switch at `averages.ts:276-290`:
  - `EMA` → `_sma_seeded_ema` (`indicators.py:202`)
  - `SMMA (RMA)` → local `_rma` (calc.ts `rma`, Wilder)
  - `WMA` → `indicators.wma`
  - `VWMA` → local `_vwma` (calc.ts `vwma` = `sma(src*vol)/sma(vol)`, `nz(volume)`)
  - default/`SMA` → `indicators.sma`
  - Types normalized with `.upper()` so the golden's `"SMA"` and the lowercase defaults both work.
- Sources handled via `_source_values` (`open/high/low/close/hl2/hlc3/ohlc4/hlcc4`).
- The reference `showMaN` display guard is not exposed (all lanes always on; the golden settings have all `showMa* = true`).
- All four lanes verified against the golden at lengths 20/50/100/200.

### woodies-cci (ranges.ts `WOODIES_CCI.calc`)
- `ranges.ts:526-531` → `turbo = _cci(closes, cci_turbo_length)`, `slow = _cci(closes, cci14_length)`, `hist = cci14 = slow`.
- `_cci` mirrors calc.ts `cci` (`calc.ts:427-434`) over **close** (not typical price — the momentum.ts CCI is a different indicator): `(src - sma) / (0.015 * dev)` where `dev` = mean **absolute** deviation from the SMA; `md == 0` → gap (`None`), not zero.
- Histogram colours in the TS `colorBy` are a rendering concern and carry no data, so they are not part of the computation.

### special-k (ranges.ts `SPECIAL_K.calc`)
- `ranges.ts:592-597` → for each of the 10 Pring terms `(weight, roc, smooth)` from `SPECIAL_K_TERMS` (`ranges.ts:549-560`): `smoothed = sma(roc(source, rocLen), smooth)` with calc.ts NaN semantics, then `out[i] += weight * smoothed[i]` with NaN propagation (a None in any term blanks the running total permanently). Implemented with a TS-exact local `_roc_ts` (`calc.ts:162-171`, `(100*(src-src[n]))/src[n]`, NaN on zero base) and `_sma_skip_none` (calc.ts `sma` NaN-counting, imported from `volume_flow.py`) — `fromFirstValue(...)` stacking (ranges.ts:593,598-599) reduces to NaN-strict SMA.
- `ranges.ts:598-599` → `once = sma(out, length1)`, `signal = sma(once, length2)`.
- On 300 candles the slowest term needs `195+130 = 324` bars, so the golden `specialK`/`signal` are all-`null`; the implementation reproduces that and was additionally exercised on a 400-bar flat series (first print at index 324, matching the reference warmup).

## Edge cases exercised

- Empty candle list → all plots `[]`.
- Single candle → all plots `[None]`.
- Flat 400-bar series → special-k warmup math holds (first finite at 324).
- Dict candles and Candle-object candles both accepted (`_src_val`/`_vol` helpers branch on `isinstance(c, dict)`), matching both the brief's `list[dict]` signature convention and the gate's `_Candle` objects.
- `py_compile` clean.

## Concerns

1. **Gate cannot run these ids until Task 5** — the official slice reports them as *skipped*, not passed, because registration + `PARAM_MAP`/`PLOT_MAP` extension is explicitly owned by Task 5 and the brief forbids editing `indicators.py` / `test_golden_parity.py`. The parity harness above is the verification for this task; after Task 5 lands, the gate slice should go from skipped to green with no code changes expected.
2. **Default length mismatch vs TS.** The mission/plan tables specify backend defaults `ma-cross short_length=10/long_length=20`, `ma-ribbon ma{1..4}_length=10/20/30/40`, `woodies-cci cci_turbo_length=14`, `special-k length1=10/length2=10`. The TS descriptor defaults differ (`averages.ts:131-132` 9/21, ribbon 20/50/100/200 at averages.ts:308-313, `ranges.ts:487` turbo 6, `ranges.ts:579-580` length1/2=100). I used the mission-specified defaults in the `IndicatorSpec.params`; the goldens are unaffected because the gate passes explicit settings from `PARAM_MAP`. Flagging so Task 5 can reconcile UI-facing defaults if the TS values are preferred.
3. **SPEC shape.** The brief's "SPEC dict format" sketch (plain dicts with `'function'`/`'params'`/`'plots'`) conflicts with the instruction to "follow the exact SPEC structure used in existing modules". I used the `IndicatorSpec` dataclass (as `volume_flow.py` and every other batch module does) so Task 5's merge can `register_indicator(spec)` directly with no conversion shim. `SPECS` is exported as requested.