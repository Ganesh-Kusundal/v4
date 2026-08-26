# Indicator Port Batch 3 — Volatility/Range Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port 11 volatility/range indicators to the backend registry, each bit-exact against the openalgo-charts TS source via the existing golden gate.

**Architecture:** New module files under `trading/src/tradex_trading/analytics/` (parallel-split pattern) so 3 tasks run concurrently. Goldens already exist. Merge task wires SPEC_* into `indicators.py` and extends PARAM_MAP/PLOT_MAP.

**Tech Stack:** Python stdlib; pytest via parent venv `../.venv/bin/python -m pytest`.

## Global Constraints

- Reference TS root: `/Users/apple/Downloads/openalgo-charts-master/src/indicators/` — READ actual calc per indicator.
- Golden gate: `trading/tests/analytics/test_golden_parity.py` at 1e-9, None-aligned. Goldens are ground truth.
- Backend params snake_case; defaults from TS inputs; fail-loud ValueError.
- Reuse helpers (sma, stdev if exists, atr, _rolling_sma, _sma_seeded_ema) — never redefine.
- Baseline before Batch 3: gate 46 passed / 45 skipped; suite 227 passed. After: +11 passed.

## Batch 3 set (11 ids)

| id | TS plots | inputs | notes |
|---|---|---|---|
| bollinger-percent-b | percentB | length=20 mult=2 | basis + (close−lower)/(upper−lower) |
| bollinger-bandwidth | bandwidth,expansion,contraction | length=20 mult=2 | (upper−lower)/basis |
| bb-trend | bbtrend | shortLength=20 longLength=50 stdDevMult=2 | (BB short−long)/mid |
| choppiness-index | chop | length=14 offset=0 | 100*log10(sumTR / (HH−LL))/log10(len) |
| historical-volatility | hv | length=10 per=1 | stdev of log returns |
| average-daily-range | adr | length=14 | SMA of daily range (use high−low SMA here; TS ADR is daily) |
| chop-zone | chopZone | (none or length) | color zone from choppiness |
| volatility-stop | up,down | length=20 factor=2 | ATR trailing stop |
| chandelier-exit | longExit,shortExit | length=22 atrLength=22 atrMultiplier=3 | highest high / lowest low ± ATR |
| chande-kroll-stop | stopLong,stopShort | p=10 x=1 q=9 | ATR stop variant |
| kama | kama | erLength=10 fastLength=2 slowLength=30 | Kaufman adaptive MA |

---

### Task 1: Bollinger family + KAMA

**Files:** `trading/src/tradex_trading/analytics/volatility_bands.py` + tests

- [ ] **Step 1: Read TS sources** — `src/indicators/adaptive.ts` (BB family: BB_TREND, BOLLINGER_BANDWIDTH, BOLLINGER_PERCENT_B, KAMA) and `calc.ts` (stdev/bb helpers).
- [ ] **Step 2: Implement** — `bollinger_percent_b`, `bollinger_bandwidth`, `bb_trend`, `kama` + SPEC_*.
- [ ] **Step 3: Edge tests** — flat series → %b = 0.5, bandwidth = 0; KAMA on flat = constant.
- [ ] **Step 4: Verify** — gate slice green.

### Task 2: Choppiness family + ADR

**Files:** `trading/src/tradex_trading/analytics/volatility_chop.py` + tests

- [ ] **Step 1: Read TS sources** — `src/indicators/adaptive.ts` (CHOPPINESS_INDEX, CHOP_ZONE, VOLATILITY_STOP) and `src/indicators/ranges.ts` (ADR, HV). CHOP = 100*log10(sumTR / range)/log10(len); HV = annualized stdev of log returns.
- [ ] **Step 2: Implement** — `choppiness_index`, `historical_volatility`, `average_daily_range`, `chop_zone` + SPEC_*.
- [ ] **Step 3: Edge tests** — CHOP trending → low, ranging → high; HV constant price → 0.
- [ ] **Step 4: Verify** — gate slice green.

### Task 3: ATR stops (Volatility Stop, Chandelier, Chande-Kroll)

**Files:** `trading/src/tradex_trading/analytics/volatility_stops.py` + tests

- [ ] **Step 1: Read TS sources** — `src/indicators/adaptive.ts` (VOLATILITY_STOP, CHANDELIER_EXIT, CHANDE_KROLL_STOP). All ATR-trailing stops: highest/lowest ± mult*ATR with ratcheting.
- [ ] **Step 2: Implement** — `volatility_stop`, `chandelier_exit`, `chande_kroll_stop` + SPEC_*.
- [ ] **Step 3: Edge tests** — flat ATR → bands flat and equal.
- [ ] **Step 4: Verify** — gate slice green.

### Task 4: Merge wiring + close-out

**Files:** `trading/src/tradex_trading/analytics/indicators.py`, `trading/tests/analytics/test_golden_parity.py`

- [ ] **Step 1:** Import SPEC_* from 3 new modules into indicators.py tail and register.
- [ ] **Step 2:** Extend PARAM_MAP/PLOT_MAP for all 11 ids (verify keys against goldens).
- [ ] **Step 3:** Gate → 57 passed / 34 skipped (46+11). Suite green.
- [ ] **Step 4:** Commit wiring.
