# 0001. Deepen parquet market history

**Date**: 2026-09-23
**Status**: Proposed

## Summary

This decision makes `ParquetMarketProvider` the one shared module for parquet backed market history. Chart history, chart backtests, indicator history, replay history, and paper quote fallback reads will stop rebuilding storage and candle conversion logic. Public responses and market data behavior stay unchanged.

## Context

> ⚠️ Premise note: This topic touches several callers, but they share one decision, the ownership of parquet market history. The spec keeps that decision focused and leaves replay state extraction and broker fallback policy for separate work.

The repository stores one minute bars in a parquet datalake and already has `ParquetMarketProvider.history()`, which returns `HistoricalSeries` and resamples through the domain series type. Several interface paths still construct `ParquetStorage`, read dataframes, call `candles_from_dataframe`, build `HistoricalSeries`, and resample independently.

The duplicated paths are in chart history and backtest handling, websocket indicator history, websocket replay, and the paper quote fallback. This spreads timestamp, empty range, and resampling policy across route modules. It also makes route tests carry storage concerns and makes a change in the market history convention difficult to apply consistently.

The system is a Python API and trading application with a strict dependency direction of domain, brokers, then trading. The datalake is local parquet storage. Broker history remains a separate fallback source and must not be pulled into the datalake provider.

## Requirements

**User stories**:

- As an API maintainer, I want every parquet history caller to use one provider seam so that storage and candle conversion behavior has one home.
- As a trading user, I want existing chart, indicator, replay, and paper quote behavior to remain unchanged while the code is reorganized.
- As a test maintainer, I want to test parquet history through `HistoricalSeries` so that route tests focus on transport behavior.

**Acceptance criteria**:

- **AC-1**: Chart datalake history and chart backtest candle loading obtain parquet history through `ParquetMarketProvider.history()` and no longer duplicate dataframe to candle conversion or series resampling.
- **AC-2**: Websocket indicator history and websocket replay obtain parquet candles through `ParquetMarketProvider.history()` while preserving their current tail, selected start bar, fallback window, and lifecycle behavior.
- **AC-3**: The paper quote fallback uses the shared parquet history seam for its stored close lookup, without changing quote values or fallback behavior.
- **AC-4**: `ParquetMarketProvider.history()` preserves the current contract for M1 storage, requested timeframe resampling, inclusive ranges, timezone normalization, missing symbols, empty stores, and returned `HistoricalSeries` metadata.
- **AC-5**: Existing REST and websocket payloads remain unchanged, including chart timestamps, bar fields, source fields, empty responses, replay acknowledgements, and indicator point shapes.
- **AC-6**: No migrated interface path imports `ParquetStorage`, `DATALAKE_ROOT`, or `candles_from_dataframe` solely to load parquet market history. Non history symbol and date range queries may retain a direct store dependency when required by their separate behavior.
- **AC-7**: Focused provider, chart, indicator, replay, and paper fallback tests pass, followed by the sanctioned full Python suite with no regression.

## Options considered

### Option 1: Deepen `ParquetMarketProvider`

Extend the existing provider seam and migrate all parquet history callers to its `history()` method. Keep route serialization, tail selection, replay filtering, and broker fallback outside the provider.

**Pros**:

- Reuses an existing module and contract.
- Concentrates storage conversion and resampling policy in one place.
- Keeps the datalake independent from broker adapters and HTTP details.
- Supports incremental migration with no public API change.

**Cons**:

- The provider must remain carefully scoped so it does not absorb transport shaping.
- Some callers still need direct store access for symbol lists or date ranges.

### Option 2: Add a separate `MarketHistory` facade

Create a new module above `ParquetMarketProvider` that owns the shared history operation, then migrate callers to the facade.

**Pros**:

- Gives the cross caller concept a new explicit name.
- Could later coordinate multiple storage sources.

**Cons**:

- Adds a second module around an already suitable interface.
- Risks a shallow wrapper that only forwards to the provider.
- Makes ownership less clear unless broker fallback is also moved, which is outside this decision.

### Option 3: Move all market history, including broker fallback, into one provider

Make one provider choose parquet first and broker second, returning one history result to callers.

**Pros**:

- Gives callers one complete history source.
- Centralizes source precedence and fallback decisions.

**Cons**:

- Couples the datalake module to broker adapters, sessions, and failure policy.
- Changes the existing chart fallback boundary.
- Expands this refactor into a provider orchestration decision that needs its own design.

## Decision

**Chosen option**: Option 1: Deepen `ParquetMarketProvider`

`ParquetMarketProvider.history(instrument, timeframe, start, end) -> HistoricalSeries` is the canonical parquet history seam. It remains parquet only. Interface callers perform only transport shaping or caller specific filtering after receiving the series. Broker fallback remains in the chart route.

## Rationale

The existing provider already owns the exact storage to `HistoricalSeries` path and is used by backtest and scanner code. Deepening it removes duplicated policy without introducing a new wrapper or crossing the dependency direction. Preserving the current contract reduces risk because the work is an architectural refactor rather than a data behavior change.

The provider must not return dictionaries, HTTP fields, websocket frames, or broker results. Those values belong to their transport or source owner. Direct store access remains allowed for non history operations such as listing symbols and reading a latest close when the operation is not a candle history request.

## Feature design

**Data model sketch**:

No persistent data model changes. The existing data source remains parquet under the configured datalake root. The in memory result is `HistoricalSeries` containing the requested instrument, requested timeframe, candles, start, and end.

**State transitions**:

None. `ParquetMarketProvider.history()` is a synchronous read operation with no persistent lifecycle.

**API surface**:

| Surface | Method | Key inputs | Key outputs | Auth | Key errors |
|---|---|---|---|---|---|
| `ParquetMarketProvider.history` | Python method | instrument, timeframe, start, end | `HistoricalSeries` or an empty series | None, local process operation | Preserve existing store errors and empty result behavior |
| Chart history route | HTTP GET | exchange, symbol, interval, optional UTC window, limit | Existing chart payload with bars, source, timeframe, and last closed time | Existing route behavior | Existing 422 and fallback behavior |
| Chart backtest loading | Internal call | instrument, timeframe, start, end | Existing candle list | Existing route behavior | Existing empty window behavior |
| Websocket indicator history | Internal call | instrument, timeframe, rolling history window | Existing serialized bar list | Existing websocket behavior | Datalake failure still degrades to live only |
| Websocket replay history | Websocket command | instrument, interval, minutes, optional start time | Existing replay acknowledgements and synthetic tick stream | Existing websocket behavior | Existing no history and selected start errors |

**Value sourcing**:

| Action | Value produced or displayed | Source |
|---|---|---|
| Provider symbol selection | Parquet partition symbol | `instrument.symbol`, or the existing instrument id fallback |
| Provider M1 candles | OHLCV candles | `ParquetStorage.read` rows and `candles_from_dataframe` |
| Provider requested timeframe | Resampled candles | `HistoricalSeries.resample(timeframe)` |
| Empty provider result | Empty candle list and metadata | No rows returned by `ParquetStorage.read` |
| Chart bars | Existing `time`, OHLCV fields | Provider candles, route IST to UTC conversion, existing serializer |
| Chart source | `datalake`, `broker`, or `none` | Provider result emptiness and existing broker fallback |
| Chart last closed time | Latest serialized bar time | Existing chart serialization of provider or broker series |
| Indicator bars | Tail window | Provider series and existing tail limit logic |
| Replay candles | Replay input candles | Provider history window, then existing selected start filter |
| Paper fallback quote | Latest stored close | Existing parquet date range and read logic, or the shared provider history result where the change can preserve the exact latest close lookup |

**Key invariants**:

- The provider remains parquet only and does not import broker adapters or session objects.
- The provider returns `HistoricalSeries`, never transport dictionaries or websocket frames.
- M1 parquet rows are converted through the existing shared candle builder.
- Non M1 requests use `HistoricalSeries.resample()` with existing semantics.
- Missing symbols and empty windows return empty series rather than raising a new application error.
- Existing timezone and timestamp conventions remain unchanged.
- Chart broker fallback remains outside the provider and runs when the datalake result has no bars.
- Route specific limits, tail selection, replay start filtering, and serialization remain in their callers.
- Symbol listing and other non history store operations are not forced through `history()`.

**Security model**:

No change. The provider is an internal local read seam. Existing HTTP and websocket authorization and session binding remain unchanged. The datalake contains market data, not user private records, and this refactor adds no new access path.

**Configuration required**:

No new environment variables. Existing `TRADEX_DATALAKE_ROOT` resolution and `DATALAKE_ROOT` behavior remain unchanged.

**Critical test scenarios**:

- Provider returns M1 candles and preserves metadata, verifies **AC-4**.
- Provider resamples to D1 and preserves the current candle values, verifies **AC-4**.
- Missing symbol and empty store return empty series, verifies **AC-4** and **AC-5**.
- Chart history matches direct `HistoricalSeries.resample()` output, verifies **AC-1**, **AC-4**, and **AC-5**.
- Chart history still falls back to broker data only when parquet is empty, verifies **AC-1** and **AC-5**.
- Indicator history preserves its tail and live bar replacement, verifies **AC-2** and **AC-5**.
- Replay preserves selected start filtering, no history errors, and acknowledgements, verifies **AC-2** and **AC-5**.
- Paper quote fallback preserves the latest stored close behavior, verifies **AC-3**.
- Source inspection test or import boundary check confirms migrated history callers do not directly construct parquet history pipelines, verifies **AC-6**.
- Focused suites and the full sanctioned suite pass, verifies **AC-7**.

## Build plan

The project has no recorded scope build approach for this standalone repository. This plan uses a tracer bullet, meaning each step keeps the system behavior working while extending one seam through the next caller.

1. Add or strengthen provider contract tests for empty ranges, metadata, timeframe resampling, and instrument symbol selection. Keep the tests failing only for any missing contract needed by migration, satisfies **AC-4**.
2. Migrate chart datalake history and chart backtest loading to `ParquetMarketProvider`, preserving the existing serializer, cache, broker fallback, and response fields. Add or update chart tests, satisfies **AC-1**, **AC-4**, **AC-5**.
3. Migrate websocket indicator history to the provider and retain existing tail limits, live bar replacement, and degradation on datalake failure. Add focused tests for the caller contract, satisfies **AC-2**, **AC-5**.
4. Migrate websocket replay parquet loading to the provider while retaining its existing window selection, date range fallback, selected start filtering, synthetic tick flow, and acknowledgements. Add focused replay regression coverage, satisfies **AC-2**, **AC-5**.
5. Migrate the paper quote fallback latest close read to the shared parquet history seam only where the exact latest close semantics remain unchanged. If date range lookup is required for the existing fallback window, keep that narrow store query and document it as non history metadata access. Add a regression test, satisfies **AC-3** and **AC-5**.
6. Remove duplicated history conversion code and imports, then add a guard that migrated paths do not recreate the parquet history pipeline. Preserve direct store access only for non history operations, satisfies **AC-6**.
7. Run focused datalake and interface tests, then run `.venv/bin/python -m pytest domain/tests brokers/tests trading/tests tests -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q`, satisfies **AC-7**.

## Consequences

**Positive**:

- One deep module owns parquet history loading, candle conversion, and resampling.
- Route modules become easier to test through their transport contracts.
- Timestamp, empty result, and resampling policy have stronger locality.
- The refactor does not require a data migration or public API change.

**Negative / tradeoffs**:

- The provider becomes more central, so changes to its contract have a wider test impact.
- Some websocket code still needs local filtering and serialization, so not all data handling disappears from routes.
- A separate future decision is still needed if broker fallback should become a shared source orchestration seam.

**Neutral**:

- Symbol listing, date range inspection, and other non history store operations may continue to use `ParquetStorage` directly.
- The existing chart response cache remains in the route and is not redesigned here.
- Replay state extraction remains separate from this decision.

## Follow-up

- [ ] Consider a separate architecture decision for extracting replay lifecycle and pacing from the websocket handler after this history seam is complete.
- [ ] Consider a separate decision for a source orchestration seam if more callers need consistent parquet and broker fallback behavior.

## Migration plan

**Strategy**: strangler migration, one caller group at a time.

**Phases**:

1. Prove and preserve the provider contract with focused tests.
2. Migrate chart callers, then websocket indicator history, then replay, then the paper fallback.
3. Remove only duplicated history pipeline bodies after each caller is green.
4. Run source guard checks and the full sanctioned test suite.

**Rollback**: Revert the caller migration commit or the complete refactor series. No persistent data or public payload migration is required.

**Risks**: A caller may depend on subtle timezone, fallback window, or empty result behavior. Focused regression tests and one caller per migration phase limit the blast radius. The paper fallback is the highest risk because it currently uses a latest close lookup rather than a normal requested history window, so it must only be migrated if exact semantics remain demonstrable.
