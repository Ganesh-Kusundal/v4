# Findings ledger — pre-production architecture review

**Date** 2026-09-29 · **Status** current ledger for the programme ·
**Pinned at** `3a1797e` (entries re-derived), **F-43…F-47 and F-39/F-40 closed
in the tree at that pin**

This is the F-numbered ledger that both production-readiness designs cite. It was
never committed, which is why they pointed at a path that does not resolve
(`docs/reviews/principal-engineering-review-2026-09-29.md`); the programme spec
even said as much, describing the ledger as held in "review working notes".
Every entry below is therefore **re-derived from the tree at `3a1797e`**, not
copied from a lost document, so the anchors are the evidence and not a
transcription.

Two rules govern this file, and both were paid for:

- A row is only here if the named symbol is greppable. If you cannot grep the
  anchor, the row is wrong.
- A doc that names a Python path is load-bearing, and it rots. Re-derive before
  trusting it (`git grep -n "<name>"`).

## Why the source lives in `docs/audits/2026-09/`

The findings were produced by a review that was never written down, then
summarised into two designs under `docs/superpowers/specs/`. The programme's own
lane `0a` moves superseded root reviews into this directory, and
`docs/audits/2026-09/README.md` already describes itself as the place where
superseded review generations are kept "so that exactly one document is
current". A ledger with no home is a ledger that rots again.

## CRITICAL

| ID | Defect | Anchor | Fails silently because |
|---|---|---|---|
| F-01 | The reconcile-check record path is a dead chain — the seam calls `engine.record_reconciliation`, which `ExecutionEngine` does not have | `interfaces/src/tradex_interfaces/routes/safety.py` (`_record_check`); the method is owned by `runtime/src/tradex_runtime/session_impl.py:372` | Nothing raises. The method that should be called is absent, and `last_checked_at` derives only from durable events (`safety.py:596`), so the dashboard reads as checked when no check ran |
| F-05 | A contract fix whose suite left `contracts` red on a dirty tree | Phase 0 / W0, integrator lane | The failure mode this whole programme is chartered to remove |
| F-06 | Dead `_attach_event_store` in the deletion ledger | Phase 6 / W6 lane A | Unused code that still reads like the event-store path is alive |
| F-12 | `session.broker.submit_order` is an ungated venue door, bypassing the single order door | `brokers/src/tradex_brokers/base.py:143`, `:336-341`; slice fan-out `brokers/src/tradex_brokers/upstox/_orders.py:196` | The broker still accepts and fills the order. A strategy can trade without touching risk, idempotency or the kill switch |
| F-12a | Engine-side permit issuance — F-12, upstream half | Phase 0 / W0.5 lane A | Without the permit nothing downstream can be enforced |
| F-12b | Adapter wall must require the engine permit — F-12, downstream half | Phase 3 / W3 lane A, `brokers/src/tradex_brokers/base.py` | The adapter accepts an order with no permit, so the engine's audit trail has a hole |
| F-19 | `adjust_series` is broken on real `Candle` objects — reads `getattr(c, "open")`, the real field is `c.ohlc.open.value` | `market_data/src/tradex_market_data/corporate_actions.py` (`adjust_series`); real shape at `domain/src/tradex_domain/market.py:155-159` | A `MockCandle`-shaped test (`trading/tests/analytics/test_corporate_actions_enhanced.py:9`) supplies the attribute the broken code reads, so the suite is green against a shape production never sends |
| F-26 | Durable-before-mutate is advisory outside live — `_append` swallows store failures unless `require_durable_events` | `execution/src/tradex_execution/engine.py:329-364`; set only for live at `runtime/src/tradex_runtime/startup.py:560-564` | Paper and backtest lose events on a store fault and keep trading. Only live is fail-closed |
| F-39 | Bar aggregation is never wired in production — `MarketFeed` publishes only `Quote`/`Depth`, so `on_bar` never fires in paper/live | `runtime/src/tradex_runtime/market_feed.py:490-507`; `wire_bar_aggregation` at `runtime/src/tradex_runtime/bar_aggregator.py:1011` has zero production callers | The strategy engine is constructed, subscribes, and receives ticks. Nothing aggregates a bar, so no signal is ever computed. It looks like a quiet strategy, not a dead wire |
| F-40 | The production sizer is the placeholder — `ReactiveStrategyEngine` is built with no `sizer=`, so paper/live size at a fixed ~₹95k notional while backtest uses risk budgets | `runtime/src/tradex_runtime/startup.py:681`; placeholder at `strategy/src/tradex_strategy/core/sizing.py:237` (`unconfigured_sizer`); the intended factory is `sizing.py:172` (`account_sizer`) | Both paths run and both place orders. The backtest equity curve is a risk-budget run and the live book is a notional run, and nothing compares them |
| F-41 | Bracket single-ownership and emit-time position marking are not enforced | Phase 4 / W4 lane A | Two owners of one bracket's levels, or a level computed against a position that has not been marked yet |
| F-43 | Feed recovery counts missing bars but never gates on them — the docstring promises the feed "goes `HALTED` if the replay proves a hole larger than the configured allowance", and no allowance parameter exists | `runtime/src/tradex_runtime/feed_recovery.py:16-18` (the promise), `:139-161` (constructor, no `max_missing_bars`), `:216` (`missing` accumulated), `:352` (counted in metrics only) | A reconnect that replays 400 bars with a 300-bar hole returns `ok=True` and returns the feed to `READY`. The book is then missing 300 minutes of tape and keeps trading it. The counter moves, so an operator sees a number and no alarm |

## HIGH

| ID | Defect | Anchor | Fails silently because |
|---|---|---|---|
| F-02 | `UNKNOWN` terminality in the order FSM | Phase 1 / W1 lane A | `UNKNOWN` means the venue's outcome is unknown. Treating it as terminal is what a duplicate live order looks like |
| F-03 | `SUBMITTED` is still in the status vocabulary | Phase 1 / W1 lane A | A status the venue never returns survives in the enum, so two spellings of one state exist |
| F-04 | Not all status writes go through `Order.transition_to` | Phase 1 / W1 lane A | A direct assignment bypasses the FSM's validation, so an illegal transition lands without complaint |
| F-10 | Drift vocabulary is not one vocabulary across `events.py` / `reconciliation.py` / `safety.py` | Phase 1 / W1 lane B | Three vocabularies mean a state that means one thing in the engine means another in the dashboard |
| F-13 | The kill switch does not cover modify | Phase 0 / W0.5 lane A | Submit and cancel are gated; a modify can still change a live order while the switch is tripped |
| F-20 | WebSocket gaps are not forced into a reconcile on reconnect | Phase 3 / W3 lane B | The socket looks healthy while the tape underneath it lost bars |
| F-21 | Point-in-time strict mode is not enforced | Phase 3 / W3 lane C | A backtest can read a corporate action that had not happened yet, and print a profit nobody could have taken |
| F-22 | Wall fix / override detection | Phase 3 / W3 lane A | An override through the venue door is indistinguishable from a sanctioned one |
| F-23 | Uncertain-submission tracker durability | Phase 3 / W3 lane B | After a restart an in-flight order's outcome is forgotten, so it can be resubmitted |
| F-24 | Cancel priority over other order operations | Phase 3 / W3 lane A | A cancel issued during a burst can be overtaken, leaving exposure the operator believed closed |
| F-27 | Overfill is not clamped before the effect is applied | Phase 2 / W2 lane B | The book takes more than was ordered, and the extra is applied before anyone clamps it |
| F-28 | `DurableFillDedup` is not wired when a store is present — the in-memory LRU dies with the process | Phase 2 / W2 lane B | A broker resending a fill after a restart double-applies it |
| F-29 | Fill dedup policy is not one policy shared with recovery | Phase 2 / W2 lane B | The engine and recovery can disagree about what "already applied" means, so the log replays to a different book |
| F-32 | Modify pickling in a pickle-encoded event log | Phase 6 / W6 lane A | A modify is decoded by executing code from a file |
| F-34 | `configure_runtime_cache` is never called | Phase 0 / W0.5 lane B | A cache that is configured nowhere reads as configured |
| F-35 | Tri-state drift report is missing | Phase 0 / W0.5 lane A | Drift is reported as a binary, so "not checked" and "checked and clean" look identical |
| F-44 | `UNKNOWN` is rendered as `rejected` on the chart, so a broker row the process cannot interpret is displayed as a definitive venue rejection | `interfaces/src/tradex_interfaces/routes/chart.py:64-70` (`_ORDER_STATUS_MAP`) | The backend already carries the distinction and `/api/charts/safety` already ages `UNKNOWN` rows. The chart is the one surface that collapses it, and it does so on the surface an operator reads to decide whether to flatten a position |

## MEDIUM

| ID | Defect | Anchor | Fails silently because |
|---|---|---|---|
| F-45 | `pending_max_age_bars` defaults to `0` (no expiry) and the composition root never passes it, so the deferred-order max-age sweep is a documented no-op in production | `strategy/src/tradex_strategy/core/engine.py:75` (default), `runtime/src/tradex_runtime/startup.py:681` (no `pending_max_age_bars=`) | A deferred `next_open` order can sit in the queue indefinitely. The constructor validates `>= 0` and the docstring documents the sweep, so the feature reads as present while never running |
| F-45b | Deferred orders that expire are not counted and not surfaced, so live has no equivalent of backtest's `num_unfilled` | `strategy/src/tradex_strategy/core/engine.py` (no counter; `_flush_pending` at `:258` runs the sweep); `interfaces/src/tradex_interfaces/routes/safety.py` (`_strategy_rows`) would carry it | The backtest reports unfilled orders; the live session has no number to compare it against, so the parity gap is invisible |
| F-46 | `WorkspaceStore` opens a new connection per call, and `:memory:` therefore yields a *new empty database on every call* — contradicting its own docstring | `interfaces/src/tradex_interfaces/workspace_store.py:26-41` | The default server passes a real file path (`interfaces/src/tradex_interfaces/fastapi_app.py:202`), so the defect is latent. But the default and the docstring are both wrong, which is exactly the trap the repo's own lesson #2 warns about |
| F-47 | Live publishes `ErrorOccurred` and bumps a global `engine.errors.total`, while backtest re-raises and aborts the run — so a live order failure is not attributable to a strategy | `strategy/src/tradex_strategy/core/engine.py:248-256` (`_place`), docstring at `:235-244` | The operator sees a rising global counter with no strategy named. Backtest fails loudly; live keeps trading the next bar |

## Cross-workstream contract

Two overlaps that would otherwise put two lanes on one vocabulary in one wave.
Both are now moot for F-44 and F-45, which landed together in one sitting
rather than in parallel lanes — recorded here because the constraint still
applies to F-02/03/04, which are *not* yet done:

- **F-44 and F-02/03/04 all edit the status vocabulary.** F-44 is presentation
  only (the chart's trade-tier map); F-02/03/04 are the domain FSM and still
  open. When they land, the chart's status union must be re-checked against
  whatever the FSM does to `SUBMITTED` — F-03 deletes it, and this map still
  carries it on the `pending` tier.
- **F-45 and F-40 both edit `startup.py`.** They landed in one pass for
  exactly that reason. Per the programme's conflict control, one lane owns
  `startup.py` per wave, and the remaining W0.5 work (F-34) must respect it.

## Closed in the tree at `3a1797e`

Each of these was a correct component that production never called, or a
documented behaviour that had no code behind it. Every control below was
demonstrated to fail when its fix was removed — that is the standard this repo
already holds, and the reason these count as closed rather than as edited.

| ID | Closed by | Control (fails when the fix is removed) |
|---|---|---|
| F-39 | `wire_bar_aggregation` called from the composition root (`startup.py`), for every strategy-wiring mode | `trading/tests/runtime/test_boot_bar_aggregation_wiring.py::test_boot_wires_a_bar_aggregator` |
| F-40 | One `account_sizer` built from `cfg.risk.effective_limits()` and passed as `sizer=`, with the live balance as `equity_source` | `…::test_boot_builds_the_account_sizer`, `…::test_the_configured_notional_ceiling_reaches_the_sizer` |
| F-43 | `max_missing_bars` on `FeedRecoveryCoordinator`, gated in `recover()`, threaded from `FeedConfig.max_missing_bars` | `trading/tests/runtime/test_feed_recovery.py::test_hole_over_allowance_fails_closed_and_halts` (plus the at-allowance positive control) |
| F-44 | `UNKNOWN` maps to its own chart tier instead of `rejected` | `trading/tests/interface/test_order_status_mapping.py::test_unknown_is_not_rendered_as_rejected` |
| F-45 | `pending_max_age_bars` sourced from `ExecutionConfig` with a non-zero default; expiries counted per strategy | `trading/tests/runtime/test_medium_batch_f45_f46_f47.py::test_pending_max_age_is_wired_at_boot`, `…::test_a_deferred_order_that_ages_out_is_cancelled_and_counted` |
| F-46 | `WorkspaceStore` holds one connection for its lifetime and closes it | `…::test_memory_store_round_trips_a_layout` — a per-call connection makes `:memory:` a *new empty database* every call |
| F-47 | Per-strategy `order_failures` / `orders_expired` on the engine, surfaced on `StrategySafety` | `…::test_order_failures_are_attributed_to_the_strategy` |

Two behavioural changes an operator should know about, both consequences rather
than accidents:

- **F-40 changes live and paper order quantities.** They are now sized by the
  risk policy instead of a fixed ~₹95,000 notional. This is the point of the
  finding, but it is a real change to what paper places. Run paper first.
- **F-43 can halt a feed that previously recovered.** The default allowance is
  20 bars, chosen to sit above Dhan's structural 15-minute session-tail hole
  (its NSE 1m series stops at 15:14). Lower it and live recovery halts on the
  known tail; that is the intended lever, not a bug.

## Not in this ledger

The execution spine itself is not re-litigated here: single order door (AC-22),
one cash operation under one lock (AC-21), durable-before-mutate, and
fail-safe unknown-outcome classification. Every finding above is a seam around
it, and this programme closes the seams without redesigning the spine.

The `pickle` event log and the lack of dependency scanning are the two open
items outside the programme's scope; `docs/audits/2026-09/README.md` already
records both with the reasoning for leaving them out.
