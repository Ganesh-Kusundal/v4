# P2 Task 2 Report — backend profiles.py + seasonality.py + golden parity gate

**Date:** 2026-08-26
**Status:** DONE
**Commit:** (see final line — staged after this report is written)

## Summary

Ported the four profile studies (`volume-profile`, `tpo`, `market-profile`,
`footprint`) to `trading/src/tradex_trading/analytics/profiles.py` and the
seasonality table to `trading/src/tradex_trading/analytics/seasonality.py`,
plus the parity gate `trading/tests/analytics/test_golden_parity_profiles.py`.
All 6 parity tests pass (5 studies + fail-loud dispatcher); full analytics
suite is 324 passed; ruff clean on all three files.

## Per-function status

| Study | Status | Notes |
|---|---|---|
| volume-profile | PASS clean | buckets/poc/vah/val bit-exact vs golden |
| tpo | PASS clean | `poc` 112.0 == golden int 112; 249 buckets |
| market-profile | PASS clean | 2 sessions, all fields exact (levels, periodDetail, developing, tails, types) |
| footprint | PASS clean | 209 cells bit-exact, delta 0.0 == golden 0 |
| seasonality | PASS (1 test-fixture fix) | colors/rows/options exact |
| unknown-id dispatcher | PASS | `ValueError` |

## The `bucket_price` parity trap — decision

JS `Math.round` is round-half-up; Python `round` is round-half-even. The
fixture **does** exercise a .5 boundary: bar `[1784092080, 113.4296, 114.05,
112.8157, 113.4794, 1661.0]` has high `114.05`, whose quotient `114.05/0.1 =
1140.5` is an exact double. JS `Math.round(1140.5)` = 1141 → bucket **114.1**;
Python `round(1140.5)` = 1140 → bucket **114.0**.

Verified empirically against the golden:
- banker's `round` produces real bucket-volume differences from the golden
  (~1 bar's share = ~118 volume cascading across ~6 buckets, e.g. golden
  `114.1 → 9128.14218281718` vs banker's `9009.499325674`).
- round-half-up produces **bit-exact** buckets for all 249 volume-profile
  prices, the 209 footprint cells, and every market-profile level.

**Decision:** implement `bucket_price` with round-half-up as
`math.floor(x + 0.5)` (equivalent to `Math.round(x)` for every double,
positive or negative). This matches the golden, which is ground truth.

## Other TS → Python subtleties

1. **`sum()` ≠ sequential reduce (the `totalVolume` trap).** CPython 3.13's
   builtin `sum()` uses pairwise summation for floats, so
   `sum(b["volume"] for b in buckets)` = `900533.0` while the reference's
   `buckets.reduce((s,b) => s + b.volume, 0)` = `900532.9999999987` (verified
   in node against the same parsed doubles). A plain left-to-right
   accumulation loop reproduces the reference exactly — used everywhere a
   running total is summed (volume-profile `totalVolume`, tpo `total`, va
   expansion totals, footprint `delta`).

2. **IST calendar.** `market-profile` and `seasonality` resolve on
   `Asia/Kolkata` (fixed +5:30, no DST). Used `zoneinfo.ZoneInfo("Asia/Kolkata")`
   with a fast fixed-offset branch for the default zone in `_day_index_in` /
   `_minute_of_day` / `_parts_in`, mirroring `feed/time.ts`'s `utcSecondsToIstParts`
   arithmetic. `zonedWallClockToUtcSeconds` is a single `datetime(...,
   tzinfo=ZoneInfo(zone)).timestamp()` — equivalent to the reference's
   two-pass DST correction because IST has no DST.

3. **Month-13 rollover.** JS `Date.UTC(2025, 12, 1)` rolls into Jan 2026;
   Python `datetime(2025, 13, 1)` raises `ValueError`. Both `_month_spans`
   (seasonality) and the test's `_seasonality_bars()` fixture construction
   needed the rollover (`2025 + m // 12, m % 12 + 1`). The plan's test
   skeleton contained the Python-invalid `datetime(2025, 1 + m, ...)` for
   m=12; adjusted to mirror the JS generator's rollover. Golden is Jan
   2025..Jan 2026 (13 spans), consistent.

4. **tpo `poc` int vs float.** The reference's bucket price at POC is a float
   (112.0) serialised by `JSON.stringify` as int `112`. Python numeric
   equality (`112.0 == 112`) plus `_round_floats` (which only rounds floats)
   handles the cross-type; no forced int/float conversion.

5. **Seasonality colors.** `withOpacity` (#rrggbb + alpha byte) ported
   exactly, including the round-half-up byte (`Math.round(alpha*255)` →
   `floor(x+0.5)`); headers `#787b8633`, SKIP `#787b8680`, ramp cells
   `#08998138`/`#08998137`/`#08998180` all reproduce. Empty cells carry no
   `bgColor` key (year/avgs/pos rows) while StDev empty cells do
   (`{text:"", bgColor: HEADER_BG}`) — matches the reference's push shapes.

6. **market-profile fields.** `levels[].letters` joins TPO letters in period
   order; `periodDetail` carries `index/letter/startTime/endTime/high/low/
   volume`; `developing` recounts counts ≤ each period index per level, then
   runs `poc_and_value_area` on the partial level list (skip levels with 0
   count); `tailEdges=0` yields `buyingTail/sellingTail = null`; session
   grouping preserves first-seen order on the IST day index; `rangeExtension`
   and `volumePoc` (max volume, `>` not `>=`) match. `options` echoes
   `{tickSize, rowTicks, session, blockMinutes, valueAreaPercent,
   initialBalancePeriods, compositeSessions, tailEdges, timezone}`.

7. **Dispatcher.** `compute_profile` coerces params per spec default type
   (`int`/`float`/`str`); `session` ("day") falls through to the `str` branch;
   `footprint` reads `time` from `params` (default `0`) and treats the bars
   payload as the trades list, per the route contract.

## Verification output

Parity gate (from `trading/`):

```
$ ../.venv/bin/python -m pytest tests/analytics/test_golden_parity_profiles.py -q
......                                                                   [100%]
6 passed in 0.47s
```

Full analytics suite:

```
$ ../.venv/bin/python -m pytest tests/analytics/ -q
........................................................................ [ 88%]
....................................                                     [100%]
324 passed in 0.76s
```

Ruff:

```
$ ../.venv/bin/ruff check trading/src/tradex_trading/analytics/profiles.py \
    trading/src/tradex_trading/analytics/seasonality.py \
    trading/tests/analytics/test_golden_parity_profiles.py
All checks passed!
```

## Files

- `trading/src/tradex_trading/analytics/profiles.py` (new)
- `trading/src/tradex_trading/analytics/seasonality.py` (new)
- `trading/tests/analytics/test_golden_parity_profiles.py` (new)

## Fix round 1: naked_levels + row_of

**Finding:** the plan/brief mandated porting `nakedLevels` (`market-profile.ts:623`)
and `rowOf` (`market-profile.ts:643`) but they were missing from `profiles.py`.

**Change:** added `naked_levels(result)` and `row_of(price, options)` in
`profiles.py` right after `compute_market_profile`, faithful to the TS source:
- `naked_levels` iterates sessions oldest-first; for each `kind` in
  `("poc","vah","val")` the level is naked when no later session `j>i` trades
  back through it (`price <= s[j].high && price >= s[j].low`); emits
  `{"time": startTime, "price", "kind"}` oldest-first.
- `row_of` is `bucket_price(price, options["tickSize"] * max(1,
  floor(options["rowTicks"])))`. Note: options keys are the camelCase
  `tickSize`/`rowTicks` that `compute_market_profile` actually returns (and
  that the golden echoes), not snake_case — so the verify command's
  `r["options"]` resolves. The module has no `__all__`, so both are directly
  importable via `from tradex_trading.analytics.profiles import ...` with no
  extra export plumbing.

**Commands run (from repo root unless noted):**

```
$ cd trading && ../.venv/bin/python -m pytest tests/analytics/test_golden_parity_profiles.py -q
......                                                                   [100%]
6 passed in 0.46s

$ ../.venv/bin/python -m pytest tests/analytics/ -q
........................................................................ [ 88%]
....................................                                     [100%]
324 passed in 0.75s

$ ../.venv/bin/python -c "import sys; sys.path.insert(0, 'src'); from tradex_trading.analytics.profiles import naked_levels, row_of; import json; r = json.load(open('tests/analytics/goldens/profiles/market-profile.json'))['result']; print('naked levels:', len(naked_levels(r))); print('row_of(111.37, r[\"options\"]) =', row_of(111.37, r['options']))"
naked levels: 6
row_of(111.37, r["options"]) = 111.4

$ ../.venv/bin/ruff check trading/src/tradex_trading/analytics/profiles.py
All checks passed!
```

**Commit:** `7d801a8` — `feat(analytics): port naked_levels + row_of in profiles.py`