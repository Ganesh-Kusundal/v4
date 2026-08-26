# Indicator Port Batch 2 — Oscillators/Momentum Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port ~20 oscillator/momentum indicators to the backend registry, each bit-exact against the openalgo-charts TS source via the existing golden gate.

**Architecture:** New module files under `trading/src/tradex_trading/analytics/` (parallel-split pattern from Batch 1) so 3–4 tasks can run concurrently. Goldens already exist under `trading/tests/analytics/goldens/`. A final merge task wires SPEC_* objects into `indicators.py` and extends PARAM_MAP/PLOT_MAP.

**Tech Stack:** Python stdlib; pytest via parent venv `../.venv/bin/python -m pytest`.

## Global Constraints

- Reference TS root: `/Users/apple/Downloads/openalgo-charts-master/src/indicators/` — READ the actual calc for every indicator.
- Golden gate: `trading/tests/analytics/test_golden_parity.py` passes at 1e-9, None-aligned. Goldens are ground truth — never edit them.
- Backend params snake_case; defaults from TS inputs; fail-loud ValueError on non-positive periods.
- Reuse helpers from `indicators.py` / `ma_vol.py` / `band_overlays.py` (sma, ema, wma, rma, _sma_seeded_ema, _ema_of_gapped, atr) — never redefine.
- Unrelated uncommitted datalake changes exist — commit explicit paths only.
- Baseline before Batch 2: gate 25 passed / 66 skipped; suite ~190 passed / 66 skipped. After: +~20 passed.

## Batch 2 indicator set (20 ids)

| id | TS plots | key inputs | source file |
|---|---|---|---|
| adx | plusDi,minusDi,adx | period=14 adxPeriod=14 | momentum.ts ADX |
| cci | cci,ma,bbUpper,bbLower | period=20 constant=0.015 | momentum.ts CCI |
| mfi | mfi | period=14 | momentum.ts MFI |
| stochastic-rsi | k,d | smoothK=3 smoothD=3 lengthRSI=14 lengthStoch=14 | ranges.ts |
| williams-percent-r | percentR | length=14 | ranges.ts |
| ultimate-oscillator | uo | length1=7 length2=14 length3=28 | ranges.ts |
| ppo | hist,ppo,signal | fastLength=12 slowLength=26 signalLength=9 | strength.ts |
| trix | trix | length=18 | strength.ts |
| tsi | tsi,signal | long=25 short=13 signal=13 | strength.ts |
| smi | smi,ema | lengthK=10 lengthD=3 lengthEMA=3 | strength.ts |
| smi-ergodic-indicator | erg,sig | longlen=20 shortlen=5 siglen=5 | strength.ts |
| smi-ergodic-oscillator | osc | longlen=20 shortlen=5 siglen=5 | strength.ts |
| aroon | up,down | length=14 | oscillators.ts |
| aroon-oscillator | osc | length=14 | oscillators.ts |
| awesome-oscillator | ao | (none) | oscillators.ts |
| chande-momentum | cmo | length=9 | oscillators.ts |
| coppock-curve | curve | wmaLength=10 longRoC=14 shortRoC=11 | oscillators.ts |
| dpo | dpo | period=21 | oscillators.ts |
| fisher-transform | fisher,trigger | length=9 | oscillators.ts |
| connors-rsi | crsi | lenrsi=3 lenupdown=2 lenroc=100 | oscillators.ts |

---

### Task 1: ADX/DMI, Aroon pair, Awesome Oscillator, CCI

**Files:** `trading/src/tradex_trading/analytics/oscillators_trend.py` + tests

- [ ] **Step 1: Read TS sources** — ADX in momentum.ts, Aroon/Aroon-Osc/AO in oscillators.ts, CCI in momentum.ts. ADX uses Wilder RMA over TR/+DM/−DM; AO = SMA(hl2,5)−SMA(hl2,34); CCI = (TP−SMA(TP))/ (0.015*meanDev) with optional smoothing block (only base cci required for golden).
- [ ] **Step 2: Implement** — `adx(candles, period, adx_period) -> dict{plusDi,minusDi,adx}`, `aroon(candles, length) -> dict{up,down}`, `aroon_oscillator(candles, length) -> {osc}`, `awesome_oscillator(candles) -> {ao}`, `cci(candles, period) -> {cci}`. Reuse rma/sma.
- [ ] **Step 3: Specs** — ids `adx`, `aroon`, `aroon-oscillator`, `awesome-oscillator`, `cci`; params as table; plots mapped to TS keys; categories Momentum/Trend per TS.
- [ ] **Step 4: Edge tests** — ADX flat series → all 0/None; AO definition check; CCI on constant TP → 0.
- [ ] **Step 5: Verify** — `../.venv/bin/python -m pytest trading/tests/analytics/test_golden_parity.py -k "adx or aroon or awesome or cci" -q` green; full analytics suite green.

---

### Task 2: MFI, PPO, TRIX, TSI/SMI family

**Files:** `trading/src/tradex_trading/analytics/oscillators_strength.py` + tests

- [ ] **Step 1: Read TS sources** — MFI in momentum.ts, PPO/TRIX/TSI/SMI in strength.ts. PPO = 100*(fastMA−slowMA)/slowMA + signal; TSI = 100*doubleSmooth(delta)/doubleSmooth(|delta|); SMI = double-EMA momentum.
- [ ] **Step 2: Implement** — `mfi`, `ppo`, `trix`, `tsi`, `smi`, `smi_ergodic_indicator`, `smi_ergodic_oscillator`. Chain EMAs via _sma_seeded_ema / _ema_of_gapped where TS uses them.
- [ ] **Step 3: Specs** — ids `mfi`, `ppo`, `trix`, `tsi`, `smi`, `smi-ergodic-indicator`, `smi-ergodic-oscillator`.
- [ ] **Step 4: Edge tests** — MFI constant price→50-ish; PPO proportionality check; TSI/SMI boundedness.
- [ ] **Step 5: Verify** — gate slice green.

---

### Task 3: StochRSI, Williams %R, Ultimate, Coppock, DPO

**Files:** `trading/src/tradex_trading/analytics/oscillators_range_a.py` + tests

- [ ] **Step 1: Read TS sources** — all in ranges.ts / oscillators.ts. StochRSI = stochastic of RSI; Williams %R = −100*(HH−close)/(HH−LL); Ultimate = weighted true-range ratio; Coppock = WMA(ROC14+ROC11); DPO = close − SMA shifted.
- [ ] **Step 2: Implement** — `stochastic_rsi`, `williams_percent_r`, `ultimate_oscillator`, `coppock_curve`, `dpo`.
- [ ] **Step 3: Specs** — ids `stochastic-rsi`, `williams-percent-r`, `ultimate-oscillator`, `coppock-curve`, `dpo`.
- [ ] **Step 4: Edge tests** — flat StochRSI; Williams %R bounds −100..0.
- [ ] **Step 5: Verify** — gate slice green.

---

### Task 4: Fisher, CMO, Connors RSI, Chande Momentum

**Files:** `trading/src/tradex_trading/analytics/oscillators_range_b.py` + tests

- [ ] **Step 1: Read TS sources** — Fisher in oscillators.ts, CMO in oscillators.ts, Connors in oscillators.ts. Fisher: recursive normalized position → 0.5*ln((1+v)/(1−v))+0.5*prev; CMO = 100*(sumUp−sumDown)/(sumUp+sumDown); Connors = mean(RSI(close,3), RSI(streak,2), percentRank(ROC1,100)).
- [ ] **Step 2: Implement** — `fisher_transform`, `chande_momentum`, `connors_rsi`.
- [ ] **Step 3: Specs** — ids `fisher-transform`, `chande-momentum`, `connors-rsi` (+ `balance-of-power` if trivially free).
- [ ] **Step 4: Edge tests** — Fisher trigger = fisher[1]; Connors first print at bar 101.
- [ ] **Step 5: Verify** — gate slice green.

---

### Task 5: Merge wiring + close-out

**Files:** `trading/src/tradex_trading/analytics/indicators.py`, `trading/tests/analytics/test_golden_parity.py`

- [ ] **Step 1:** Import all SPEC_* from the 4 new modules into indicators.py tail and register them.
- [ ] **Step 2:** Extend PARAM_MAP/PLOT_MAP for all 20 ids (verify keys against goldens).
- [ ] **Step 3:** `../.venv/bin/python -m pytest trading/tests/analytics/test_golden_parity.py -q` → expect 45 passed / 46 skipped (25+20). Full analytics suite green.
- [ ] **Step 4:** Commit wiring.
