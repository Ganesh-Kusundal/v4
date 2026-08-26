# Batch 5 Task 2 — Complex trend studies (alligator, parabolic-sar, ichimoku, halftrend, alphatrend)

**Status:** DONE

## Files

- Module: `trading/src/tradex_trading/analytics/studies_trend.py`
- Report: `docs/superpowers/reports/2026-08-26-batch5-task2-report.md`

## Test results

The golden parity harness (`test_golden_parity.py`) currently reports the 5
new ids as **SKIPPED** — its `PARAM_MAP`/`PLOT_MAP` are wired by Task 5
(merge wiring), and the brief forbids editing the file in this task.

```bash
../.venv/bin/python -m pytest trading/tests/analytics/test_golden_parity.py \
  -k "alligator or parabolic-sar or ichimoku or halftrend or alphatrend" -v
# 5 skipped, 86 deselected in 0.42s   (all 5 SKIPPED — Task 5 wires the maps)
```

Full suite: `71 passed, 20 skipped` (no regressions; the 20 skips are the
unwired/unported ids). Lint (`ruff check`, repo config E/F/W/I/UP, line-length
100): clean.

### Bit-exact gate (verified independently)

Because the harness skips until Task 5, parity was verified with a standalone
script that replicates the harness `_compare` exactly (None-aligned, `abs=1e-9`)
against the goldens in `trading/tests/analytics/goldens/`, feeding the golden
`settings` mapped to the snake_case backend params, using the exact harness
candle objects (`_Candle` from `fixtures.json`). A second check drove the same
params through `compute_indicator` after registering the five SPECs, proving the
Task 5 wiring path will pass. **Every plot of all 5 indicators matched with a
worst diff of `0.0`** (bit-exact, not merely within 1e-9).

```
PASS alligator.jaw / .teeth / .lips        PASS parabolic-sar.sar
PASS ichimoku.conversion/.base/.spanA/.spanB/.lagging
PASS halftrend.up/.down/.atr_high/.atr_low/.buy_signal/.sell_signal
PASS alphatrend.alphatrend/.lagged
ALL PASS  (all worst diffs 0.00e+00)
```

## Formulas implemented (all ports of the TS `calc()`)

Candle access is dual-mode: the documented `list[dict]` contract and the harness
`.ohlc.*.value` objects both work (`_f`/`_vol`/`_extract`).

### alligator — `averages.ts` `ALLIGATOR.calc`
- Source `hl2 = (high+low)/2` (hard-coded in TS, no source setting).
- Each line = `rma(hl2, length)` shifted by its offset. `_rma` is Wilder's RMA
  (seed = SMA of first `length` values at index `length-1`, then
  `(prev*(length-1)+v)/length`) — exactly `calc.ts rma`, not a plain EMA.
- `_shift(values, k)` applies the plot offset: value on bar `i` lands in slot
  `i+k` (`out[i] = values[i-k]`), leaving `k` leading nulls and dropping the
  shifted tail. First print at `length-1+offset` (jaw 12+8=20, teeth 7+5=12,
  lips 4+3=7 — matches golden).

### parabolic-sar — `trend.ts` `PARABOLIC_SAR.calc`
- Seed: `rising = close[1] >= close[0]`; `sar = low[0]` (rising) or `high[0]`;
  `ep = high[1]` (rising) or `low[1]`; `af = start`.
- Per bar `i`: `sar += af*(ep-sar)`; clamp against prior two bars' range
  (`min` when rising against lows, `max` when falling against highs; bar 1
  reuses bar 0 for the second of the pair).
- Flip branches (`low[i] < sar` / `high[i] > sar`) reset `sar=ep, ep=extremum,
  af=start`; EP-update branches advance `ep` and `af = min(maximum, af+increment)`.
- Output None at index 0, first value at index 1 (golden confirms).

### ichimoku — `trend.ts` `ICHIMOKU.calc`
- Donchian midpoint `mid(p) = (max(high, p) + min(low, p))/2`, None before
  index `p-1`. `conversion = mid(9)`, `base = mid(26)`, `spanB = mid(52)`.
- `spanA = (conversion+base)/2` — None until BOTH are live (index 25), then
  displaced forward by `displacement=26` (first print 51). `spanB` displaced
  (first print 51+26=77). `lagging = shift(closes, -displacement)` — close
  shifted BACK 26 bars (first value at 0, trailing 26 nulls). Spans DO include
  the displacement; edges are null, not extrapolated.

### halftrend — `trend.ts` `HALFTREND.calc`
- `amp = max(1, round(amplitude))`; `meanHigh/meanLow = sma(h/l, amp)`;
  `rollHigh/rollLow = highest/lowest(h/l, amp)`; `halfAtr = atr(h,l,c, atr_period)`
  (Wilder, first at `atr_period-1`).
- Two state machines: `trend` (0 up / 1 down) and `armed` (the pending flip).
  Down-flip arms: track running `maxLow = max(rollLow, maxLow)`; fires when
  `meanHigh < maxLow && close < prevLow`, setting `trend=1, minHigh=rollHigh`.
  Up-flip is the mirror (`meanLow > minHigh && close > prevHigh`).
- Level stepping: on a flip the new level starts from the other side's level
  (`upLevel = downLevel` / `downLevel = upLevel`) and the flip bar gets a
  signal marker half an ATR inside the channel (`buy = upLevel - half`,
  `sell = downLevel + half`). Before any flip, level seeds from `maxLow`
  (up) / `minHigh` (down) on bar 0 (`wasTrend == -1`), then carries forward
  `max(maxLow, upLevel)` / `min(minHigh, downLevel)`.
- `atrHigh/atrLow = level ± chDev*half` (None until ATR warmup — golden first
  print at 99 with `atr_period=100`). `up`/`down` split the level, null on the
  inactive side. Golden confirms: `up` live from bar 0, `down` first at 81.

### alphatrend — `studies.ts` `ALPHATREND.calc`
- Gauge = `moneyFlowIndex(typical, volume, ap)`: typical `(h+l+c)/3`; flow
  signed by typical-price direction, rolling-summed over `ap`; first value at
  index `ap`; window with no down-flow pins at 100. (The `novolumedata`/RSI
  branch is not exercised by the default-settings golden; `ap` defaults to 14.)
- Band = plain SMA of true range over `ap` (`trueRange[0] = high-low`), NOT
  Wilder's ATR.
- Recursion reads its own previous value through `nz` (unresolved slot = 0):
  `level[i] = gauge >= 50 ? max(low - band*coeff, prev) : min(high + band*coeff, prev)`.
- `lagged[i] = level[i-2]` (None until 2 bars after the level starts).
- Crossovers (level vs lagged, both sides finite) drive `buy_signal/sell_signal`
  gated by the TS `barsSince` shifted-counter comparison (`sinceShiftedUp >
  sinceDown` etc.), which suppresses the very first signal. Returned for TS
  fidelity; the golden only asserts `alphatrend`/`lagged`.

## Conventions followed

- Module is a standalone parallel-split file (like `volume_flow.py`): imports
  shared helpers from `indicators.py` (`sma`, `_highest`, `_rolling_sum`,
  `_isfinite`, `_to_float`, `IndicatorSpec`), defines local-only helpers
  (`_rma`, `_lowest`, `_shift`, `_atr_arrays`, `_money_flow_index`,
  `_bars_since`), and exposes `SPEC_*`/`SPECS` WITHOUT registering them.
- SPEC structure matches `volume_flow.py` exactly (`IndicatorSpec` dataclass
  with `params`/`plots` tuples, `_fn_*` wrappers that cast int/float).
- Plot keys follow the task table (snake_case): `atr_high`, `atr_low`,
  `buy_signal`, `sell_signal` (TS golden keys `atrHigh`/`atrLow`/`buySignal`/
  `sellSignal` are for Task 5's `PLOT_MAP`).

## Concerns

1. **`-k` filter hyphen**: the brief's `-k "... parabolic_sar ..."` uses an
   underscore but the parametrize id is `parabolic-sar` (hyphen), so that one
   test is not selected. Run with `parabolic-sar` to include it.
2. **Harness skip state is expected**: the 5 tests show SKIPPED in the official
   pytest run until Task 5 adds `PARAM_MAP`/`PLOT_MAP`. The bit-exact gate was
   proven with an equivalent standalone harness replication (worst diff 0.0),
   including the full `compute_indicator` path.
3. **halftrend SPEC defaults** follow the brief table (`amplitude=3,
   channel_deviation=1, atr_period=10`), which differ from the TS input defaults
   (`2, 2, 100`) and from the golden's settings. The golden was generated with
   the golden's settings and verifies correctness at those settings; the SPEC
   defaults only define the chart menu default. No correctness impact.
4. **alphatrend signals** are computed and returned as extra keys
   (`buy_signal`/`sell_signal`) for TS fidelity; the SPEC plots list only
   `alphatrend`/`lagged` per the brief, so extra keys are inert for the gate.