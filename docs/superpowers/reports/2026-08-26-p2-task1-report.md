# P2 Task 1 Report — Profiles + seasonality golden generator

Date: 2026-08-26
Status: DONE

## Export check (Step 1)

```bash
cd /Users/apple/Downloads/openalgo-charts-master/dist
node --input-type=module -e "
import * as p from './openalgo-charts.profile.mjs';
console.log(['computeVolumeProfile','computeTpo','computeMarketProfile','computeFootprint'].every(k => typeof p[k] === 'function'));
import * as ind from './openalgo-charts.indicators.mjs';
console.log(typeof ind.SEASONALITY.table);
"
```
Output:
```
true
function
```
All four profile exports live in `openalgo-charts.profile.mjs` as expected; `SEASONALITY.table` is a function in the indicators bundle. No import adjustment was needed.

## Generator

`scripts/generate_goldens_profiles.mjs` reads `trading/tests/analytics/goldens/fixtures.json` (`fixtures.bars`, 300 bars as `[time, open, high, low, close, volume]`), invokes each reference function with the plan's fixed defaults, and serializes the FULL result dict (floats: `Number.isFinite` → keep, else `null`) as `{ "id", "settings", "result" }`.

Calls (TS side, exact):
- `computeVolumeProfile(bars, 0.1, 0.7)`
- `computeTpo(bars, 30, 0.1, 0.7, 2)`
- `computeMarketProfile(bars, { tickSize: 0.1, rowTicks: 1, session: 'day', blockMinutes: 30, valueAreaPercent: 0.7, initialBalancePeriods: 2, compositeSessions: 1, tailEdges: 0 })`
- `computeFootprint(time=BARS[0].time, trades, 0.1, 1)`
- `SEASONALITY.table({ bars, settings: { startYear: 2015, ignoredMonths: '' } })`

## Synthetic fixtures (Step 3)

- **Footprint:** synthetic trades from ALL 300 bars — per bar one `ask` print at `high` for `volume/2` and one `bid` print at `low` for `volume/2`. `time = BARS[0].time = 1784087100`. Golden settings `{ time: 1784087100, tickSize: 0.1, rowTicks: 1 }`.
- **Seasonality:** deterministic 13-month series inline — Jan 2025..Jan 2026, one bar per month at UTC first-of-month midnight, `close = 100 + 3*month_index`, `open = close-1`, `high = close+1`, `low = close-2`, `volume = 1000`. Golden settings `{ startYear: 2015, ignoredMonths: '' }`. Missing settings handled by the `table` hook's `num`/`str`/`on` defaults (confirmed in `seasonality.ts`).

## Per-profile golden structure (Step 4)

| id | settings | structure |
|---|---|---|
| `volume-profile` | `{tickSize, valueAreaPercent}` | `buckets` (249, sorted 118.7→99.9 high→low, each `{price, volume}`), `poc` 111.3, `vah` 118.1, `val` 102.8, `totalVolume` 900533.0 |
| `tpo` | `{periodBars, tickSize, valueAreaPercent, ibPeriods}` | `buckets` (249, each `{price, count}`), `poc` 112, `vah` 117.2, `val` 103.5, `ib` `{high: 114.682, low: 99.8541}` |
| `market-profile` | 8 params + `timezone: 'Asia/Kolkata'` (in result `options`) | `sessions` (2): session 0 = 189 levels (start 1784087100, end 1784101440), session 1 = 81 levels (start 1784173500, end 1784177040); each session has `levels[{price,count,periods,volume,letters}]`, `poc/vah/val/high/low/open/close`, `periods` (8 / 1), `periodDetail`, `initialBalance`, `rangeExtension`, `singlePrints`, `buyingTail/sellingTail` (null), `poorHigh/poorLow`, `developing`, `dayType` ('normal'), `openType` ('auction'), `volumePoc`, `totalVolume` |
| `footprint` | `{time, tickSize, rowTicks}` | `time` 1784087100, `cells` (209, sorted high→low, each `{price, bidVol, askVol}`), `delta` 0 (ask sum = bid sum by construction) |
| `seasonality` | `{startYear, ignoredMonths}` | `rows` (6): header, 2025 year row (Feb..Dec populated: 3.00%..2.31%, Jan blank as the first month is unmeasurable), divider, `Avgs:` row, `StDev:` row, `Pos%:` row; `options` `{position: 'bottom-center', cellWidth: [52, 46×12], cellHeight: 18, rowWeights: [1,1,0.3,1,1,1], widthPercent: 100, heightPercent: 95, fontSize: 10, margin: 8}` |

The forming month (Jan 2026) is excluded by the TS `buildMatrix` logic, so exactly one year row (2025) is present — the non-empty matrix requirement is met.

## Concerns

- `footprint.delta` is exactly 0 because the synthetic rule makes ask total = bid total by construction; that's expected, not an error.
- `market-profile` fixture bars span 2 calendar days (~63 bars day 1, ~1 bar day 2 in the 300-bar series), so 2 sessions is correct and expected.
- No floats serialized to `null` in any golden (all finite).

## Commit

`929806a` — `test(profiles): golden fixtures from reference profile bundle` (6 files: generator + 5 goldens). The report file itself is left untracked per the brief's "stage ONLY those" rule.