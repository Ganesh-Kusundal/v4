# P1 Task 2 Report — backend transforms.py + golden parity gate

**Date:** 2026-08-26
**Status:** DONE

## Summary

Ported all 6 openalgo-charts series transforms (heikin-ashi, renko, range-bars,
line-break, point-figure, kagi) to pure Python in
`trading/src/tradex_trading/analytics/transforms.py`, plus the golden parity
gate `trading/tests/analytics/test_golden_parity_transforms.py`. All 7 tests
pass (6 parity + 1 fail-loud) against the actual reference-bundle goldens at
1e-9 / None-aligned. Full analytics suite: 318 passed. Ruff clean.

## Per-transform status

| Transform | Golden bars | Status |
|-----------|------------:|--------|
| heikin-ashi | 300 | PASSED (1:1) |
| renko | 21 | PASSED |
| range-bars | 14 | PASSED |
| line-break | 183 | PASSED |
| point-figure | 5 | PASSED |
| kagi | 2 | PASSED |
| unknown-id fail-loud | — | PASSED |

All 6 passed clean on first implementation; no golden debugging required. One
self-inflicted bug during authoring (kagi `_vertex` calls omitted `_thick`) was
caught and fixed before the first test run.

## TS → Python subtleties

- **Undefined volume → 0.** Renko/range-bars/line-break/point-figure emit bars
  without a `volume` field (the golden generator wrote `volume ?? 0`). Python
  mirrors this by not setting `volume`, so the parity test's
  `g.get("volume", 0)` yields 0. Heikin-ashi keeps `volume`, kagi sets `1/0`.
- **kagi time-0 flush vertex.** `flush()` emits `vertex(0, _ext, _thick)`;
  `_ensure_increasing_times` bumps time `0` to `prev + 1` (golden second bar =
  1784093821). The flush vertex carries the current `_thick` (volume 0 in the
  golden) — passed correctly.
- **Floor division.** Point-figure/renko use `Math.floor` for integer box
  indices/anchor; Python `math.floor` matches JS exactly for finite numbers.
  Also used `math.ceil` for the reversal-boundary box derivation in point-figure.
- **Wilder ATR seeding.** Ported `WilderAtr` as a private helper: during warmup
  (`n < period`) it reports the running TR mean (`sum/n`), then switches to the
  recursive `(atr*(period-1)+tr)/period`. `period = max(1, floor(period))`.
- **Point-figure `_resolveBox` fallbacks.** `box > 0 && isFinite(box)` →
  previous box → `abs(price)*0.01` → `1`. Only `mode='fixed'` exercised here.
- **Dispatch coercion.** `compute_transform` coerces params to `int`/`float`
  per the spec default type (`int(value) if isinstance(default, int) else
  float(value)`), bridging JSON numbers.

## Exact test output

```
$ ../.venv/bin/python -m pytest tests/analytics/test_golden_parity_transforms.py -q
.......                                                                  [100%]
7 passed in 0.41s
```

Per-transform:
```
test_transform_matches_ts_golden[heikin-ashi] PASSED
test_transform_matches_ts_golden[kagi] PASSED
test_transform_matches_ts_golden[line-break] PASSED
test_transform_matches_ts_golden[point-figure] PASSED
test_transform_matches_ts_golden[range-bars] PASSED
test_transform_matches_ts_golden[renko] PASSED
test_unknown_transform_id_fails_loudly PASSED
```

Full analytics suite:
```
$ ../.venv/bin/python -m pytest tests/analytics/ -q
318 passed in 0.65s
```

## Ruff output

```
$ ../.venv/bin/ruff check trading/src/tradex_trading/analytics/transforms.py \
    trading/tests/analytics/test_golden_parity_transforms.py
All checks passed!
```

(Initial run flagged E741 `l` in the transcribed test skeleton and E501 long
lines in `TRANSFORM_SPECS`; fixed by renaming the loop variable to `low`/`line`
and reflowing the spec table. No behavior change.)
