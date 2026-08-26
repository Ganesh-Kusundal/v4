# Indicator Port Batch 4 — Volume/Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port 14 volume/flow indicators to the backend registry, each bit-exact against the openalgo-charts TS source via the existing golden gate.

**Architecture:** New module files under `trading/src/tradex_trading/analytics/` (parallel-split pattern) so 3 tasks run concurrently. Goldens already exist. Merge task wires SPEC_* into `indicators.py` and extends PARAM_MAP/PLOT_MAP.

**Tech Stack:** Python stdlib; pytest via parent venv `../.venv/bin/python -m pytest`.

## Global Constraints

- Reference TS root: `/Users/apple/Downloads/openalgo-charts-master/src/indicators/` — READ actual calc per indicator.
- Golden gate: `trading/tests/analytics/test_golden_parity.py` at 1e-9, None-aligned. Goldens are ground truth.
- Backend params snake_case; defaults from TS inputs; fail-loud ValueError.
- Reuse helpers (sma, ema, atr, true_ranges, _to_float) — never redefine.
- Baseline before Batch 4: gate 57 passed / 34 skipped; suite 250 passed. After: +14 passed.

## Batch 4 set (14 ids)

| id | TS source | inputs | plots |
|---|---|---|---|
| adl | volume.ts | (none) | adl |
| volume | volume.ts | (none) | volume |
| chaikin-money-flow | flow.ts | length=20 | cmf |
| chaikin-oscillator | flow.ts | short=3 long=10 | osc |
| ease-of-movement | flow.ts | length=14 divisor=1000000 | eom |
| elder-force-index | flow.ts | length=13 | efi |
| mass-index | indices.ts | length=9 | mi |
| nvi | indices.ts | maLength=55 | nvi, ema |
| pvi | indices.ts | maLength=55 | pvi, ema |
| pvo | indices.ts | fastLength=12 slowLength=26 signalLength=9 | hist, pvo, signal |
| pvt | indices.ts | (none) | pvt |
| ulcer-index | indices.ts | length=14 | ui |
| klinger-oscillator | adaptive.ts | (none or few) | kvo, signal |
| know-sure-thing | adaptive.ts | roclen1..4 smalen1..4 siglen=9 | kst, signal |

---

### Task 1: Simple volume + OBV-like (adl, volume, pvt)

**Files:** `trading/src/tradex_trading/analytics/volume_simple.py` + tests

- [ ] **Step 1: Read TS sources** — `src/indicators/volume.ts` (VOLUME, ADL) and `src/indicators/indices.ts` (PVT). VOLUME is raw volume; ADL is Chaikin A/D Line; PVT is price-volume trend.
- [ ] **Step 2: Implement** — `adl`, `volume_raw` (id="volume"), `pvt` + SPEC_*.
- [ ] **Step 3: Edge tests** — zero-volume bars.
- [ ] **Step 4: Verify** — gate slice green.

### Task 2: Chaikin flow family + Ulcer Index (CMF, Chaikin-Osc, EOM, EFI, UI)

**Files:** `trading/src/tradex_trading/analytics/volume_flow.py` + tests

- [ ] **Step 1: Read TS sources** — `src/indicators/flow.ts` (CMF, CHAIKIN_OSCILLATOR, EASE_OF_MOVEMENT, ELDER_FORCE_INDEX) and `src/indicators/indices.ts` (ULCER_INDEX).
- [ ] **Step 2: Implement** — `chaikin_money_flow`, `chaikin_oscillator`, `ease_of_movement`, `elder_force_index`, `ulcer_index` + SPEC_*.
- [ ] **Step 3: Edge tests** — zero-volume bars for CMF.
- [ ] **Step 4: Verify** — gate slice green.

### Task 3: Volume indices (NVI, PVI, PVO, Mass Index) + KST + Klinger

**Files:** `trading/src/tradex_trading/analytics/volume_indices.py` + tests

- [ ] **Step 1: Read TS sources** — `src/indicators/indices.ts` (NVI, PVI, PVO, MASS_INDEX, KNOW_SURE_THING) and `src/indicators/adaptive.ts` (KLINGER_OSCILLATOR). KST = weighted sum of 4 ROCs smoothed by SMAs. PVO = PPO on volume. NVI/PVI = +/- volume index.
- [ ] **Step 2: Implement** — `nvi`, `pvi`, `pvo`, `mass_index`, `know_sure_thing`, `klinger_oscillator` + SPEC_*.
- [ ] **Step 3: Edge tests** — zero-volume bars for NVI/PVI.
- [ ] **Step 4: Verify** — gate slice green.

### Task 4: Merge wiring + close-out

**Files:** `trading/src/tradex_trading/analytics/indicators.py`, `trading/tests/analytics/test_golden_parity.py`

- [ ] **Step 1:** Import SPEC_* from 3 new modules into indicators.py tail and register.
- [ ] **Step 2:** Extend PARAM_MAP/PLOT_MAP for all 14 ids (verify keys against goldens).
- [ ] **Step 3:** Gate → 71 passed / 20 skipped (57+14). Suite green.
- [ ] **Step 4:** Commit wiring.
