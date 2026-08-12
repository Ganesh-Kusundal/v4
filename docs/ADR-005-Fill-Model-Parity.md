# ADR-005: Unified FillModel for Cross-Mode Fill Parity

## Status

Accepted — 2026-08-12

## Decision

Extract the price resolution, slippage, timestamp, and fill-construction rules
every execution path shared into one class, `FillModel`, at
`trading/src/tradex_trading/execution/fill_model.py:17`. All four fill sources
now subclass it and delegate their common logic there:

- `SimulatedFillSource` (backtest) — `fill_sources.py:64`
- `PaperFillSource` (paper) — `fill_sources.py:111`
- `BrokerFillSource` (live) — `fill_sources.py:159`
- `ReplayFillSource` (replay) — `fill_sources.py:203`

`FillModel` owns the parity contract:

- `resolve_fill_price` (`fill_model.py:29`) picks the market reference price
  (LTP / next bar's open), falling back to the request's limit price then its
  trigger price. It raises `ValueError` on a non-positive price so a
  zero-priced fill can never silently corrupt P&L (`avg_price=0`), and applies
  the configured slippage model identically in every mode.
- `fill_timestamp` (`fill_model.py:60`) returns the request's market-data
  reference timestamp (or `now(UTC)`), giving reproducible event logs.
- `make_fill` (`fill_model.py:65`) builds a deterministic `Fill` from an order
  + resolved price.
- `restamp_fill` (`fill_model.py:86`) rebuilds a fill with a fresh order id,
  preserving its recorded identity — how `ReplayFillSource` matches a
  historical fill to the freshly minted order (`fill_sources.py:222`).

Each source keeps only its mode-specific concern: Paper's LTP cache lookup,
Broker's ACK-only submission, Replay's recorded-fill re-stamp.

The parity contract is enforced by `TestFillSourceParity`
(`trading/tests/parity/test_golden_mode_parity.py:450`): the same request +
market reference must resolve to the identical fill price in every price-
resolving source, with and without slippage (`test_all_fill_sources_agree_on_same_input`
at `:467`, `test_fill_source_parity_with_slippage` at `:477`). The golden
mode-parity suite additionally asserts exact fill-price + P&L equality across
backtest, replay, and paper (`TestGoldenReactiveParity`, `TestAccountingConvergence`).

## Context

Backtest, replay, paper, and live each built fills in their own copy of the
price logic. The parity review (HIGH-6b, CRITICAL-1) found the modes drifted:
a MARKET order with no reference price could fill at zero / nominal 1.0,
slippage was applied inconsistently, and identical input events produced
different fill prices (and therefore different P&L) across modes. A single
authoritative fill-resolution path is required so strategies backtested on
paper produce the same fills they would live.

## Consequences

### Positive

- One price-resolution rule everywhere: the same request + market reference
  resolves to the identical fill price in backtest, replay, paper, and live —
  net P&L agrees across modes (HIGH-6b acceptance).
- The non-positive-price `ValueError` guard is enforced in every mode, closing
  the zero/nominal-1.0 fill class of bugs (CRITICAL-2).
- Slippage models apply identically per mode, so backtested slippage assumptions
  hold live.
- Deterministic timestamps and deterministic `Fill` construction make event
  logs reproducible and replayable.
- `ReplayFillSource` reuses `restamp_fill` to match historical fills to fresh
  orders instead of duplicating fill construction.

### Negative

- Fill sources must inherit `FillModel`; a new execution mode must opt into the
  shared base or it silently falls back to its own price logic.
- `FillModel` is class-inheritance-based rather than a pure composition of
  protocols, coupling sources to the base class' public surface.
- The fallback chain (market price → limit → trigger) is implicit; a caller
  must read `resolve_fill_price` to know a price-less MARKET order with no
  trigger will fail loudly rather than fill somewhere.
- The parity gate only covers the price-resolving sources and the backtest/
  paper/replay trio; live-path parity is asserted by construction (shared base)
  but not exercised by a live broker in CI.
