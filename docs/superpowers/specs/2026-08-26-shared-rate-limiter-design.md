# Shared Rate-Limiter Between Fetcher and HTTP Client — Design

**Date:** 2026-08-26
**Status:** Approved (awaiting plan)
**Scope:** Trading + Brokers packages

## Problem

`ParallelHistoryFetcher` builds its own `MultiBucketRateLimiter` per broker via
`limiter_for_provider(name)` at construction time
(`trading/src/tradex_trading/datalake/parallel_fetcher.py:152-156`). The HTTP
client inside each broker builds a *separate* `MultiBucketRateLimiter` via
`limiter_for_provider(provider)` at
`brokers/src/tradex_brokers/common/client_shared.py:107`. The two never share
state.

Consequence: when Dhan returns 429, the HTTP client's limiter enters 130s
cooldown, but the fetcher's limiter keeps handing out tokens at 5/s. The
fetcher burns quota on requests that are already doomed, then the K-threshold
blacklist (the per-batch broker-health tracker we just shipped in
`2d5c8a6`) trips and the rest of the 20-symbol batch dies.

Reproducible today: `python3 trading/scripts/sync_today.py` reports
`0/20 succeeded` for every batch when the daily top-up runs against a Dhan
quota that is already drained. The K-threshold messages
(`dhan: blacklisted for batch after K distinct failures`) confirm the
behaviour: the fetcher thinks the broker is fine, makes 20 doomed calls, marks
3 of them as failed, blacklists Dhan for the rest of the batch, then the
failover broker (Upstox) has its own independent limiter and dies the same
way.

## Goal

One rate-limiter per broker. Both the fetcher and the HTTP client see the
same bucket, cooldown, and rolling-window counters. When the bucket is in
cooldown, the fetcher's `acquire()` fails fast instead of waiting 30s on a
known-broken broker.

## Architecture

```
DhanApiClient / UpstoxApiClient
└── self._pipeline : ResiliencePipeline
    └── self._rate_limiter : MultiBucketRateLimiter   ← single source of truth

DhanBroker / UpstoxBroker
├── super().__init__(..., transport=client, ...)
└── .rate_limiter : MultiBucketRateLimiter            ← NEW: proxy to transport

ParallelHistoryFetcher
├── brokers : dict[str, Broker]
└── self._limiters : dict[str, MultiBucketRateLimiter]   ← reads from broker.rate_limiter
                                                          (fallback for test doubles)
```

## Components

### 1. Broker exposes `.rate_limiter`

In `DhanBroker.__init__` and `UpstoxBroker.__init__`
(`brokers/src/tradex_brokers/dhan/adapter.py:58-71`,
`brokers/src/tradex_brokers/upstox/adapter.py:60-73`), after `super().__init__`,
add:

```python
self.rate_limiter = self._transport._pipeline._rate_limiter
```

(One line. Reads from the transport the broker already holds. No new
construction logic.)

For the `transport=None` case (the broker exists without an HTTP client,
e.g. unit tests of broker-only logic), `self._transport` is `None` and the
attribute read would fail. Guard:

```python
self.rate_limiter = (
    self._transport._pipeline._rate_limiter if self._transport is not None else None
)
```

A `None` rate limiter is the existing "no rate limiting" path; the fetcher
already handles this (see fallback in §3).

### 2. No change to the HTTP client

`client_shared.py:106-110` already builds the pipeline with
`limiter_for_provider(provider)`. The fetcher now reads the *same* limiter
through the broker. The `trigger_cooldown(bucket)` call at
`resilience.py:1187-1188` already mutates the correct instance.

### 3. Fetcher reads from broker

In `ParallelHistoryFetcher.__init__`
(`trading/src/tradex_trading/datalake/parallel_fetcher.py:138-157`), replace
the limiter construction:

```python
# OLD
self._limiters: dict[str, MultiBucketRateLimiter] = (
    {name: rate_limiter for name in brokers}
    if rate_limiter is not None
    else {name: limiter_for_provider(_provider_for(name)) for name in brokers}
)

# NEW
self._limiters = {}
for name, broker in brokers.items():
    if rate_limiter is not None:        # explicit override (kept for tests)
        self._limiters[name] = rate_limiter
        continue
    shared = getattr(broker, "rate_limiter", None)
    if shared is not None:              # production path: read from broker
        self._limiters[name] = shared
        continue
    # ponytail: test doubles / non-standard brokers — fall back to a
    # provider-tuned limiter so existing test isolation is preserved.
    self._limiters[name] = limiter_for_provider(_provider_for(name))
```

Three branches, in priority order: explicit override, broker-owned shared
limiter, per-broker fallback. Behaviour for test mocks
(`broker = MagicMock()`) is unchanged because the mock has no `rate_limiter`
attribute → falls through to the existing fallback. Behaviour for production
brokers changes: one limiter, not two.

### 4. Fail-fast on cooldown (no code change)

The fetcher's `acquire("historical", timeout=ACQUIRE_TIMEOUT_S)` already
returns `False` on timeout. Once the limiter is shared, the
`if not limiter.acquire(...): log.warning(...proceeding anyway)` paths at
`parallel_fetcher.py:253-257, 293-298, 322-329, 352-359` become meaningful:
the warning fires only when the broker is genuinely in cooldown (not when
the fetcher's phantom limiter is empty). No code change here — only the
*meaning* changes.

## Data flow (sync_today.py, post-fix)

1. `build_broker_from_env("dhan").connect()` →
   `DhanBroker` with `self.rate_limiter = limiter_for_provider("dhan")`.
2. `ParallelHistoryFetcher({"dhan": dhan, "upstox": upstox}, ...)` →
   reads `dhan.rate_limiter` and `upstox.rate_limiter`.
3. Fetcher `acquire("historical", timeout=30)` → same bucket the HTTP
   client uses.
4. Dhan returns 429 → HTTP client `trigger_cooldown("historical")` on the
   shared bucket. Cooldown = 130s.
5. Next fetcher `acquire("historical", timeout=30)` sees cooldown → bucket
   returns `False` immediately (cooldown_until > now, acquire short-circuits
   before the 200ms token wait). Fetcher logs the existing warning, calls
   the broker anyway, the HTTP layer returns 429 in <50ms, the fetcher
   records the failure on `_BrokerHealth`. No 30s wait.
6. K-threshold blacklist still trips after 3 distinct failures — same
   bounded fan-out guarantee, but the failures are now real (cooldown-driven)
   not phantom (token-bucket-only-driven).
7. Once cooldown expires (130s after the first 429), the next
   `acquire("historical", timeout=30)` succeeds against a fresh bucket.

## Error handling

- **Broker missing `.rate_limiter`** (test doubles, `transport=None`
  brokers, or any non-Dhan/Upstox adapter) → fetcher falls back to
  `limiter_for_provider(_provider_for(name))` and logs nothing. Today this
  fallback is already used for unknown provider names; we widen it to
  "missing attribute" too. No test isolation lost.
- **429 in HTTP client** → unchanged (`trigger_cooldown(bucket)` on the
  shared limiter, `resilience.py:1187-1188`).
- **Cooldown active at fetch time** → `acquire` returns `False`
  synchronously; fetcher logs the existing `WARNING` and proceeds. The
  existing 4 log sites stay (one per code path: primary windowed, primary
  unwindowed, failover windowed, failover unwindowed) — they are already
  distinct enough for log triage.
- **`transport=None` broker** → `broker.rate_limiter` is `None`; fetcher
  uses the per-broker fallback. No regression: a broker without an HTTP
  client can't make API calls anyway, so its limiter state is irrelevant.

## Testing

### New unit tests (`trading/tests/datalake/test_parallel_fetcher.py`)

1. **`test_fetcher_uses_broker_owned_limiter`** — construct a real
   `MultiBucketRateLimiter`, attach it to a mock broker as
   `broker.rate_limiter`, instantiate the fetcher, monkey-patch
   `MultiBucketRateLimiter.acquire` to assert it received the *same* object
   the broker held (id-equal).
2. **`test_cooldown_short_circuits_acquire`** — attach a limiter, call
   `limiter.trigger_cooldown("historical")`, time the fetcher's
   `acquire("historical", timeout=30)`. Assert `elapsed < 1.0s` (cooldown
   is 130s, so 30s timeout never fires; the bucket returns False in <1s).
3. **`test_k_threshold_works_against_shared_limiter`** (regression) — reuse
   the existing `TestBrokerHealth` test bodies but with a real shared
   limiter instead of the per-broker fallback. Confirms the
   `2d5c8a6`-shipped K-threshold logic still works.

### Updated tests

- **`test_failover_throttled_by_failover_broker_limiter`** — already exists
  in `trading/tests/datalake/test_parallel_fetcher.py:299-326`. The
  monkey-patched `UPSTOX_RATE_LIMITS` table no longer affects the fetcher
  (the limiter is read from the broker, not built per-call). Either (a)
  patch `broker.rate_limiter` to the slow limiter before constructing the
  fetcher, or (b) skip the patch and assert the real 50/s Upstox bucket
  throttles correctly. Pick (a) to keep the assertion deterministic.

### New broker test

- **`DhanBroker` / `UpstoxBroker` exposes `.rate_limiter`** — minimal
  smoke: `DhanBroker.from_fetch(...)` returns an instance where
  `broker.rate_limiter` is the same `MultiBucketRateLimiter` as
  `broker._transport._pipeline._rate_limiter` (id-equal).

### Manual smoke (real broker, no test)

- `python3 trading/scripts/sync_today.py` against a drained Dhan quota.
  Pre-fix: `0/20 succeeded` per batch, K-threshold messages dominate the
  log. Post-fix: fetcher's `WARNING rate-limit gate timed out` fires
  per-symbol in <1s, no 30s waits, K-threshold still trips, but the
  failures are real (cooldown-driven) and the run completes faster.
  Compare wall-time vs. pre-fix.

### Benchmark

- Re-run `benchmarks/bench_parallel_fetcher.py` after the fix. The
  latency-bound scenario (which was already 1.5-1.9x faster with split
  routing) should be unchanged — the limiter change is correctness, not
  throughput. The quota-bound scenario should show fewer 30s waits
  against a forced-drained broker.

## File changes

| File | Change |
|---|---|
| `brokers/src/tradex_brokers/dhan/adapter.py` | +1 line in `__init__` |
| `brokers/src/tradex_brokers/upstox/adapter.py` | +1 line in `__init__` |
| `trading/src/tradex_trading/datalake/parallel_fetcher.py` | rewrite `__init__` limiter construction (3 branches instead of 2) |
| `trading/tests/datalake/test_parallel_fetcher.py` | 3 new tests, 1 updated test |
| `brokers/tests/common/test_resilience.py` (or new `test_brokers.py`) | 1 new smoke test |

Estimated diff: ~80 lines including tests, ~15 lines of production code.

## Skipped (ponytail)

- **Process-global `limiter_for_provider` cache.** Would make the HTTP
  client and fetcher see the same limiter without per-broker ownership, but
  kills test isolation (a test that monkey-patches `UPSTOX_RATE_LIMITS`
  would affect every other test in the run). The broker-owned attribute is
  the right unit of sharing.
- **Expose `.rate_limiter` on the `Broker` base class** (or ABC). The two
  production brokers are the only ones that hit real APIs. Paper and mocks
  stay duck-typed; the fetcher's `getattr` fallback handles them.
- **Remove the `rate_limiter=...` constructor arg on the fetcher.** Kept as
  an explicit override for tests that want a fully synthetic limiter
  independent of any broker. Not used in production.
- **Add a `cooldown_notice` log helper** to dedupe the 4 warning sites. The
  sites fire under different conditions (primary vs. failover, windowed vs.
  unwindowed) and the messages already differ in context. No
  deduplication gain worth the abstraction.
- **Backfill the existing `trigger_cooldown` on the historical bucket for
  the fetcher itself.** The fetcher doesn't return 429s; it consumes the
  cooldown state the HTTP layer sets. Symmetric backfill is YAGNI.
