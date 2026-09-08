# Indicator Parity 2.1.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port 12 missing indicators to the backend and correct any drift in the existing 90, proven by a differential harness against openalgo-charts 2.1.0.

**Architecture:** A node dump script runs all 102 engine `calc`s on the shared `fixtures.json` bars; Python parity tests compare backend output 1:1 at 1e-9. Ports mirror engine ids, input keys, and plot keys.

**Tech Stack:** Python (tradex_trading analytics), Node 22 (engine bundle), pytest.

## Global Constraints

- Engine version pinned to 2.1.0, not latest.
- Tolerance 1e-9, None-aligned, never NaN on the wire.
- Backend ids, input keys, plot keys mirror engine descriptors exactly.
- Full suite stays green (baseline 2939 passed, 2 skipped).

---

### Task 1: Differential dump harness

**Files:**
- Create: `openalgo-charts/scripts/dump-indicator-parity.mjs`
- Consumes: `openalgo-charts/dist/openalgo-charts.indicators.mjs`, `trading/tests/analytics/goldens/fixtures.json`
- Produces: `/tmp/parity-102.json` shaped `{id: {settings, plots: {plotKey: (number|null)[]}}}`

**Interfaces:**
- Consumes: descriptor `calc(bars, settings)` where bars are `{time,open,high,low,close,volume}[]`
- Produces: JSON dump with engine defaults for every BUILTIN_INDICATOR

- [ ] **Step 1: Write the dump script**

```js
// openalgo-charts/scripts/dump-indicator-parity.mjs
import { readFileSync, writeFileSync } from 'node:fs';
import { BUILTIN_INDICATORS } from '../dist/openalgo-charts.indicators.mjs';

const fixturePath = process.argv[2];
const outPath = process.argv[3];
const { bars } = JSON.parse(readFileSync(fixturePath, 'utf8'));
const out = {};
for (const d of BUILTIN_INDICATORS) {
  const settings = {};
  for (const inp of d.inputs ?? []) {
    if (inp.type === 'number' || inp.type === 'select' || inp.type === 'source') settings[inp.key] = inp.default;
    if (inp.type === 'boolean') settings[inp.key] = inp.default;
  }
  const values = d.calc(bars, settings, undefined, undefined);
  const plots = {};
  for (const [k, arr] of Object.entries(values)) plots[k] = Array.from(arr, (v) => (v === null || v === undefined || Number.isNaN(v) ? null : v));
  out[d.id] = { settings, plots };
}
writeFileSync(outPath, JSON.stringify(out));
console.log(`dumped ${Object.keys(out).length} indicators`);
```

- [ ] **Step 2: Run it**

Run: `node scripts/dump-indicator-parity.mjs ../trading/tests/analytics/goldens/fixtures.json /tmp/parity-102.json` from `openalgo-charts/`
Expected: `dumped 102 indicators`

- [ ] **Step 3: Commit**

```bash
git add openalgo-charts/scripts/dump-indicator-parity.mjs
git commit -m "test: differential indicator dump harness against engine 2.1.0"
```

### Task 2: Drift audit of existing 90

**Files:**
- Create: `/tmp/drift-report.txt` (scratch, not committed)
- Test: run a scratch pytest comparing `compute_indicator` vs `/tmp/parity-102.json` for the 90 registered ids

- [ ] **Step 1: Write scratch comparison script** `trading/tests/analytics/test_parity_21_scratch.py` mapping backend plot keys to engine plot keys via existing PLOT_MAP/PARAM_MAP, comparing at 1e-9, printing mismatches with index and values.
- [ ] **Step 2: Run and record drift list.** Expected: a concrete list of `{id}.{plot}[{i}]` mismatches (possibly empty).
- [ ] **Step 3: Delete scratch file after promoting findings into Task 4 fixes.**

### Task 3: Port the 12 missing indicators

**Files (follow existing family layout):**
- Modify: `trading/src/tradex_trading/analytics/volatility_bands.py` — standard-error-bands, standard-deviation, standard-error
- Modify: `trading/src/tradex_trading/analytics/volatility.py` — ma-channel, chaikin-volatility
- Modify: `trading/src/tradex_trading/analytics/oscillators_trend.py` (or matching trend module) — hull-suite, linreg-slope, smma, t3, consolidation-breakout
- Modify: `trading/src/tradex_trading/analytics/volume_simple.py` (or matching volume module) — net-volume
- Modify: studies module hosting seasonality — seasonality (wrap existing `seasonality.py` compute, register under engine id/inputs/plots)

**Per indicator (TDD):**
- [ ] Read the TS source in `openalgo-charts/src/indicators/<family>/` for exact formula, warmup (null count), and settings defaults.
- [ ] Write failing test in `trading/tests/analytics/test_parity_21.py`: `compute_indicator('<id>', CANDLES, <engine defaults>)` vs `/tmp/parity-102.json` plots at 1e-9.
- [ ] Implement minimal port; run test; commit per indicator (`feat(indicators): port <id> from engine 2.1.0`).

### Task 4: Correct drifted logic

**Files:** whichever modules own the drifted indicators (from Task 2 report).

- [ ] Per drifted indicator: failing case first (harness output pinned), minimal correction to match 2.1.0 calc, re-run harness, commit per indicator (`fix(indicators): align <id> with engine 2.1.0`).

### Task 5: Registry, catalogue, Tier-2 conformance

**Files:**
- Modify: `trading/src/tradex_trading/analytics/indicators.py` (PARAM_MAP entries for 12, catalogue inclusion)
- Modify: `trading/tests/analytics/test_golden_parity.py` (extend PARAM_MAP + goldens for 12)
- Create: `trading/tests/analytics/test_tier2_contract_2_1_0.py`

- [ ] Extend PARAM_MAP with TS-settings→backend-param entries for the 12 (identity where keys already match).
- [ ] Add goldens for the 12 generated from `/tmp/parity-102.json`.
- [ ] Conformance test: for every engine descriptor input key of the 102, backend catalogue accepts it; compute endpoint returns `{id, points: [{time, ...plots}], meta}` with null (never NaN) padding matching engine alignment.
- [ ] Run full parity + conformance; commit.

### Task 6: Full verification

- [ ] Run: `PYTHONPATH=domain/src:brokers/src:trading/src python -m pytest tests domain/tests brokers/tests trading/tests services -q -p no:cacheprovider`
- [ ] Expected: all pass, 0 failures; commit final (`test: full 102-indicator parity with engine 2.1.0`).
