# Batch 5 — Task 4: Complex studies (`studies_signals.py`)

**Status: DONE_WITH_CONCERNS**

## Deliverable

`trading/src/tradex_trading/analytics/studies_signals.py` — five indicators,
each verified bit-exact against the compiled-bundle goldens (1e-9, None-aligned):

| id | TS source | plots returned |
|---|---|---|
| `rsi-divergence` | `signals.ts` `RSI_DIVERGENCE` | `rsi` (+ marker payloads `bull`/`hiddenBull`/`bear`/`hiddenBear`) |
| `trend-strength-index` | `signals.ts` `TREND_STRENGTH_INDEX` | `tsi` |
| `williams-fractals` | `signals.ts` `WILLIAMS_FRACTALS` | `fractals` (+ marker payloads `upFractal`/`downFractal`) |
| `williams-vix-fix` | `momentum.ts` `WILLIAMS_VIX_FIX` | `wvf`, `range_high`, `range_low`, `upper_band` |
| `wavetrend` | `wavetrend.ts` `WAVETREND` | `mom`, `wt1`, `wt2` |

`SPECS = [SPEC_RSI_DIVERGENCE, SPEC_TREND_STRENGTH_INDEX, SPEC_WILLIAMS_FRACTALS, SPEC_WILLIAMS_VIX_FIX, SPEC_WAVETREND]`
is defined but **not registered** — Task 5 (merge wiring) owns registration.

## Verification

### Exact command from the brief

```
../.venv/bin/python -m pytest trading/tests/analytics/test_golden_parity.py -k "rsi_divergence or trend_strength_index or williams_fractals or williams_vix_fix or wavetrend" -v
```

Output: `1 skipped, 90 deselected` — the node ids are the golden *hyphenated*
stems, so the underscore `-k` expression only matches `wavetrend`; and because
`test_golden_parity.py`'s `PARAM_MAP`/`PLOT_MAP` do not yet carry these ids
(Task 5 extends the gate file), that one test is **SKIPPED**, not run.

With hyphenated ids the same command selects all 5 and still **SKIPPED** each:
`5 skipped, 86 deselected` (reason: `param mapping not yet defined (later batch)`).

### Real parity proof (gate replica)

Because the gate file must not be edited (Task 5 extends it), parity was proven
with a standalone replica of `test_golden_parity.py`'s comparison logic using
the exact PARAM_MAP/PLOT_MAP entries Task 5 will add:

```
OK rsi-divergence.rsi: n=300 worst_diff=0.000e+00
OK trend-strength-index.tsi: n=300 worst_diff=0.000e+00
OK williams-fractals.fractals: n=300 worst_diff=0.000e+00
OK williams-vix-fix.wvf: n=300 worst_diff=0.000e+00
OK williams-vix-fix.range_high: n=300 worst_diff=0.000e+00
OK williams-vix-fix.range_low: n=300 worst_diff=0.000e+00
OK williams-vix-fix.upper_band: n=300 worst_diff=0.000e+00
OK wavetrend.mom: n=300 worst_diff=2.842e-14
OK wavetrend.wt1: n=300 worst_diff=1.421e-14
OK wavetrend.wt2: n=300 worst_diff=2.842e-14
ALL PASS
```

All within 1e-9; the wavetrend residuals are ~1e-14 (IEEE double noise, the
chained-smoother recompute path), far inside tolerance.

Full suite regression check: `71 passed, 20 skipped in 0.25s`. `ruff check`
on the new module: clean.

## Formulas implemented

Shared primitives mirror the reference `calc.ts` exactly (running-sum
NaN-counting `sma` via `_sma_skip_none`; `_sma_of_gapped` = `fromFirstValue`
+sma; population `stdev`; `highest`/`lowest` that skip non-finite; strict
`pivotHigh`/`pivotLow`; `barsSince`; `valueWhen`; Pearson `correlation` with
the `period*sab - sa*sb` covariance and NaN-on-zero/negative-variance
semantics). `sma`, `ema`, `rsi`, `_sma_seeded_ema`, `_ema_of_gapped`,
`_to_float` come from `indicators.py`.

- **rsi-divergence**: `osc = rsi(close, length)` (base-bundle Wilder RSI);
  `plFound/phFound` = pivot extrema on `osc` with `(lb_l, lb_r)`. Signals use
  `shift(osc/lows/highs, lb_r)`, `barsSince(shiftFlags(found,1))` range gate
  (5–60), and `valueWhen(found, …, 1)` for the previous pivot's oscillator and
  price. Regular bull: osc higher low + lower price low; regular bear: osc
  lower high + higher price high (hidden classes off, matching TS defaults).
- **trend-strength-index**: `tsi = correlation(close, [0..n-1], length)`;
  flat windows (zero variance) are None, exactly where the golden nulls.
- **williams-fractals**: the five-variant `isFractal` test — strict on the
  newer side (`periods` strictly-lower bars after), and an OR of five older
  sides tolerating 0–4 bars that merely equal the candidate before the strict
  run. `fractals` column is all-null (marker-layer owner); `upFractal`/
  `downFractal` carry the anchored price on the candidate bar.
- **williams-vix-fix**: `wvf = (highest(close,pd) − low)/highest(close,pd) × 100`;
  `upper_band = sma(wvf,bbl) + mult·stdev(wvf,bbl)`; `range_high = highest(wvf,lb)·ph`;
  `range_low = lowest(wvf,lb)·pl`. Show toggles (`hp`/`sd`) are off in the
  golden, so the range/band columns are all-null exactly as expected.
- **wavetrend**: `ap = (h+l+c)/3`; `esa = smaSeededEma(ap,n1)`;
  `absDev = smaSeededEma(|ap−esa|,n1)`; `ci = (ap−esa)/(0.015·absDev)` (0 when
  `absDev==0`, None while warming); `wt1 = smaSeededEma(ci,n2)`;
  `wt2 = sma(wt1,sigLen)`; `mom = wt1 − wt2`. Each chained stage goes through
  `fromFirstValue`; warmup lands exactly as the source promises (wt1 @ 38,
  wt2/mom @ 41) and as the golden shows.

## Concerns

1. **Brief's `williams-vix-fix` defaults disagree with TS and golden.** The
   table lists `lb=1, ph=85, pl=15`; both `momentum.ts` and the golden use
   `lb=50, ph=0.85, pl=1.01`. I implemented the TS/golden values (goldens are
   ground truth). Task 5's PARAM_MAP must pass golden settings explicitly, so
   this only affects default-invocation, but it is a real discrepancy.
2. **`williams-vix-fix` plot keys are snake_case** (`range_high`,
   `range_low`, `upper_band`) per the brief's table, while the TS goldens are
   `rangeHigh`/`rangeLow`/`upperBand`. Task 5's PLOT_MAP must map them
   explicitly (`{"wvf":"wvf","range_high":"rangeHigh",…}`).
3. **Task 5 must add explicit PLOT_MAP entries for `rsi-divergence` and
   `williams-fractals`** (not rely on the identity fallback): the functions
   return TS marker payloads (`bull`/`hiddenBull`/`bear`/`hiddenBear`;
   `upFractal`/`downFractal`) beyond the plotted columns, and the identity
   fallback would KeyError against the goldens.
4. **`williams-vix-fix` show toggles (`hp`, `sd`) are not backend params** per
   the brief's signature, so `range_high`/`range_low`/`upper_band` are always
   null — correct for the golden (both toggles off) but a fidelity limit for
   future users.
5. **The brief's `-k` string can't select the hyphenated test ids.** As
   written it matches only `wavetrend`, and even then the test skips until
   Task 5 extends `PARAM_MAP`. Parity evidence above comes from the gate
   replica, which is exact up to the (immutable) PARAM_MAP/PLOT_MAP additions.