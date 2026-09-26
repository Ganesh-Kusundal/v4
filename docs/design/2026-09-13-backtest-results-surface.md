# The backtest results surface

**Status:** implemented, tested
**Scope:** `frontend/src/backtest.ts` (new), `frontend/src/tier2.ts`, `frontend/src/main.ts`,
`trading/src/tradex_trading/strategy/core/brackets.py` (new),
`trading/src/tradex_trading/strategy/extensions/strategies/bracket_breakout.py` (new),
`trading/src/tradex_trading/strategy/core/engine.py`,
`trading/src/tradex_trading/replay/backtest.py`,
`trading/src/tradex_trading/interface/routes/chart.py`
**Tests:** `frontend/e2e/backtest-surface.spec.ts` (3 tests),
`trading/tests/strategy/test_signal_protective_levels.py` (10 tests),
`trading/tests/replay/test_bridge_protective_levels.py` (5 tests),
`trading/tests/interface/test_chart_backtest.py::TestProtectiveLevelsOnFills` — with
three negative controls

## The problem

`POST /api/charts/backtest` returned `{metrics, equity_curve, trades}`. The chart
used **one** of those three fields: the equity curve. The statistics
(`total_return`, `sharpe`, `max_drawdown`, `num_trades`, `num_rejected`,
`total_fees`) and the fill list (`time, side, price, qty, reason, rejected`) were
computed by the engine, serialized over the wire, and dropped on the floor. Most
of the TradingView-style results surface was already paid for.

The backend is explicit about which list is for drawing, too:

> `fills`: Executed fills recorded by the pipeline… **chart markers should plot
> THESE.** Rejections appear here with `rejected=True` and price 0 so a UI can
> render them as error markers.

— `trading/src/tradex_trading/replay/backtest.py`

## What was wired

`frontend/src/backtest.ts` owns the single run and fans it out, because one run
has four consumers that resolve at different times:

```
POST /api/charts/backtest → {metrics, equity_curve, trades}
  equity_curve ──→ the Backtest Equity pane (Tier-2 plot, right scale)
                └→ the drawdown column (left scale), derived off the same series
  metrics      ──→ the statistics grid in that pane (`table` hook)
  fills        ──→ entry/exit chevrons (`chart.trading.setTrades`)
                ├→ position zones      (IndicatorDrawings on pane 0)
                └→ protective brackets ┘  SL/TP lines, from each entry fill
```

The run is published once (`publishBacktestResult`) and every consumer reads
`currentBacktestResult()`, so the four surfaces cannot disagree about what was
run. `null` means "this run produced nothing" and clears rather than leaving a
stale table or marker set over a new instrument.

### Drawdown

Derived in `runBacktest` from the shipped equity curve against its own running
peak, ×100, and carried on the same `Tier2Point` as the equity value — so the
pane plots the curve the engine measured `max_drawdown` on.

**Why this is not "recomputing the engine's numbers":** `max_drawdown` *is* the
minimum of that column. Measured live against the endpoint:

```
derived min drawdown: -0.005112905838150605
engine max_drawdown : -0.005112905838150578
```

They are the same number to floating-point noise, which is why the panel and the
statistics block cannot drift apart. The alternative (a `drawdown_curve` from the
backend) would cost a route change and a test for no additional truth — but note
the invariant is now asserted in the E2E suite, so a backend change that breaks
it fails a test rather than silently mislabelling a panel.

It is plotted on the **left** scale, `title: 'Drawdown %'`. On one scale a 0.5%
dip is invisible against a 100 000 equity line, which is the entire reason a
drawdown column exists.

### Position zones

A fill list is not a position list. These strategies **scale in** —
`mean_reversion` buys five dips before it exits — so a zone is not "fill N to
fill N+1": `pairRoundTrips` carries an open lot at its own weighted-average entry
and closes it with whatever fill takes it to zero or flips it. A flip is one fill
but two positions' worth of fact: it closes the lot and opens the remainder at
the fill's own price.

Measured on the live datalake (RELIANCE 1h, Apr–Sep):

| strategy | fills | round trips | lot open at the end |
|---|---|---|---|
| `mean_reversion` | 42 | 16 | **buy +10 @ 1313.90** |
| `sma_cross` | 50 | 25 | none |

A zone is a `box` from entry time/price to exit time/price, with the return on a
`label` chip at the zone's top-left corner. Anchors are **times**, not indices, so
zones stay on the bars they happened on when history is paged in.

Two rendering facts worth keeping:

- **The chip is a separate `label`, not the box's `text`.** A box caption is
  plated in the box's own colour, so a matching `textColor` paints the plate solid
  and hides the number — which is exactly what the first version did (solid
  green/red blocks, no readable text). Passing a *different* colour would hard-code
  a contrast the theme cannot re-decide.
- **The caption is the price return.** The backend reports fees per run, not per
  trade, so a net per-trade figure would be a guess. The statistics block carries
  the run's total.

The open lot is a dashed level at its average entry price plus a caption, **not**
a box: a position with no exit has no second real price to anchor to, and drawing
one from the last close would claim a fill that never happened.

## The defect this work introduced, and the test that catches it

The statistics grid was first written as the descriptor's `attach`:

```ts
{ ...createTier2Indicator({ … fetch }), attach: (ctx) => { /* the table */ } }
```

`createTier2Indicator` **already returns an `attach`** — that *is* the fetch /
align / subscribe lifecycle (`openalgo-charts/src/indicators/external.ts`). The
spread deleted it. The pane still rendered, its legend still appeared, and no
error surfaced anywhere; it simply never fetched, so `calc` aligned an empty
point list forever.

The library has a hook for this and says so:

> Some studies are not a value per bar at all: a seasonality heatmap is a matrix
> of monthly returns, **a scoreboard is a handful of statistics**. Those have no
> place in `calc`… so they come back through here instead. Runs after every
> `calc`.

— `table?(ctx)` in `openalgo-charts/src/model/indicator-registry.ts`

`table` **composes** with the Tier-2 descriptor; `attach` collides with it. The
grid now rides `table` and the runtime creates it lazily in the pane, so it moves,
clips and dies with the pane and the host owns no primitive lifecycle.

Nothing in the rendered output said "no request was ever made". One assertion
does — which is why the E2E test's first check is `POST /api/charts/backtest`
fired, not anything about the canvas.

## Protective levels: from a signal's declaration to a drawn line

The first version of this surface drew no stop-loss or target lines, because
nothing in the pipeline produced them. The domain was ready — `OrderRequest`
carries `stop_loss_price`/`target_price`, and `BracketOrderRequest` validates
`SL < entry < target` (mirrored for a short) at construction — but every
producer in between was empty: no strategy declared a level,
`ReactiveStrategyEngine._maybe_publish_order` did not read one, and
`BacktestEngine._bridge_signal` built a bare `MARKET` request.

The capability is now wired end to end, through **one** contract.

### The contract

```
Signal.metadata['stop_loss_price' | 'target_price']      ← the strategy's decision
  → protective_request(signal, entry=…, quantity=…)      ← one place, all three call sites
  → BracketOrderRequest   (both legs, side-valid)        ← domain invariant, venue composite
  → Order.stop_loss_price / target_price                 ← already persisted by the OMS
  → BacktestResult.fills[i]['stop' | 'target']           ← indexed by order_id from OrderPlaced
  → GET /backtest  trades[].stop / .target
  → SL/TP lines + captions on the price pane
```

`strategy/core/brackets.py` is the single implementation, called by all three
places that build an order from a signal: the engine's `next_open` flush, the
engine's `signal_close` submit, and the backtest's recording-only bridge. Before
it, those three sites built near-identical `OrderRequest`s by hand — so a level
added at one site would have been silently absent at the other two, and the
whole point of the shared signal→order path is that live and backtest cannot
diverge.

Four decisions in that module are deliberate:

- **A pair, never a lone leg.** The venue's composite (super) endpoint is the only
  thing in the stack that carries a protective level; a plain order's adapter
  builds its own payload and would drop one. A half-declared pair is ignored —
  loudly — rather than shipped as protection the strategy believes it has. The
  domain encodes the same rule as a hard invariant.
- **Validated against the price the order actually fills at.** With `next_open`
  the fill is the *next* bar's open, which can gap past the declared target. An
  invalid pair degrades to an unprotected order and logs; it does not raise. A
  run that dies on bar 4,000 of 5,000 because of one gap is worse than a run
  with one unprotected trade, and the venue would have rejected it anyway.
- **An unusable value is dropped, not fatal.** Strategy arithmetic on a thin bar
  produces `None`/`NaN`; a non-finite level would make every later comparison
  silently false, so `NaN`/`inf` are rejected explicitly rather than converted.
- **Nothing is inferred.** No `null` level becomes a default, and the chart draws
  a line only where a level is present, so it can never show a stop the run did
  not have.

### The producer

`extensions/strategies/bracket_breakout.py` declares the levels *and trades them*:
entry on a close beyond the previous N bars' extreme, stop at `stop_atr` × ATR,
target at `reward` × risk, and exits only when the bar reaches one of the two.
That pairing is the point — the contract is only worth having if the drawn line
describes the order that existed.

Two conventions in it are worth knowing. When one bar's range covers both levels,
OHLC cannot say which came first, so the strategy resolves it as the **stop** —
the pessimistic reading, because assuming the favourable order of two unknowable
events reports a return that was never earned. And exits are reported *at the
level*, not at the bar's extreme, since the level is where the order was; the
fill then lands wherever the next bar opens, which on a gap is *beyond* the
level. The chart shows both, because both are true.

Measured on the live datalake (RELIANCE 1h, `lookback=10`, `atr_period=5`): 9
fills, 5 of them entries carrying a valid pair — e.g. a short at `1410.70` with
`stop=1468.70` / `target=1302.20`.

### The honest limit

Engine-side protective exit **is** implemented in ``tradex_replay.backtest``
(Option C1 of the solid-platform mega design): after an entry fill carrying a
complete stop/target pair, subsequent bars that pierce a level close via the
same ``ExecutionEngine`` path, stop-first when both hit on one bar, fill at the
level price.

Remaining limits:

- A strategy that both declares brackets **and** emits its own exit can still
  double-exit; ``bracket_breakout`` that exits at its declared levels remains
  the safer reference producer.
- Lone legs are still dropped (pair required); trailing stops are not simulated.
- Zones/brackets show price results, not net fees.

## Verification

| check | result |
|---|---|
| `frontend/e2e/backtest-surface.spec.ts` | **3 passed**, negative control fails the POST assertion |
| `frontend/e2e` (full) | **9 passed** |
| `npm run typecheck` / `npm run build` | clean |
| `pytest domain/tests trading/tests brokers/tests` | **3044 passed, 2 skipped** (+ the bridge suite below) |
| `trading/tests/strategy/test_signal_protective_levels.py` | **10 passed** |
| `trading/tests/replay/test_bridge_protective_levels.py` | **5 passed** |
| live chart | equity + drawdown panes, statistics grids, zone chips, chevrons, and **SL/TP lines with captions**, zero console errors |

**Three negative controls, all run.**

1. Disabling the pane's fetch so it never requests (`ctx.to === 0 || true`) fails
the E2E POST assertion — that is how the stale-bundle hole below was found, and
after fixing the build step it fails for the right reason.
2. Feeding `declared_levels(None)` into `protective_request` failed exactly three
tests — the two mapping unit tests and `TestProtectiveLevelsOnFills` — while the
tests asserting *dropped* or *null* levels kept passing. That profile is the one
worth having: it distinguishes "carried" from "correctly not carried" instead of
passing on both.
3. Making `_brackets()` always return `True` failed two tests with the domain's own
`ValueError: bracket protective prices are invalid for the order side (BUY)` —
which is the point of checking geometry *before* constructing the order. Without
that check the exception is thrown from a constructor in the middle of a bar
stream, i.e. it takes the run down rather than degrading one trade.

**The negative control caught a hole in the test harness itself.** Patching the
fetch to never request did *not* fail the new test — because `npm run e2e` did not
build, so the suite was asserting against whatever `dist/` happened to be on
disk. `"e2e": "npm run build && playwright test"` now, and the same patch then
fails the test as it should. Any earlier "the suite passes" claim from a session
that had not just built is worth re-reading in that light.

## Known limitations

**Protective exits are engine-simulated in backtest** (see "The honest limit"
above). Remaining gaps: double-exit if a strategy also emits its own exit; lone
legs still dropped; trailing stops not simulated.

With several Backtest Equity panes on one chart, the price-chart layer (markers,
zones and brackets) shows the run that published **most recently** — there is one price
pane and N runs. The pane count is unconstrained, so "the topmost pane wins"
would strand the overlay when that pane is deleted; last-publish-wins always
describes a live run. Two backtest panes are two strategies on the same
instrument, so this is a real case, not a hypothetical one.
