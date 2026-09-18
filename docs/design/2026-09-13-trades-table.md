# The per-trade table

**Status:** implemented, tested
**Scope:** `frontend/src/trades-panel.ts` (new), `frontend/src/backtest.ts`,
`frontend/src/tier2.ts`, `frontend/src/main.ts`
**Tests:** `frontend/e2e/trades-panel.spec.ts` (2 tests), with two negative controls run

## The problem

The backtest surface drew the run: chevrons for fills, a box per round trip, SL/TP
lines, an equity and drawdown curve, a statistics block. What none of it could say
is what any *individual* trade was worth. A chart holds a box you can point at and
the statistics block holds the run's aggregate (`Net Profit`, `Sharpe`,
`Max Drawdown`, `Trades`, `Fees`) — the middle layer, one row per trade, was
missing. On a 27-trade run, "which of these boxes paid for the other ones" had no
answer on screen.

## What was built

A panel docked as the widget root's own grid row, below the replay transport and
the quantity bar: heading, run summary, one row per round trip.

```
#  SIDE  OPENED          ENTRY     CLOSED          EXIT      SIZE  P&L      RETURN
1  SELL  11 May, 15:30   1,419.90  12 May, 08:30   1,392.00  1     +27.90   +1.96%
2  SELL  12 May, 13:30   1,374.40  15 May, 15:30   1,347.10  1     +27.30   +1.99%
3  SELL  18 May, 09:30   1,324.30  18 May, 11:30   1,338.90  1     -14.60   -1.10%
—  SELL  10 Sept, 10:30   1,275.00  open           —         1     —        —
```

Three things about it are load-bearing:

**Rows come from the same ledger as the zones.** `pairRoundTrips(view.fills).trips`
— the list `mountBacktestZones` draws from — so `data-trip="3"` is an index into
the boxes on screen, not a parallel count that can drift from them. Re-deriving
positions in the panel (or from `metrics.num_trades`, which counts *fills* — 55
against 27 round trips in the run above) is exactly how a row ends up selecting a
trade that resembles the one it names.

**The open lot is a row, with dashes.** A run that ends holding a position has no
exit fill for it, so its exit time, exit price and P&L say `open` / `—` rather
than being marked to the last bar. The chart draws that position's level and the
table lists it, so the two agree about what exists; the numbers a mark would
require are the ones the run never traded at.

**Everything is gross of fees.** `total_fees` is per run, so a net per-trade
figure would be a division nobody made. The heading's total says `gross +114.40`
and carries a `title` saying so.

## Why a DOM table, and not the library's `ChartTable`

`ChartTable` was the first choice — it is the primitive the host already draws the
statistics block with, and it stays put while the chart pans. It cannot do this:

```ts
// primitives/table.ts
public hitTest(x: number, y: number): PrimitiveHit | null {
  if (this._opts.id === undefined || r === null) return null;
  if (x < r.x || x > r.x + r.w || y < r.y || y > r.y + r.h) return null;
  return { externalId: this._opts.id, … };   // the table's box — never the row
}
```

A click names the table, not the row inside it, so the "select the matching zone"
half of the feature has nothing to route on. Rows that select something need real
elements. That costs a scoped host stylesheet (`frontend/src/trades-panel.ts`
injects `.v4-trades`, the same one-per-document pattern as the widget's own),
and it buys scrolling, focus, `aria-selected` on a `role="grid"`, and a table a
test can read.

## The selection contract

```ts
export type TripSelection = { kind: 'trip'; index: number } | { kind: 'open' } | null;
```

One state, two consumers: the panel mirrors it onto the rows and the zone painter
reads it to emphasise what was picked, so a row and its zone cannot disagree. Two
rules fall out of it:

- **A pick is cleared when a run is published.** A new run replaces the fill list,
  so an index into the old one names a different trade, or none. `publishBacktestResult`
  clears before notifying, so the table and the painter rebuild from the same empty
  selection rather than one of them keeping a pick the other dropped.
- **A second click on a picked row clears it.** That is the only way back to a
  chart with nothing emphasised.

The picked zone is redrawn in the **theme's accent colour** with a harder fill and a
2px outline, rather than a thicker line in the trip's own green. A dozen
neighbouring zones of the same hue make a weight change invisible; the accent is
also the one cue a zone that overlaps another still shows. Palette values come from
the chart theme per paint (`chart.theme().upColor/downColor/lineColor`), so a theme
switch restyles the zones with the candles.

### Bringing the picked trade into view

`revealRange` shifts — never resizes — the visible logical range so the picked
span is centred, and does nothing at all when the span is already inside the
viewport: moving a chart the user just framed is worse than doing nothing.

It is reachable because of what the run's window is: **the bars the pane was handed
and not the viewport** (`ctx.bars`, see the defect below). After a zoom-in the run
still covers the wider loaded range, so rows off to either side of the viewport are
ordinary — and a selection nobody can see is not a selection. The conversion goes
through `dataLayer.timeToIndexFloat`, the same mapping the chart places a shape
with, which is why it lands on the bars the zone was drawn on.

## The run-window defect this exposed

Tier-2 passes a descriptor's `fetch` the **request's** window as `ctx.from/ctx.to`.
The engine pages an *extension* — `load(c, state.to, c.to, true)` — whenever the
visible slice grows past what it already holds, and a live bar append does exactly
that. The pane's fetch used those two values as the strategy's window, so an append
re-ran the strategy over the appended sliver and replaced a real curve with it:

```
live, 1h, RELIANCE — observed while building this table
  run over the loaded range      51 closed · 1 open · gross +58.40   (equity curve, Sharpe 0.32)
  seven bars appended →          0 fills          → pane flat, statistics "Trades 0"
```

The table made it visible (its summary went from 51 rows to none) but the defect
was in the window, not the table. Now the window is read from the bars the fetch
was handed:

```ts
const first = ctx.bars[0];
const last = ctx.bars[ctx.bars.length - 1];
if (first === undefined || last === undefined || last.time <= first.time) { … clear … }
```

That is the same value the engine's own `context()` derives its bounds from, so it
is stable across both kinds of load: a full load and an extension now both describe
the whole window on screen.

## Verification

`frontend/e2e/trades-panel.spec.ts`, two tests:

1. **Every row, against the run's own fills.** The spec re-implements the pairing
   (signed lot, weighted-average entry, closed by whatever fill takes it to zero or
   flips it), independently of the app, and compares all nine cells of every row,
   asserting the **sign** on the text as well as the value. The pane is switched to
   `bracket_breakout` first, because the default `sma_cross` is long only and a
   table that renders every side's P&L with the wrong sign is exactly the defect a
   long-only fixture cannot see. The run leaves an open lot, so the dashes are
   covered too.
2. **Selecting.** The row's `aria-selected`, exactly one selected row, and a pixel
   hash of the price pane — the only place the emphasised zone exists, since the
   zone is canvas geometry that no DOM query can see. The first pick and clear are
   discarded before that comparison: the first pick may scroll the trade into view,
   which repaints for a reason that is not the emphasis. The pane is switched to 1h
   and the viewport widened for the same reason — a 2-minute, half-point trade at 1m
   is a sub-pixel box, correctly emphasised and quite invisible.

Negative controls, both run against a rebuilt bundle (the suite serves `dist/`, so a
control without a build proves nothing):

| control | result |
|---|---|
| drop `* direction` from `tripPnl` | **fails** — `Expected 2.4, Received -2.4` on a SELL row |
| painter ignores the pick (`const picked = null`) | **fails** — "the chart must repaint for the picked zone" |

And live, in the browser: picking a row whose trip was off-screen scrolled the chart
onto it (axis moved from 31 Jul onward, the picked zone's plate rendered in the
accent colour, and the equity pane's canvas hash did not change); hashing the price
pane across pick → clear → pick returned the original hash each time, so the
emphasis repaints the same geometry rather than adding a layer.

## What it deliberately does not show

- **Net per trade.** Fees are reported per run; splitting them would be a guess.
- **The open lot's P&L.** No exit fill exists for it, and the run's metrics already
  carry whatever it was worth at the last bar.
- **Rejected fills.** They are counted in the statistics block (`Rejected`), and a
  rejected fill never reached the book, so it is not part of any round trip.

## Limits worth knowing

- On a 1m chart over a multi-day window, a round trip is a sub-pixel box: the
  selection is correct and invisible until the chart is zoomed in. Culling the
  labels by available width would fix the reading and silently drop information, so
  it is not done.
- With more than one Backtest Equity pane, the table (like the statistics block and
  the price-chart overlay) describes the **most recently published** run. One table,
  N panes; "the topmost pane wins" would strand the table when that pane is removed.
- The panel is expanded by default and its list is capped at 140px, so it takes its
  space from the chart. It collapses from its own heading, and the collapsed heading
  still states the counts and the gross total.
