# Indicator Port Batch 5 — Complex Studies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port 20 complex-study indicators to the backend registry, each bit-exact against the openalgo-charts TS source via the existing golden gate.

**Architecture:** New module files under `trading/src/tradex_trading/analytics/` (parallel-split pattern) so 4 tasks run concurrently. Goldens already exist. Merge task wires SPEC_* into `indicators.py` and extends PARAM_MAP/PLOT_MAP.

**Tech Stack:** Python stdlib; pytest via parent venv `../.venv/bin/python -m pytest`.

## Global Constraints

- Reference TS root: `/Users/apple/Downloads/openalgo-charts-master/src/indicators/` — READ actual calc per indicator.
- Golden gate: `trading/tests/analytics/test_golden_parity.py` at 1e-9, None-aligned. Goldens are ground truth.
- Backend params snake_case; defaults from TS inputs; fail-loud ValueError.
- Reuse helpers (sma, ema, atr, true_ranges, _to_float, _highest, _rolling_sum, _cumulative, _change, roc, _sma_seeded_ema, _ema_of_gapped) — never redefine.
- Baseline: gate 71 passed / 20 skipped; suite 271 passed. After: +20 passed.

## Batch 5 set (20 ids)

| id | TS source | inputs (key defaults) | plots |
|---|---|---|---|
| momentum | strength.ts | len=10 | mom |
| ma-cross | averages.ts | shortLength=10 longLength=20 | short, long, cross |
| ma-ribbon | averages.ts | ma1Type/Source/Length etc | ma1..ma4 |
| woodies-cci | ranges.ts | cciTurboLength=14 cci14Length=14 | hist, turbo, cci14 |
| special-k | ranges.ts | length1=10 length2=10 | specialK, signal |
| alligator | averages.ts | jawLength=13 teethLength=8 lipsLength=5 + offsets | jaw, teeth, lips |
| parabolic-sar | trend.ts | start=0.02 increment=0.02 maximum=0.2 | sar |
| ichimoku | trend.ts | conversion=9 base=26 lagging=52 displacement=26 | conversion, base, spanA, spanB, lagging |
| halftrend | trend.ts | amplitude=3 channelDeviation=1 atrPeriod=10 | up, down, atrHigh, atrLow, buySignal, sellSignal |
| alphatrend | studies.ts | coeff=1 AP=14 | alphatrend, lagged |
| cpr | studies.ts | pivotMode pivot settings | dPivot..mTc (27 plots) |
| range-analysis | studies.ts | showAverage avgLength=14 | range, avgRange |
| vortex | signals.ts | length=14 | vip, vim |
| relative-vigor-index | ranges.ts | length=10 offset=0 | rvgi, signal |
| relative-volatility-index | ranges.ts | length=14 offset=0 maType/Length bbMult | rvi, ma, bbUpper, bbLower |
| rsi-divergence | signals.ts | length=14 lbR=5 lbL=5 | rsi |
| trend-strength-index | signals.ts | length=14 | tsi |
| williams-fractals | signals.ts | periods=2 | fractals |
| williams-vix-fix | momentum.ts | pd=22 bbl=20 mult=2 lb=1 ph=85 pl=15 | wvf, rangeHigh, rangeLow, upperBand |
| wavetrend | wavetrend.ts | n1=10 n2=21 sigLen=4 | mom, wt1, wt2 |

---

### Task 1: Simple studies (momentum, ma-cross, ma-ribbon, woodies-cci, special-k)

**Files:** `trading/src/tradex_trading/analytics/studies_simple.py`

- [x] **Step 1: Read TS sources** — `src/indicators/strength.ts` (MOMENTUM), `src/indicators/averages.ts` (MA_CROSS, MA_RIBBON), `src/indicators/ranges.ts` (WOODIES_CCI, SPECIAL_K).
- [x] **Step 2: Implement** — `momentum`, `ma_cross`, `ma_ribbon`, `woodies_cci`, `special_k` + SPEC_*.
- [x] **Step 3: Edge tests**.
- [x] **Step 4: Verify** — gate slice green.

### Task 2: Trend overlays (alligator, parabolic-sar, ichimoku, halftrend, alphatrend)

**Files:** `trading/src/tradex_trading/analytics/studies_trend.py`

- [x] **Step 1: Read TS sources** — `src/indicators/averages.ts` (ALLIGATOR), `src/indicators/trend.ts` (PARABOLIC_SAR, ICHIMOKU, HALFTREND), `src/indicators/studies.ts` (ALPHATREND).
- [x] **Step 2: Implement** — `alligator`, `parabolic_sar`, `ichimoku`, `halftrend`, `alphatrend` + SPEC_*.
- [x] **Step 3: Edge tests**.
- [x] **Step 4: Verify** — gate slice green.

### Task 3: Complex studies (cpr, range-analysis, vortex, RVGI, RVI)

**Files:** `trading/src/tradex_trading/analytics/studies_complex.py`

- [x] **Step 1: Read TS sources** — `src/indicators/studies.ts` (CPR, RANGE_ANALYSIS), `src/indicators/signals.ts` (VORTEX), `src/indicators/ranges.ts` (RELATIVE_VIGOR_INDEX, RELATIVE_VOLATILITY_INDEX).
- [x] **Step 2: Implement** — `cpr`, `range_analysis`, `vortex`, `relative_vigor_index`, `relative_volatility_index` + SPEC_*.
- [x] **Step 3: Edge tests**.
- [x] **Step 4: Verify** — gate slice green.

### Task 4: Signals/divergence (rsi-divergence, trend-strength, fractals, vix-fix, wavetrend)

**Files:** `trading/src/tradex_trading/analytics/studies_signals.py`

- [x] **Step 1: Read TS sources** — `src/indicators/signals.ts` (RSI_DIVERGENCE, TREND_STRENGTH_INDEX, WILLIAMS_FRACTALS), `src/indicators/momentum.ts` (WILLIAMS_VIX_FIX), `src/indicators/wavetrend.ts` (WAVETREND).
- [x] **Step 2: Implement** — `rsi_divergence`, `trend_strength_index`, `williams_fractals`, `williams_vix_fix`, `wavetrend` + SPEC_*.
- [x] **Step 3: Edge tests**.
- [x] **Step 4: Verify** — gate slice green.

### Task 5: Merge wiring + close-out

**Files:** `trading/src/tradex_trading/analytics/indicators.py`, `trading/tests/analytics/test_golden_parity.py`

- [x] **Step 1:** Import SPEC_* from 4 new modules into indicators.py tail and register.
- [x] **Step 2:** Extend PARAM_MAP/PLOT_MAP for all 20 ids.
- [x] **Step 3:** Gate → 91 passed / 0 skipped. Suite green.
- [x] **Step 4:** Commit wiring.
