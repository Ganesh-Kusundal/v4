# P1 Task 1 Report — Transform golden generator

Date: 2026-08-26

## Status

DONE

## Bundle export check

The brief's Step 1 check against the base bundle returned all `undefined`:

```
node --input-type=module -e "import * as t from '.../openalgo-charts.mjs'; console.log(typeof t.HeikinAshiTransform, ...)"
=> undefined undefined undefined undefined undefined undefined
```

Grep confirmed the base bundle (`openalgo-charts.mjs`) contains **zero** references to the transform class names — the transform tier lives in a dedicated bundle. The transform classes were located and verified in `openalgo-charts.transform.mjs`:

```
node --input-type=module -e "import * as t from '.../openalgo-charts.transform.mjs'; console.log(Object.keys(t).join(' '))"
=> HeikinAshiTransform KagiTransform LineBreakTransform PointFigureTransform RangeBarsTransform RenkoTransform TRANSFORM_TIER ensureIncreasingTimes registerTransformChartTypes runTransform

node --input-type=module -e "import { HeikinAshiTransform, RenkoTransform, RangeBarsTransform, LineBreakTransform, PointFigureTransform, KagiTransform } from '.../openalgo-charts.transform.mjs'; console.log(typeof ...)"
=> function function function function function function
```

**Action taken (per brief):** the import path in `scripts/generate_goldens_transforms.mjs` was adjusted from `/dist/openalgo-charts.mjs` to `/dist/openalgo-charts.transform.mjs`. All other code was transcribed verbatim from the plan's Task 1 Step 2.

**Verification of transcription:** the reference bundle also exports `runTransform` and `ensureIncreasingTimes`; their minified bodies match the plan's hand-rolled `runTransform` (reset → push all → flush-if-present → strict-increase collision bump `+1s`) exactly. `HeikinAshiTransform.prototype` has no `flush`, so the `if (transform.flush)` guard correctly skips it.

## Per-transform bar counts (sanity check)

| id | bars | notes |
|----|------|-------|
| heikin-ashi | 300 | 1:1 with fixtures (expected) |
| renko | 21 | |
| range-bars | 14 | |
| line-break | 183 | |
| point-figure | 5 | |
| kagi | 2 | thick vertex + thin flush vertex |

All goldens have **strictly increasing** times (verified programmatically). None emitted 0 bars, so no 0-bar investigation was needed.

## Concerns

- The plan's Step 2 snippet imports from the **base** bundle (`openalgo-charts.mjs`), which does not export the transform classes. Task 2/3/4 of the plan should be aware that the reference transform tier is only reachable via `openalgo-charts.transform.mjs` (or `.all.mjs`). This task already used the corrected path.
- `kagi` golden contains only 2 bars (a thick turning vertex at the first move and the flush vertex). This matches the reference behavior for a `reversal: 2` setting over this fixture; the flush vertex (time 0) is bumped to `prev+1` by `ensureIncreasingTimes`, so it is non-zero and strictly increasing. Task 2's parity test must handle `volume` 0/absent for these thin/thick vertices (already accounted for in the Task 2 test skeleton).

## Constraints honored

- `fixtures.json` NOT modified.
- No source under `trading/src/` modified.
- Plan/spec/ledger files untouched.
- Committed only `scripts/generate_goldens_transforms.mjs` and `trading/tests/analytics/goldens/transforms/`.
