# 0002. Production readiness programme

**Date**: 2026-09-27
**Status**: Accepted

## Summary

This spec replaces five competing architecture plans with one sequenced programme. The first
target is paper trading that produces trustworthy numbers, because a strategy is chosen by
reading backtest results and those results are currently wrong in the flattering direction. Later
waves cover the work needed for real money, and they are gated: they do not start until a
strategy has cleared the promotion bar. Code moves from seventeen workspace members to five packages, and work is
organised into waves so that parallel agents never wait on each other unnecessarily.

## Structure

One umbrella spec. Child specs are cut when a wave starts, not before, because writing them now
would freeze decisions the waves are meant to inform.

| Workstream | Waves | Supports |
|---|---|---|
| Instruments and fences | 0 | Making the gates and the graph trustworthy so agents can tell what they broke |
| Research validity | 1 | Trustworthy out of sample numbers |
| Paper path and promotion gate | 2 | A live paper run that means something, and an executable promotion bar |
| Feed correctness | 3 | Live quotes into paper, and no silently dead socket |
| Package collapse | 4 | A dependency graph that is real rather than an artifact of what the scanner can parse |
| Operational floor | 5 | Unattended safe operation on one machine |
| Live money | 6, gated | Real capital. Blocked until the promotion bar is cleared |
| Scale | 7, gated | More symbols, more strategies, more brokers. Not scheduled, designed only where it constrains earlier waves |

**Cross workstream contract**: no wave may leave a test that passes vacuously. Every item that
adds a test also adds a negative control, following the pattern already used in
`tests/regression/test_broker_timeout_unknown.py` and `tests/test_import_boundaries.py` (a test
that asserts a thing is *absent*).

**Source references and premise check**: this spec was written against
`docs/audit/2026-09-27-full-platform-review.md`, which is pinned at `fcaa6b4`. The repository has
since moved to `b941da2`, eight commits later, and three of those commits invalidate premises in
the first draft of this spec. What was re-verified against `b941da2` before this spec was
finalised:

- The lint gate is **green** (`ruff check .` passes, `.ruff_baseline` is `0`). The 13 `I001`
  violations the review reported were fixed in `9e118a9`. Raising the baseline to a non zero value
  would reintroduce headroom the ratchet deliberately has none of, and contradicts the ratified
  decision in `docs/specs/remediation-and-hardening.md`. Lane 0c was deleted for this reason.
- `tradex_trading` is **retired** (`b941da2`), `trading/src/` is empty, and the shim tree is
  **gone** (`388b39f` deleted 113 leaf shims, `0976456` re pointed all 228 test files at the real
  packages, `fc9d147` retired the workspace member). Two `globals().update` shims remain. The
  package collapse, shim removal and test re pointing that the first draft of this spec scheduled in
  Wave 4 are **already done**. Wave 4 was rewritten.
- `sdk/` moved into `tradex_runtime`, so the live fill bridge is
  `runtime/src/tradex_runtime/live_fill_bridge.py` and walk forward is
  `replay/src/tradex_replay/walk_forward.py`. The review's paths for these are stale.

**Correction to the review itself**: it reported 18 packages. There are **17 workspace members**
(`trading` is no longer one). It also reported "17 mypy blocks" where the gate has 11 invocations
over 17 members, leaving 6 uncovered, not 5.

## Requirements

**User stories**:
- As a strategy author, I want backtest results that cannot flatter me, so that I pick the right
  strategy to promote.
- As an operator, I want one screen that tells me whether it is safe to trade, so that I do not
  trade on a stale feed or an unresolved order.
- As an operator, I want to stop one losing strategy without touching the others.
- As a maintainer, I want parallel work streams that do not edit each other's files, so that
  several agents can work at once.

**Acceptance criteria** (each independently checkable):

- **AC-1**: No backtest can fill a signal at a price the strategy could not have known when it
  decided. The final bar flush and the recorded signal bridge both currently can.
- **AC-2**: A backtest that exceeds its capital fails loudly. Today the failing cash debit is
  swallowed by the event bus error isolation and the equity curve looks better than reality.
- **AC-3**: Every reported out of sample metric annualises at the bar frequency stored with the data
  and asserted constant across the dataset, is computed on time disjoint segments that may not
  touch, and each segment starts warm rather than cold.
- **AC-4**: The universe of any backtest is point in time correct including delisted names, and the
  loader asserts this rather than trusting the caller.
- **AC-5**: Position size comes from an explicit quantity on the signal, is clamped to lot size and
  tick size, and no shipped strategy trades one share by accident.
- **AC-6**: Fees are product aware, are on by default in paper and live, and carry Indian
  intraday and delivery rates applied to the correct sides.
- **AC-7**: Each partial fill is booked at that partial's own price. Today the second partial of an
  order is booked at the running average of both.
- **AC-8**: A strategy can be paused or stopped without affecting any other strategy, and stopping
  one halts its order flow on the next event.
- **AC-9**: Every order, fill and position carries the strategy that produced it, so per strategy
  profit and loss and per strategy budgets are computable.
- **AC-10**: No risk limit is optional in paper or live. A missing limit prevents boot instead of
  defaulting to unbounded.
- **AC-11**: Bar buckets are anchored to the session open, so hourly bars cover 09:15 to 10:15
  rather than 08:30 to 09:30, and the last bar of every session is emitted as closed.
- **AC-12**: A silently dead market data socket is detected within a bounded time, and the order
  and portfolio stream is re read from the venue after a reconnect instead of resuming blind.
- **AC-13**: Every strategy produces a persisted validation report that states pass or fail against
  the promotion bar, and the reasons for a failure are visible in the interface.
- **AC-14**: The two paper fill paths, slippage model and book matching, are selectable per run and
  agree within a stated tolerance on a recorded session.
- **AC-15**: A corrupt parquet file is quarantined and counted, and the **affected** symbol returns
  an empty series instead of raising at every call site. Other symbols are unaffected today, because
  the lake is partitioned by symbol, so that half is not the defect. The defect is that
  `ArrowInvalid` propagates to the chart, scanner, backtest, replay and recovery paths with zero
  handling, so one truncated file makes that symbol unreadable everywhere, permanently.
- **AC-16**: The package graph is the five target packages plus one named exempt member, enforced
  by a boundary test with no allowlist that resolves string based imports, so the enforced graph is
  the real one.
- **AC-17**: One route, `GET /api/charts/safety`, returns feed state and last event age, the list of
  unresolved `UNKNOWN` orders with age and correlation id, reconciliation drift totals by severity
  and time last checked, and per strategy its lifecycle state, promotion status, report id and any
  promotion failures. Drift severity is derived rather than entered: an order or position quantity
  or status mismatch is HIGH, a funds difference above one rupee is HIGH, a missing venue read is
  CRITICAL, anything below is LOW, and only HIGH and CRITICAL refuse boot.
- **AC-19**: The backtest is realistic about the two things that flatter a strategy quietly: a protective exit that gaps through its stop fills at the stop price, and a corporate action that is not applied. Both are modelled, and a split landing between two bars is covered.
- **AC-20**: A cold boot reaches `READY` because something deliberately made it ready, and refuses to start if that does not happen. Today the only writer of `READY` is a recovery path that `boot()` never calls, so a session rejects every order until an unrelated reconnect happens to trigger it.
- **AC-21**: Position, cash and fee are applied under one lock as a single operation, so two concurrent submissions cannot both pass the affordability check and both debit. Today the cash ledger has no lock at all and the check and the debits are three separate critical sections.
- **AC-22**: There is exactly one path from a signal to the venue, and it is the one in Flow C. Today a strategy can publish an order command onto the public bus and skip the sizing, risk and protective pair gates entirely.
- **AC-18**: The instruments that decide whether a change is acceptable are themselves trustworthy.
  The lint ratchet holds at `0` and is not loosened, the boundary test resolves string based
  imports so the graph it enforces is the real one, branch coverage is measured with a per package
  floor rather than one aggregate number, and the three benchmarks that currently measure
  placeholders measure real work instead.

## Decision

**Chosen option**: Option 3: gate on research validity, consolidate to five packages, wave the rest.

The repository consolidates from seventeen workspace members to five packages: `domain` (pure
types, zero internal dependencies), `config` (a leaf every package may import and which imports
none, kept separate because `domain` depends on it), `core` (execution, application, reactive,
observability, research, analytics), `ingest` (brokers and market data), `app` (runtime,
interfaces, strategies, replay, backtest). The full map is in the Wave 4 lane table. The first
target is paper trading with a trustworthy validation report. The promotion bar is written into
the report as an executable assertion. Work is organised into waves, each wave internally
parallel.

**Implementation skills**: `architect`
(`.agents/skills/architect/`) · `ponytail` (`~/.agents/skills/ponytail/`) ·
`test-driven-development` (`~/.agents/skills/superpowers/test-driven-development/`) ·
`systematic-debugging` (`~/.agents/skills/superpowers/systematic-debugging/`) ·
`verification-before-completion` (`~/.agents/skills/superpowers/verification-before-completion/`)

## Proposed stack

The stack does not change. That is the decision. Every layer below is already in place and
settled, so the programme spends its budget on correctness rather than on tools.

| Layer | Choice | Reason |
|---|---|---|
| Language | Python 3.12 | Unchanged |
| Framework | FastAPI plus uvicorn, one process | Correct for one operator machine; multi process is explicitly not scheduled |
| Primary store | SQLite, event log plus orders | Correct at this scale; a relational store with transactions is what the idempotency reserve depends on |
| Market data store | Parquet, Hive partitioned | Already holds 4.4 GB across 500 symbols; needs pushdown, not a new engine |
| Auth | Existing API key plus session cookie | Sufficient for one operator; the defect is exposure, not mechanism |
| Event bus | Existing in process reactive bus, publish split from dispatch | Correct shape; the defect is that dispatch happens on the publisher's thread |
| Observability | Existing in house metrics plus structured logging | The catalog and the counters already exist and are correct; nobody consumes them |
| Frontend | Vanilla DOM TypeScript over the pinned chart library | 6,747 lines is well under the size where a state machine library or a component library earns its keep |

## Target data model

Signed off before this spec was written. Every change is additive; no existing column or type is
removed, so every wave is independently revertable. Migration is sized per wave below, not as one
up front task.

| Entity | Change | Required | Null | Why |
|---|---|---|---|---|
| `Signal` | `quantity: Quantity` | no | yes | Sizing today is `abs(signal.strength)`, which trades one share. Null means the sizer computes it |
| `Signal` | `strategy_id: str` | yes | no | The strategy that produced it, taken from `StrategyArtifact.strategy_id` alone, with no version suffix. Required, because every downstream attribution reads it |
| `Order` | `strategy_id: str` | no | yes | Per strategy kill switch, budgets and attribution |
| `Fill` | `strategy_id: str` | no | yes | Same, on the fill itself so attribution does not depend on the order surviving |
| `Fill` | `product_type: ProductType` | no | yes | **Nullable, not additive as NOT NULL.** SQLite cannot add a NOT NULL column to a table with rows, so it is nullable and backfilled from the parent order. A null is treated as `INTRADAY` **and counted** in `fills_missing_product_type_total`, so a mislabelled order is visible rather than silently charged intraday fees |
| `Fill` | `venue_trade_id: str` | no | yes | The key that makes one fill per venue trade possible, replacing the synthetic delta that is booked at the cumulative average |
| `Position` | key becomes `(instrument_id, strategy_id)` | yes | no | The first draft made this a nullable column on a position keyed by instrument alone, which cannot answer which strategy owns what when two strategies share a symbol, and so made AC-9 wrong. Keying the position by both fixes attribution and costs one extra position per strategy |
| `RiskConfig` | `budgets: dict[str, RiskBudget]` | no | yes | Exists today and is unreachable; this is the field that makes it reachable |
| `RiskConfig` | the five limits | yes | no | They default to `None` today, which is why nothing constrains anything |
| `MarginModel` | NEW, per product and segment leverage | yes | no | The cash gate reads broker available margin and treats it as cash, overstating buying power five to twenty times |
| `TradeValidationReport` | NEW. Identity: `report_id`, `strategy_id`, `parameter_set_hash`. Metrics: `oos_sharpe`, `oos_round_trips`, `oos_fills`, `oos_span_days`, `expectancy`, `profit_factor`, `max_drawdown_pct`, `deflated_sharpe`, `param_stability_share_of_grid`. Verdict: `net_pnl_at_2x_costs`, `survives_2x_costs`, `passed`, `failures[]` | yes | no | Makes the promotion bar an assertion rather than a judgement call. Every promotion criterion is one of these fields, so `passed` is computed, never entered |
| `EventRecord` | `stream_id` scoped by account | yes | no | Two brokers today would write into one stream and silently overwrite each other |
| `EventRecord` | per event `schema_version: int` | yes | no | Starts at 1. Existing rows are read by a registered v0 decoder that supplies `prev_hash = None` and skips chain verification, so a bump never invalidates history. Today a version bump invalidates the whole log at once, with no upcaster |
| `EventRecord` | `prev_hash: str` | yes | no | The order log is a compliance artifact and is currently rewritable. `prev_hash = sha256(seq ‖ prev_hash ‖ canonical_json(payload))`, genesis `"0" * 64`, verified on every replay, and a mismatch aborts boot rather than warning |
| `Snapshot` | NEW: `stream_id`, `through_sequence`, payload, `created_at` | yes | no | Recovery replays the entire log into memory today, with no ceiling on growth. Written on reaching `READY` and every 10,000 events. Serialised as canonical JSON, **not pickle**, replacing the log below its `through_sequence` only after the venue reconciles |
| `Fill` dedup index | partition by account | n/a | n/a | Two brokers in one process would drop each other's fills as duplicates |

**Key invariants** (must always hold, enforced in code):
- A fill's `venue_trade_id`, once seen for an account, is never booked twice.
- A position's key is `(instrument_id, strategy_id)` and a fill's `strategy_id` is its own, so a reducing fill from strategy B can never be attributed to strategy A's position.
- The log below a snapshot's `through_sequence` is never read except by that snapshot, and the `prev_hash` chain is verified across the boundary.
- A snapshot's `through_sequence` is the highest sequence it has folded, and replay starts strictly
  after it.
- A `TradeValidationReport` with `passed = true` satisfies every promotion criterion, and no code
  path can set it to true without evaluating all of them.
- No risk limit is `None` once a session reaches `READY`. The set is `max_order_value`, `max_position_value`, `max_orders_per_minute`, `max_daily_loss_amt`, `max_drawdown_pct`, all required in paper and live; `require_fresh_marks` and `reject_unknown_market_value` required **true**; `kill_switch_default` required **false**; `max_mark_age_seconds` defaults to 5.0. There is no environment flag to relax this, because a flag that disables a required limit is a flag that ships unbounded trading.
- A fill's `product_type` is never null in a book created after the backfill, and a null is counted rather than assumed.

**Configuration required**: no new environment variable. An earlier draft of this spec added
`TRADEX_REQUIRE_RISK_LIMITS`; it was removed, because a flag that can switch off a required limit
is a flag that ships unbounded. The requirement is unconditional, and all seven settings already
have environment paths in `config/src/tradex_config/env.py`, so the change is in the schema
defaults rather than in new configuration surface.

## Designs

### Flow A: strategy validation, the leading flow

This is the new flow, and it is the reason research validity comes first.

```
strategy spec + parameter grid
  -> walk_forward, purged, embargoed, warmup carried from train tail
       per fold:
         train   -> grid search, trial count recorded
         out of sample -> run on the test segment only
                          next_open fill, no look ahead, cash enforced
     -> aggregate, frequency correct, segments disjoint and checked
  -> statistical gate
       minimum trades, deflated Sharpe, parameter stability, minimum sample
  -> cost stress
       rerun the out of sample segments at 2x modelled slippage and fees
  -> TradeValidationReport, persisted, one per strategy per parameter set
  -> assertion: passed == every promotion criterion
  -> surfaced in the interface as promotable or not, with the failures listed
```

**Promotion bar** (asserted, not judged, and every criterion is computable from a named report
field):

1. `oos_sharpe >= 1.0`, computed on the **concatenated** out of sample equity curve, never as a mean
   of per fold Sharpes, annualised at the square root of bars per year from the stored timeframe,
   risk free rate 0. A mean of per fold Sharpes is the specific failure being designed out.
2. `oos_round_trips >= 100`, where a round trip is a fill sequence that takes a position to zero or
   through it. Counted in `replay` and `research`; the existing fill count stays available as
   `oos_fills` and is never reported as trades.
3. `oos_span_days >= 90`, measured from the first to the last out of sample bar. The span **may
   contain train windows and embargo gaps** under purged walk forward, so it is a calendar span and
   not a claim of continuous out of sample data.
4. `net_pnl_at_2x_costs > 0`, where the rerun doubles modelled slippage and fees. The statistic is
   total net profit and loss, stated here because expectancy and Sharpe are also defensible readings
   that give different verdicts.
5. `deflated_sharpe >= 0.0`, the Sharpe corrected for the number of trials the grid search tried.
6. `param_stability_share_of_grid >= 0.60`, meaning at least 60% of grid cells score within one
   Sharpe unit of the optimum, so the result is a plateau rather than a spike.

A report failing any criterion sets `passed = false` and names the failures. A strategy with no
report reports `promotion_status = 'not_evaluated'`, which the safety surface displays as not
promotable.

### Flow B: paper session

```
live broker market data -> feed -> bar aggregation (session anchored)
     -> strategy engine -> Signal(quantity, strategy_id, protective levels)
     -> engine.submit
     -> RiskManager, fail closed, all limits present
     -> idempotency reserve -> durable OrderPlaced
     -> fill source, selected per run:
          slippage model: fill at the live quote through a slippage model
          book matching:  the existing paper broker's simulated book
     -> one Fill per trade, at that trade's own price
     -> position, cash and fees applied together under one lock
  parallel, same session:
     recorded session -> replay -> the same engine, for research parity
```

The two fill paths existing side by side is deliberate: running the same recorded session through
both is how the paper path is proven to agree with the research path before a strategy is promoted.

### Flow C: order submission, the money path

The gate order matters more than the gate contents. Each step can only deny, and a denial stops
the order before it can reach the venue.

```
Signal(quantity, strategy_id, protective levels)
  1  PositionSizer      may override or deny; clamps to lot, tick, max notional
  2  RiskManager.check  budget, then exposure, then fresh marks, then cash or
                        margin, then rate, then daily loss. Fail closed
  3  IdempotencyGuard   reserve, bound to the request hash
  4  event store        persist OrderPlaced before any money moves
  5  protective_request levels are a pair, geometry checked against the fill price
  6  venue submit       non idempotent, never retried blind
  7  outcome, three states
        provably local failure -> REJECTED
        may have reached venue -> UNKNOWN, kill switch trips
        accepted                -> ACK
  8  per trade fills -> position, cash and fees applied atomically
```

Step 7's `UNKNOWN` is the design's best existing control and is kept. What is added is step 1,
step 2 with no optional limits, and step 8 applied atomically.

### Flow D: boot, fail closed

```
resolve config -> reject if any risk limit is unset in paper or live
  -> acquire the single writer lock, fail closed on a live lock
  -> build broker, load and validate the instrument master
  -> open the event store, replay from the last snapshot
  -> reconcile orders, positions and funds against the venue
       any high or critical drift -> refuse to start
  -> start the feed, reach READY, or refuse to start
  -> bind the reconciliation monitor, the stale feed monitor, the master scheduler
  -> session state READY
```

Every step above is a precondition for the next, and a failure at any of them means the session
does not start. The current boot sets `READY` without proving the feed reached it, and readiness
ignores whether the event store is writable, so both are corrected here.

### Flow E: recovery

```
process starts -> read the last snapshot for the account stream
  -> replay events after the snapshot sequence
  -> fold with check and debit atomic, and with buying power enforced
  -> reconcile against the venue
  -> adopt the venue's positions, not just report the drift
  -> session READY
```

Recovery today replays the entire log into a tuple in memory with no snapshot and no ceiling on
growth, and it adopts order status but never positions. Both are corrected.

## Build plan

Build approach: end to end tracer bullet slices within each wave, then the wave gate. No task
below starts before its wave's entry condition holds.

### Wave 0, instruments and fences

Entry: now. Nothing else may start until this closes, because agents cannot tell what they broke
if the instruments lie.

| Lane | Task | Satisfies |
|---|---|---|
| 0a | Retire `docs/design/state.md` to a pointer at this spec; move the three repository root reviews into `docs/audits/`. The note on each states, per source review, the findings still open and the lane that owns each, where the open set is exactly the findings this spec does not schedule | none, documentation |
| 0b | Teach the boundary test to resolve `import_module("...")` string edges, with a negative control that a synthetic string edge is detected. Asserting the real graph belongs to the Wave 4 gate, not here | AC-18 |
| 0c | Add the six missing mypy invocations (`research`, `operations`, `observability`, `application`, `persistence`, `services/duckdb-analytics`; the gate runs 11 over 17 members). `operations` and `persistence` are skipped rather than typed, because lane 4d deletes them | AC-18 |
| 0d | Anchor bar buckets to the session open at 09:15 IST; drop and count ticks before 09:15 and after 15:30 in `feed_out_of_session_ticks_total`; emit the final bucket of a session once with `closed=True` when the first tick of the next session arrives **or** at 15:30 wall clock, whichever is earlier, so on Dhan, whose series stops at 15:14, the clock path fires it. Recalibrate `GapDetector`'s default `--min-gap-stamps` and `datalake_stats.py`'s intraday window against the new grid and record both values | AC-11 |
| 0e | Rebuild all three placeholder benchmarks (`bench_tick_parse.py`, `bench_bus_throughput.py`, `bench_order_path.py`) on real work with negative controls, and put `benchmarks/` back under ruff and mypy | AC-18 |

Programme hygiene, tracked but not acceptance bearing: add a `Superseded by 0002` line to each of
the five superseded plans. Tree deletions are lane 4d and are not scheduled twice.

Wave 0 gate: lint ratchet still `0`, mypy covering all 17 members, string edges resolvable, bar grid
correct and the gap tooling recalibrated, benchmarks honest.

### Wave 1, research validity

Entry: Wave 0 closed. Lanes 1a to 1h are independent and run in parallel.

| Lane | Task | Satisfies |
|---|---|---|
| 1a | Remove both backtest look aheads: the final bar flush and the recorded signal bridge | AC-1 |
| 1b | Make the backtest cash debit atomic with the position booking, **and** construct the backtest `RiskManager` with a `CashLedger` and `require_debit=True` so the gate is actually armed, **and** wire the chart backtest route (`interfaces/src/tradex_interfaces/routes/chart.py`) to pass a risk manager, slippage model and fee calculator so an overdraft surfaces as HTTP 422 rather than a flattering curve. Negative control: remove the `cash=` argument and assert the overdraft is then swallowed | AC-2 |
| 1c | Point in time universes: add a membership date column to each `Dependencies/nifty*_list.csv` and a hand maintained `Dependencies/nse_delistings.csv` (`symbol,delist_date,series`); make `research.pit.assert_historical_universe` run on **every** backtest rather than once. The membership history has to be a checked in artifact because nothing in the repository has it and a vendor does not serve it for past dates | AC-4 |
| 1d | Walk forward: carry warmup of `max(indicator period) + 1` bars from the train tail into each out of sample window, where a fold that cannot supply it **fails the report** rather than starting cold; add an embargo of `max(1, ceil(0.01 x window_length_bars))`; annualise from the `timeframe` column of the stored bars, asserted constant across the dataset before the run; require at least 30 out of sample bars per fold; and a fold that raises fails the whole report naming the fold and the exception, because dropping it would silently shrink the span and the trade count and so make the bar easier to pass | AC-3 |
| 1e | Multiple testing control on the grid search: record the trial count, report `deflated_sharpe` and `param_stability_share_of_grid` as first class report fields, and assert a minimum of 30 out of sample bars per fold. No surface failure is ever converted into a zero score, because an argmax over a grid that silently dropped its crashed cells is the standard overfitting trap | AC-3, AC-13 |
| 1f | Live fill bridge: one `Fill` per venue trade at that trade's own price | AC-7 |
| 1g | New `TradeValidationReport` entity and the six criterion promotion bar as an executable assertion, generated by the research walk forward entry point and never by the HTTP route. Persisted to a `tradex_validation_report` table in the same SQLite file as the event store, keyed `(strategy_id, parameter_set_hash)` | AC-13 |
| 1h | Scanner: stop the 30 bar buffer truncating the warmup window, and stop an un warmed indicator evaluating as zero. `max_bars` is derived from the longest declared indicator period plus margin, and an un warmed indicator returns no match rather than `0.0` | AC-4 |
| 1m | `boot()` calls `FeedRecoveryCoordinator.recover()` and refuses `READY` if it does not return ok, so a cold boot reaches READY deliberately rather than by accident. Negative control: the five minute cold boot test. **This is promoted out of the first draft's Follow-up list because Wave 2's gate is a live paper run, which is unachievable while every order is rejected with `feed_not_ready`** | AC-20 |

Wave 1, second batch, after 1a to 1h: sizing, fees, protective realism and parity. 1i and 1j are sequenced after 1f because all three change `Signal` or `Fill`; 1k and 1l depend on all of them.

| Lane | Task | Satisfies |
|---|---|---|
| 1i | `Signal.quantity` plus a `PositionSizer` that clamps to lot size, tick size and max notional | AC-5 |
| 1j | Fees product aware: intraday and delivery rate tables required; `MARGIN`, `MTF` and `COVER_ORDER` **refuse the order at submit** until a schedule exists, rather than being charged a silent zero or the wrong schedule. Flip both the `AppConfig` default and the `TRADEX_FEES_ENABLED` default to true, and **delete** the route's `body["fees"]` switch rather than defaulting it, because a caller must not be able to switch costs off in a run whose output is compared against the promotion bar | AC-6 |
| 1k | Protective exit gap model and corporate actions in the backtest | AC-19 |
| 1l | A parity suite proving the backtest and the live path agree on fills, positions and profit and loss, run on a session captured by lane 2g | none, wave gate |

Wave 1 gate: a strategy run end to end produces a validation report whose numbers a reviewer can
reproduce, and no metric can be flattering by construction.

### Wave 2, paper path and the promotion gate

Entry: Wave 1 closed.

| Lane | Task | Satisfies |
|---|---|---|
| 2a | Strategy lifecycle enforcement against the seven existing states (CREATED, STARTING, RUNNING, PAUSED, STOPPING, STOPPED, FAILED): unregister disposes that strategy's subscriptions, per strategy state isolation. `STOPPED` takes effect **synchronously**, not on the next event, because gating delivery only is the current defect. It gates delivery, disposes subscriptions and cancels working orders with UNKNOWN aware cancel all, and it **leaves open positions and their protective legs alone**, since flattening is a separate explicit command. `PAUSED` gates delivery only and keeps subscriptions | AC-8 |
| 2b | `strategy_id` on `Order`, `Fill` and `Position`, stamped by the engine, with per strategy profit and loss | AC-9 |
| 2c | Wire `RiskBudget` through config, keying the lookup on `request.strategy_id` rather than parsing the `name@version` string out of `tag`, and add per strategy capital and drawdown baselines that survive a restart. The daily loss and drawdown guards read a mark that is either fresh or explicitly stale flagged: a fill no longer resets `unrealized_pnl` to zero, and a stale mark fails the guard closed rather than reading a reset zero. Negative control: a position whose last mark is ten minutes old and fully under water | AC-9, AC-10 |
| 2d | The slippage model fill path beside the existing book matching, selectable per run. **Book matching is the oracle.** On a session captured by 2g, both paths must produce the same order count, the same terminal position per instrument to the lot, and a net profit and loss within **0.05% of the oracle's absolute net profit and loss**. Comparison is on round trips and terminal position, not fill by fill, because one path books one fill per partial and the other one fill per order. Unmatched orders are excluded from the profit and loss comparison and must be listed in the test report with a reason. Negative control: a deliberately wrong slippage constant must fail the tolerance | AC-14 |
| 2e | `GET /api/charts/safety` per AC-17, with the derived severity rule, and the interface surface that shows promotion status per strategy as promotable, not evaluated or not promotable with the failure reasons | AC-17 |
| 2f | Fail closed on the enumerated limit set at boot, in paper as well as live, per the data model. Additionally: call the dead `_make_order_gate()` from `boot()` and bind it to `AppConfig.live_orders_enabled`, so the documented "one config flip stops all order flow" control actually exists; and replace `RiskManager._increases_exposure`, a net test, with a per side gross test so the mark, daily loss and drawdown gates evaluate the **resulting** position. Negative controls: booting with a limit unset refuses boot, booting with the gate flag false refuses every submit, and a sell of 150 against a long 100 is classified as increasing exposure | AC-10 |
| 2g | A JSONL tick recorder (`runtime/src/tradex_runtime/tape_recorder.py`) writing `{broker, symbol, ts, price, qty}` per quote, gated by `TRADEX_RECORD_TAPE=1`, flushed on close, with a reader that replays a file through both fill paths. **This is the prerequisite AC-14 and lane 1l depend on, and no such recorder exists today.** It also makes the Wave 7 tape a first slice rather than a later rebuild | AC-14 |
| 2h | `CashLedger` takes a lock, and `require_debit` plus the position booking plus the fee debit collapse into one `apply_fill` that holds it for the whole sequence, with every fill routed through it. Negative control: the review's missing test, N threads submitting through a `Barrier` to one engine, asserting no fill is lost and no concurrent submit both passes the affordability check | AC-21 |

Wave 2 gate: a strategy runs live paper, stops cleanly, and its report decides promotion.

### Wave 3, feed correctness

Entry: Wave 2 closed. Paper uses live quotes, so this is blocking, not deferred.

| Lane | Task | Satisfies |
|---|---|---|
| 3a | Websocket heartbeat and a frame age watchdog on both brokers: `ping_interval=20, ping_timeout=20` on each `websockets.sync.connect`; frame age over 30 s on quotes or 60 s on the order stream marks the feed DEGRADED. Wire the stale monitor to the order stream, not only to quotes, since a silently dead order socket is the one that leaves a resting order believed live | AC-12 |
| 3b | Re read orders and positions from the venue after a reconnect, and after the 10th attempt back off from 5 s to a 300 s ceiling retrying **indefinitely** rather than giving up silently for the rest of the session | AC-12 |
| 3c | Late tick handling with a **60 second** revision window, and a rule that resolves the tension with AC-1: a bar no fill has been booked against may be reopened and revised; a bar that already has a booked fill is **never** revised, because a strategy acted on the original. Such a late tick is counted in `feed_late_ticks_dropped_total` and logged. Revision is in memory only; the lake stays append only and a revised bar is written by `reconcile_bars.py`, not by the feed | AC-11 |
| 3d | Per symbol cadence liveness, since neither wire format carries a sequence number and a dropped frame is therefore indistinguishable from an illiquid minute. A symbol needs at least one tick in any 3 minute window during session hours; a symbol that `master_lifecycle` reports halted or delisted is exempted, and the exemption is counted in `feed_liveness_exempt_total` so it cannot become a silent blind spot | AC-12 |
| 3e | Parquet predicate pushdown and column projection, multiple row groups per file with a timestamp sort order so statistics can prune, compression and dictionary encoding. Measured by the review's own figure: a single symbol single month read from 16.8 ms to a target of 2 ms | none, performance |
| 3f | Corrupt file quarantine: move the file to `data/quarantine/<mtime>-<symbol>.parquet`, emit `datalake_quarantine_total{symbol,file}`, and return an empty series for the affected symbol instead of letting `ArrowInvalid` reach every call site | AC-15 |
| 3g | Process level indicator tail cache keyed by `(instrument, timeframe)` rather than per connection, and incremental compute that warms from the previous bar, removing the full 400 bar recompute and the per closed bar parquet read from the event loop | none, wave gate |
| 3h | Split bus publish from dispatch onto a dedicated pipeline thread, **and** remove `PlaceOrderCommand` from the bus entirely so `ExecutionEngine.submit()` is the only path to the venue. Today the bus is public, so a strategy can publish an order command directly and skip the whole gate order in Flow C, including the protective pair | AC-12, AC-22 |
| 3i | Scanner portfolio context, a rate limit, a result cache, and a bump `max_bars` so a long period indicator is not silently truncated | AC-13 |

Wave 3 gate: a dead socket is detected and recovered inside a bounded time, and a scanner over 500
symbols does not degrade the feed.

### Wave 4, package consolidation and event store hardening

Entry: Wave 3 closed. This wave does **not** run concurrently with the rest of the programme. An
earlier draft of this spec claimed it could, on the grounds that the lanes touched disjoint trees;
they do not, because moving tests and deleting modules collides with the code other waves are
editing.

The package extraction this wave used to own is **already done** in `b941da2` and its
predecessors. What remains is consolidation from 17 workspace members to the target shape, plus
the event store work that the Target data model requires and that no earlier wave owns.

**Target package map.** The first draft of this spec left four of the 17 members unassigned. The
complete map:

| Target | Members | Note |
|---|---|---|
| `tradex_domain` | `domain` | Unchanged. Zero internal dependencies, and it keeps zero |
| `tradex_config` | `config` | **Stays a separate leaf.** Every package may import it and it imports none of them. `domain` depends on it, so folding it into `core` would either break that or force a split |
| `tradex_core` | `execution`, `application`, `reactive`, `observability`, `research`, `analytics` | Research and analytics are pure computation over domain types, so they belong inward of the adapters |
| `tradex_ingest` | `brokers`, `market_data` | Folding these together makes the market data to broker edge intra package, so it stops being a cross package layering claim |
| `tradex_app` | `runtime`, `interfaces`, `strategy`, `replay` | Hosts the one legitimate composition root |
| exempt | `services/duckdb-analytics` | Stays a separate workspace member and is exempt from the package boundary test. Stated in that test's docstring so the exemption is deliberate rather than a hole |

| Lane | Task | Satisfies |
|---|---|---|
| 4a | Create the target `src/` trees plus the config leaf, moving code in the map above. Runs first, because every later lane lands in these trees | AC-16 |
| 4b | Move `trading/tests/**` and the root `tests/` tree into the packages that own them, in one pass, after 4a. The root `tests/` tree holds `test_import_boundaries.py`, which is this wave's own gate instrument, so its destination is named explicitly | AC-16 |
| 4c | Event store: `stream_id = f'{broker_id}:{account_id}:{stream}'`, per event `schema_version` with a registered legacy decoder so history is never invalidated, a `prev_hash` chain verified on replay, `Snapshot` written on reaching `READY` and every 10,000 events, and the fill dedup index partitioned by account | AC-16, AC-17 |
| 4d | Delete the dead packages `operations` and `persistence`, the dead `http_response.py`, `.kilo/`, `poc/`, and the 1,112 lines of dead frontend. Move `apps/top_gainers` to its own repository rather than deleting it, since it is a working dashboard | AC-16 |
| 4e | Give the remaining orphan trees an owner: `trading/scripts/` becomes the single scripts directory, and `trading/data/`, `trading/runtime/` and `trading/pyproject.toml` are deleted, since `CLAUDE.md` already states `trading/data/` is not a data root and nothing reads it | AC-16 |
| 4f | Fix `CLAUDE.md`, which names a frontend file that does not exist and contradicts the backtest behaviour on protective exits | none, documentation |

Wave 4 gate: the boundary test passes on the target graph with no allowlist and no string edge it
cannot see; `pyproject.toml` `testpaths`, `pythonpath`, `uv.workspace.members` and `[tool.uv.sources]`
name only the target packages plus the exempt member; every orphan tree has an owner.

### Wave 5, operational floor

Entry: Wave 4 closed. Not blocking for paper, blocking for anything unattended.

| Lane | Task | Satisfies |
|---|---|---|
| 5a | Make the metrics honest first, because the catalog's own thresholds are currently uncomputable: implement Prometheus histogram buckets on `_Histogram` (default `[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300]`) so `p95` and `p99` rows become computable, and index counter, gauge and histogram values by sorted label tuple instead of executing `del labels` and discarding it. Label rows like `orders.rejection_reason.<code>` cannot otherwise exist | AC-17 |
| 5b | `deploy/prometheus-rules.yml` with an explicit **number** for every catalog row, since the catalog text is `p99 > SLO`, `spike` and `sustained rate` rather than thresholds, plus `deploy/alertmanager.yml` with a delivery route. `orders.submit_latency_seconds` and `orders.process_latency_seconds` get p50 50 ms, p95 250 ms, p99 750 ms, and `datalake_quarantine_total` joins the alert set | AC-17 |
| 5c | Turn on the structured logging that already exists, plus a trace id from websocket tick through signal to order to venue | AC-17 |
| 5d | Secret file permissions, asserted at boot and failing closed | AC-17 |
| 5e | A reconciliation monitor on the stale feed monitor pattern, and adopting venue positions rather than only reporting drift | AC-17 |
| 5f | Deploy artifacts with a probe block, and the seven missing runbooks | AC-17 |

### Wave 6, gated on the promotion bar

Entry: a strategy has cleared the promotion bar and Wave 5 is closed. Not scheduled.

The money path work from the review belongs here: margin and leverage model, gross short
exposure cap, per symbol and sector concentration limits, live corporate actions, the account
dimension in the order management store, request signing, removal of the API key from the root
route, and the startup cash reconciliation that currently compares settled cash against available
margin.

### Wave 7, gated, designed only where it constrains earlier waves

Not scheduled. The socket sharding, the multi process split, the market data tape and the read
model all live here. The only thing earlier waves owe this wave is that the event store is
account scoped and versioned from Wave 4, and that the bus has a real dispatch thread from Wave 3,
so neither has to be rebuilt to accommodate it.

**Advisory on parallel agents.** Waves are designed so that lanes rarely contend, and no lane is
given a file ownership rule as you chose. Three places still carry real contention risk and are
worth sequencing by hand. Wave 1 lanes 1a, 1b, 1i and 1k all touch the backtest engine, so run 1a
and 1b as one agent before 1i. Wave 4 lane 4a must finish before 4b, because the tests have to land
in trees that exist. And Wave 3 lane 3d edits the feed liveness path while 3a edits the heartbeat on
the same brokers, so run those two as one agent.

## Consequences

**Positive**:
- A strategy is chosen on numbers that cannot flatter you, and the promotion decision is an
  assertion rather than an argument.
- One plan replaces five, so nobody reads the wrong one.
- Parallel agents have a defined entry condition per wave and a gate to pass, so a wave cannot
  half land.
- The enforced dependency graph becomes the real one, so future architecture decisions are made
  against a graph that is true.
- Live money work is scheduled against evidence rather than optimism.

**Negative / tradeoffs**:
- The paper first target delays every hardening item in the money path by at least two waves. A
  defect in sizing or fees is not fixed until Wave 1, and live specific risk is not fixed until
  Wave 6.
- Wave 4 is large, mechanical, and touches 53,800 lines of tests. It will produce a large diff
  with little behaviour change, which makes review harder and rollback coarser.
- Not writing child specs up front means each wave's detailed spec is a fresh piece of work, and
  some design detail will be decided during the wave rather than before it.
- No file ownership rule on lanes means the contention noted above is managed by hand every time.
- A single process on one machine is a deliberate ceiling. When symbol count or strategy count
  forces Wave 7, that work lands late and against a system that has grown in the meantime.

**Neutral**:
- The stack is unchanged, so no new dependency enters the tree through this programme.
- The data model changes are all additive; no existing column or type is removed, so every wave is
  independently revertable.
- `docs/specs/remediation-and-hardening.md` stays In Progress and is unaffected; it owns the
  quality gate, which Wave 0c extends rather than replaces.

## Follow-up

- [ ] Cut a child spec when Wave 0 starts, carrying forward the still valid detail from the five
      superseded plans so it is not lost with the status line.
- [ ] Decide whether `apps/top_gainers` is deleted or moved to its own repository; Wave 4d assumes
      moved.
- [ ] Re open the package layout decision if Wave 4 reveals a seam that does not fit the target
      package shape. The `config` leaf is the one to watch, since `domain` depends on it.
- [ ] Confirm the two venue ceilings against the current broker documentation before lane 3a
      lands: Dhan's NSE 1 minute series stops at 15:14, so the 15:30 close is clock driven there,
      and Upstox serves the full session. Both were measured, not read from a vendor contract.
- [ ] Decide whether the market data tape grows into the Wave 7 store or stays a JSONL research
      artifact. Lane 2g deliberately builds the smallest version that unblocks AC-14.

## Rationale

The decision record, the five superseded plans reconciled against the 13 panel review, and the
options considered are in [rationale.md](rationale.md).
