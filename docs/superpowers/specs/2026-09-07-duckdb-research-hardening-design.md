# DuckDB Research Layer Hardening Design

**Date:** 2026-09-07

## Goal

Strengthen `services/duckdb-analytics` as an isolated offline research service without coupling it to the trading runtime. Research queries must be safe under concurrent access, visibly point-in-time bounded, reproducible against a dataset snapshot, and unable to silently consume truncated results.

## Non-goals

- Replacing `ScannerEngine` for live/session scanning.
- Moving DuckDB into the `tradex_trading` package.
- Building a general SQL parser or a distributed query service.
- Changing the canonical execution/backtest engine.

## Design

### 1. Connection isolation

The catalog will serialize access to its single in-memory DuckDB connection. Query timeout interruption and retry behavior will occur inside the same critical section, so one request cannot interrupt another request. Concurrent callers remain supported at the service boundary, but execution is deterministic and single-flight per catalog.

### 2. Verified point-in-time metadata

Callers will no longer be able to mark arbitrary SQL as point-in-time safe with a free boolean. Query results will expose explicit safety metadata. Scanner-generated queries, which require an `as_of` bound, will be the only normal path that reports verified point-in-time safety. Raw SQL remains marked unverified even when the SQL happens to contain a timestamp filter.

### 3. Strict research results

Query execution will support a strict completeness mode. When enabled, truncation raises an error instead of returning a partial result. Research studies and MCP scanner tools will use strict mode where complete universes are required. Existing interactive query behavior remains backward-compatible unless strict mode is requested.

### 4. Dataset provenance

The catalog will expose a deterministic dataset fingerprint derived from the configured Parquet inputs and relevant schema/configuration metadata. Query results will carry the fingerprint and the catalog’s timezone/session policy. This identifies the source snapshot used by a run without changing the lake’s storage format.

### 5. Correctness tests

Add tests for concurrent query execution, strict truncation, verified scanner safety versus raw query safety, and provenance propagation. Preserve the existing SQL guard, timeout, view, resampling, and indicator parity tests. The known research-script rolling-window issue will be tracked separately unless it can be fixed without changing the approved service boundary.

## Error handling

- Concurrent requests wait for the catalog execution lock.
- Timeout and interrupted-query errors are surfaced after cleanup.
- Strict truncation raises a typed query error containing the result cap.
- Missing or unreadable data continues to fail closed.
- Provenance computation must not silently return a misleading fingerprint; configuration identity is still reported if file metadata cannot be read.

## Acceptance criteria

1. No concurrent request can interrupt or reuse another request’s active connection execution.
2. Raw SQL results never claim verified point-in-time safety.
3. Scanner results report verified point-in-time safety and `as_of`.
4. Strict callers cannot proceed with truncated results.
5. Results expose a stable dataset/configuration fingerprint and session policy.
6. Existing DuckDB tests pass, plus new tests cover each changed behavior.
7. The service remains independent of `tradex_domain`, `tradex_brokers`, and `tradex_trading`.
