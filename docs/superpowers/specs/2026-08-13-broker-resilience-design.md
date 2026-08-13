# Re-add Broker Resilience Stack — Design

Date: 2026-08-13
Status: Approved

## Context

The multi-agent cleanup pass deleted the broker resilience stack
(`common/rate_limit.py`, `common/retry.py`, `common/circuit_breaker.py`,
`common/resilience.py`, ~1,029 lines) because it was verified test-only at
that time. Production always used `FetchResiliencePipeline` — a thin adapter
routing through an injected `fetch` with NO rate limiting, retry, or circuit
breaking.

That deletion left a real operational gap: the live path and the datalake
backfill path (`ParallelHistoryFetcher`, `max_workers=4` fan-out) hit broker
APIs with no throttling. The fetcher's docstring still claims "Each broker's
existing rate limiter handles throttling" — now stale and misleading.

The user asked to re-add the resilience stack with proper planning: accurate
and needed, wired into production.

## Decisions (confirmed with user)

- **Full resilience stack** (rate limiter + safe retry + circuit breaker) — not
  a fetcher-only limiter.
- **Safe retry**: retry idempotent-safe methods (GET/HEAD/OPTIONS) only on
  transport failures + 5xx/429; NEVER auto-retry mutations (order submission) —
  those fail loud for reconciliation.
- **Restore rate tables as-is** from the deleted stack (Dhan historical 5/s cap
  10, orders 10/s; Upstox historical 5/s cap 10, orders 10/s; etc.) — documented
  to match broker API limits, never exercised live, kept verbatim.
- **Consolidated module**: one `common/resilience.py` instead of the old
  four-file split (less fragmentation).

## Architecture

### Single module `brokers/src/tradex_brokers/common/resilience.py`

1. **Throttling core** (restored from deleted `rate_limit.py`):
   - `RateLimitConfig` — frozen dataclass: `rate_per_second`, `capacity`,
     `min_interval`, `cooldown_seconds`
   - `TokenBucketRateLimiter` — thread-safe; `acquire(timeout)` blocks,
     `try_acquire()` non-blocking, `trigger_cooldown()` on 429, min-interval
     enforcement
   - `RollingWindowCounter` — sliding-window count (for `extra_windows` tables)
   - `MultiBucketRateLimiter` — per-category buckets + optional rolling windows
   - `DHAN_RATE_LIMITS`, `UPSTOX_RATE_LIMITS`, `PAPER_RATE_LIMITS`,
     `table_for_provider`, `limiter_from_table`, `limiter_for_provider`,
     `bucket_for_path`
2. **Circuit breaker** (restored from deleted `circuit_breaker.py`):
   - `CircuitBreaker` — closed/open/half-open states; trip after N consecutive
     failures, probe after cooldown, half-open single probe
3. **Safe retry** (restored from deleted `retry.py`):
   - Retry policy: only `GET`/`HEAD`/`OPTIONS`; max 1-2 attempts on 5xx/429/
     transport errors; never retry mutating methods
4. **Composite** (restored from deleted `resilience.py`):
   - `ResiliencePipeline` — `send(method, url, **kwargs)` composing
     rate_limit → circuit_breaker → retry → transport send; classifies the URL
     into a rate-limit bucket via `bucket_for_path`

## Wiring

- `build_provider_client` (`common/client_shared.py:89`): build a
  `ResiliencePipeline` around the injected `fetch`, choosing the provider's
  rate table by provider name (dhan/upstox/paper). This is the single seam —
  `DhanBroker.from_fetch`, `UpstoxBroker.from_fetch`, and the live path
  (`runtime/live.py` `_urllib_fetch`/`_curl_cffi_fetch`) all route through it.
- `ProviderHttpClient` keeps its `SendPipeline` protocol — `ResiliencePipeline`
  satisfies it (its `send` signature matches).
- `ParallelHistoryFetcher`: accept an optional shared limiter (or build one via
  `limiter_for_provider`) so its fan-out respects the `historical` bucket —
  this fixes the flagged backfill-throttling gap. Update the stale docstring
  line ("Each broker's existing rate limiter handles throttling") to reflect the
  now-true behavior.

## Data flow

```
runtime/live.py fetch (urllib / curl_cffi)
  → build_provider_client → ResiliencePipeline (provider rate table)
  → TokenBucketRateLimiter.acquire("historical"|...)  (blocks, min_interval, 429 cooldown)
  → CircuitBreaker.check()  (fail fast when open)
  → safe retry (GET/HEAD/OPTIONS only, 5xx/429/transport)
  → transport fetch → ProviderHttpClient.send → broker adapter → back
```

## Error handling

- 429 response → `trigger_cooldown` on the bucket; circuit breaker counts it as
  a failure.
- Circuit open → immediate `RateLimitError`/`CircuitOpenError` (fail fast, no
  silent hang; backfill tools surface it via existing error paths).
- Mutation retry → never; a 5xx/timeout on a mutation raises so reconciliation
  (`get_order` keyed by correlation id) resolves the outcome.
- The composite must not break the auth-retry path in `ProviderHttpClient`
  (401/403 handling stays in the client, unchanged).

## Testing

- Restore adapted versions of the deleted tests into one file
  `brokers/tests/common/test_resilience.py`:
  - token-bucket refill/burst/cooldown/min-interval
  - multi-bucket `bucket_for_path` classification
  - circuit breaker trip/open/half-open recovery
  - safe retry: retries GET on 5xx/transport, never retries POST
  - composite pipeline ordering (rate limit before send)
- New test: `ParallelHistoryFetcher` fan-out respects the historical bucket
  (the flagged gap) — a burst of N symbols at max_workers=N still throttles to
  the historical rate, or is testable by injecting a slow/deterministic limiter.

## Files

- **Create:** `brokers/src/tradex_brokers/common/resilience.py`,
  `brokers/tests/common/test_resilience.py`
- **Modify:** `common/client_shared.py` (wire pipeline in
  `build_provider_client`), `common/provider_client.py` (only if the
  `SendPipeline` annotation needs adjustment), `common/__init__.py` (exports),
  `datalake/parallel_fetcher.py` (shared limiter + stale comment fix),
  possibly `runtime/live.py` (pass provider name to the builder)
- **Not touching:** auth/token_lifecycle/totp, ws_decoder, proto, tick_parser,
  execution engine, fill sources, SDK services.

## Non-Goals

- No retry of mutations (explicit safety constraint).
- No new dependencies — all stdlib (`threading`, `time`, `collections.deque`).
- No change to the public `BrokerAdapter`/`ExtensionAdapter` protocol surface.
- No behavior change to auth-retry (401/403) — that stays in ProviderHttpClient.

## Success Criteria

- All broker adapter tests pass (existing + restored resilience tests).
- Full suite: `python -m pytest domain/tests brokers/tests trading/tests -q`
  green.
- `ParallelHistoryFetcher` with a fan-out burst stays within the historical
  rate bucket (new test proves it).
- Live `boot()` path builds a provider client whose pipeline is a
  `ResiliencePipeline` (not the bare fetch adapter).
- `ruff check` and `mypy` (domain) clean.
- `graphify update .` run after merge.
