# Re-add Broker Resilience Stack — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-add the broker resilience stack (rate limiting + safe retry + circuit breaker) as ONE consolidated module wired into production `build_provider_client`, so all broker HTTP calls throttle, and fix the `ParallelHistoryFetcher` backfill throttling gap.

**Architecture:** One module `common/resilience.py` holds the token-bucket/multi-bucket rate limiters + per-broker rate tables + circuit breaker + safe retry + composite `ResiliencePipeline`. `build_provider_client` gains a `provider` param and builds a `ResiliencePipeline` (rate_limit → circuit_breaker → safe retry → fetch). `ParallelHistoryFetcher` uses a shared `historical`-bucket limiter so fan-out throttles.

**Tech Stack:** Python 3.12+, stdlib only (`threading`, `time`, `collections.deque`, `urllib`), RxPY (unchanged), pytest.

## Global Constraints

- Dependency boundary: `domain ← brokers ← trading` (never reverse)
- Safe retry ONLY: GET/HEAD/OPTIONS may retry on 5xx/429/transport; mutations (POST/PUT/DELETE) NEVER auto-retry — they fail loud for reconciliation
- Rate tables restored AS-IS from the deleted stack (commit `f912655^`): Dhan historical 5/s cap 10, orders 10/s cap 20, cooldown 130s; Upstox historical 5/s cap 10, orders 10/s cap 20, cooldown 60s
- Do NOT touch: auth.py, token_lifecycle.py, totp_cooldown.py, ws_decoder.py, proto/, tick_parser.py
- No new dependencies (stdlib only)
- Public `BrokerAdapter`/`ExtensionAdapter` protocol surface unchanged
- 401/403 auth-retry stays in `ProviderHttpClient` (unchanged)
- Full suite green after every task; ruff + mypy(domain) clean

---

### Task 1: Create the consolidated `common/resilience.py`

**Files:**
- Create: `brokers/src/tradex_brokers/common/resilience.py`
- Test: `brokers/tests/common/test_resilience.py` (created in Task 4)

**Interfaces:**
- Consumes: `tradex_domain.RateLimitError`, `tradex_domain.BrokerUnavailableError` (both exist in `domain/src/tradex_domain/errors.py`)
- Produces (used by Task 2 wiring and tests):
  - `RateLimitConfig(rate_per_second, capacity, min_interval, cooldown_seconds)`
  - `TokenBucketRateLimiter.acquire(timeout=None) -> None` (raises `TimeoutError`), `.try_acquire() -> bool`, `.trigger_cooldown()`, `.available_tokens`
  - `MultiBucketRateLimiter.acquire(category, tokens=1, timeout=None) -> bool`, `.trigger_cooldown(category)`, `.categories()`
  - `DHAN_RATE_LIMITS`, `UPSTOX_RATE_LIMITS`, `PAPER_RATE_LIMITS` dicts
  - `table_for_provider(provider)`, `limiter_from_table(table)`, `limiter_for_provider(provider) -> MultiBucketRateLimiter`, `bucket_for_path(path, method) -> str`
  - `CircuitBreaker(failure_threshold=5, recovery_timeout=30.0, half_open_max=1).request(fn, *args, **kwargs)`, `.state`, `.metrics`, `.reset()`
  - `RetryConfig(max_attempts=3, base_delay=0.1, max_delay=30.0, exponential_base=2.0, jitter=True, retryable_exceptions=(...))`, `RetryableHttpClient(config).send(method, url, **kwargs) -> dict`
  - `ResiliencePipeline(rate_limiter, retry, breaker, *, rate_limit_timeout=None).send(method, url, **kwargs) -> dict`
  - `retryable(method) -> bool`

- [ ] **Step 1: Verify the deleted source is in git history**

Run: `git show f912655^:brokers/src/tradex_brokers/common/rate_limit.py | wc -l` then same for `retry.py`, `circuit_breaker.py`, `resilience.py`.
Expected: 509 / 202 / 208 / 110 lines respectively — the four source files are recoverable.

- [ ] **Step 2: Reconstruct the throttling core into the new module**

Create `brokers/src/tradex_brokers/common/resilience.py`. Recover the throttling core from the deleted `rate_limit.py` and place it in the new file:

```bash
git show f912655^:brokers/src/tradex_brokers/common/rate_limit.py
```

Extract verbatim: `RateLimitConfig`, `DHAN_RATE_LIMITS`, `UPSTOX_RATE_LIMITS`, `PAPER_RATE_LIMITS`, `_RATE_TABLES_BY_PROVIDER`, `table_for_provider`, `limiter_from_table`, `limiter_for_provider`, `bucket_for_path`, `TokenBucketRateLimiter`, `RollingWindowCounter`, `MultiBucketRateLimiter`. Do NOT copy the `BROKER_RATE_TABLES` upper-case alias (unused). Update the module docstring to describe the consolidated module.

- [ ] **Step 3: Add the circuit breaker section**

Recover from `circuit_breaker.py` (same git show pattern): `CircuitState`, `CircuitBreakerConfig`, `CircuitBreaker`, `CircuitBreakerOpenError`. Append after the rate-limiter section.

- [ ] **Step 4: Add the safe-retry section**

Recover from `retry.py`: `_SAFE_METHODS`, `retryable`, `RetryExhaustedError`, `RetryConfig`, `RetryableHttpClient`. Append after the circuit-breaker section.

- [ ] **Step 5: Add the composite pipeline + `__all__`**

Recover `ResiliencePipeline` from `resilience.py`, adjusting imports to be local (all classes now in the same module). Add a consolidated `__all__`:

```python
__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerOpenError",
    "CircuitState",
    "DHAN_RATE_LIMITS",
    "MultiBucketRateLimiter",
    "PAPER_RATE_LIMITS",
    "RateLimitConfig",
    "ResiliencePipeline",
    "RetryConfig",
    "RetryExhaustedError",
    "RetryableHttpClient",
    "RollingWindowCounter",
    "TokenBucketRateLimiter",
    "UPSTOX_RATE_LIMITS",
    "bucket_for_path",
    "limiter_for_provider",
    "limiter_from_table",
    "retryable",
    "table_for_provider",
]
```

- [ ] **Step 6: Verify the module imports cleanly**

Run: `PYTHONPATH=brokers/src:domain/src python -c "from tradex_brokers.common.resilience import ResiliencePipeline, limiter_for_provider, bucket_for_path, CircuitBreaker, RetryableHttpClient; print('ok')"`
Expected: prints `ok`

- [ ] **Step 7: Export from `common/__init__.py`**

Add to `brokers/src/tradex_brokers/common/__init__.py` the imports and `__all__` entries for: `ResiliencePipeline`, `MultiBucketRateLimiter`, `TokenBucketRateLimiter`, `CircuitBreaker`, `RetryableHttpClient`, `limiter_for_provider`, `bucket_for_path`.

- [ ] **Step 8: Verify imports + commit**

Run: `PYTHONPATH=brokers/src:domain/src python -c "from tradex_brokers.common import ResiliencePipeline, limiter_for_provider; print('ok')"`
Then:
```bash
git add brokers/src/tradex_brokers/common/resilience.py brokers/src/tradex_brokers/common/__init__.py
git commit -m "feat(brokers): consolidated resilience module (rate limit + circuit breaker + safe retry)"
```

### Task 2: Wire `ResiliencePipeline` into `build_provider_client`

**Files:**
- Modify: `brokers/src/tradex_brokers/common/client_shared.py`
- Modify: `brokers/src/tradex_brokers/dhan/client.py` (from_fetch)
- Modify: `brokers/src/tradex_brokers/upstox/client.py` (from_fetch)

**Interfaces:**
- Consumes: `ResiliencePipeline`, `limiter_for_provider` from Task 1
- Produces: `build_provider_client(*, fetch, base_url, auth_headers, token_manager=None, access_token="", provider="paper")` — builds a `ResiliencePipeline` instead of `FetchResiliencePipeline`

- [ ] **Step 1: Write a failing test asserting the pipeline type**

Add to `brokers/tests/common/test_client_seam.py` (or a new `test_resilience_wiring.py`):

```python
def test_build_provider_client_wires_resilience_pipeline() -> None:
    from tradex_brokers.common.client_shared import build_provider_client
    from tradex_brokers.common.resilience import ResiliencePipeline

    def fake_fetch(method, url, **kwargs):
        return 200, {"data": {"ok": True}}

    http, _ws = build_provider_client(
        fetch=fake_fetch, base_url="https://api.dhan.co/v2",
        auth_headers=lambda t: {"access-token": t}, provider="dhan",
    )
    # ProviderHttpClient._pipeline is the composed resilience pipeline
    assert isinstance(http._pipeline, ResiliencePipeline)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest brokers/tests/common/test_resilience_wiring.py::test_build_provider_client_wires_resilience_pipeline --tb=short`
Expected: FAIL — pipeline is `FetchResiliencePipeline`, not `ResiliencePipeline`

- [ ] **Step 3: Add `provider` param + build ResiliencePipeline**

In `brokers/src/tradex_trading/../tradex_brokers/common/client_shared.py`:

```python
from tradex_brokers.common.resilience import (
    CircuitBreaker,
    ResiliencePipeline,
    RetryableHttpClient,
    limiter_for_provider,
)
```

Replace in `build_provider_client`:

```python
    pipeline = FetchResiliencePipeline(fetch)
```

with:

```python
    # Rate limit → circuit breaker → safe retry (GET/HEAD/OPTIONS only).
    # Mutations never auto-retry — they fail loud for reconciliation.
    pipeline = ResiliencePipeline(
        rate_limiter=limiter_for_provider(provider),
        retry=RetryableHttpClient(),
        breaker=CircuitBreaker(),
    )
    # Pipeline.send adapts to the injected fetch the same way the old
    # FetchResiliencePipeline did (auth/transport stay in ProviderHttpClient).
    pipeline = _PipelineAdapter(pipeline, fetch)
```

Add the adapter (preserves the existing fetch-injection test seam):

```python
class _PipelineAdapter:
    """Wrap ResiliencePipeline so the injected-fetch seam still works.

    ``ResiliencePipeline.send`` expects the transport inside ``RetryableHttpClient``;
    here the injected ``fetch`` is the transport, so the adapter funnels the
    pipeline's retry client onto the injected callable.
    """
```

RESOLVE the adapter design by reading the current `FetchResiliencePipeline.send` (client_shared.py:74-86) — it calls `self._fetch(method, url, **kwargs)` and normalizes the result. The resilience pipeline's `send` calls `RetryableHttpClient.send` which would do real urllib. To keep the fetch-injection seam AND the resilience, the cleanest approach: give `ResiliencePipeline` an optional `transport` callable (the injected fetch) that `RetryableHttpClient` delegates to instead of urllib. Implement this in Task 1 by adding `transport: Callable | None = None` to `RetryableHttpClient` — when provided, `send` calls `transport(method, url, **kwargs)` and normalizes the `(status, body)` tuple into `{"data": body, "_http_status": status}`, preserving the seam. If you already implemented Task 1 without this, add it now and re-verify Task 1's import check.

- [ ] **Step 4: Pass `provider` from both `from_fetch` methods**

In `brokers/src/tradex_brokers/dhan/client.py` `from_fetch`, add `provider="dhan"` to the `build_provider_client(...)` call. Same for `upstox/client.py` with `provider="upstox"`.

- [ ] **Step 5: Run tests + fix the failing test**

Run: `python -m pytest brokers/tests/common/test_resilience_wiring.py -q`
Expected: PASS (pipeline is now ResiliencePipeline)
Then: `python -m pytest brokers/tests/common -q` — all pass (the seam tests must still pass through the adapter)

- [ ] **Step 6: Full broker suite + commit**

Run: `python -m pytest brokers/tests -q`
Then:
```bash
git add brokers/src/tradex_brokers/common/client_shared.py brokers/src/tradex_brokers/common/resilience.py brokers/src/tradex_brokers/dhan/client.py brokers/src/tradex_brokers/upstox/client.py brokers/tests/common/
git commit -m "feat(brokers): wire ResiliencePipeline into build_provider_client"
```

### Task 3: Fix `ParallelHistoryFetcher` backfill throttling

**Files:**
- Modify: `trading/src/tradex_trading/datalake/parallel_fetcher.py`

**Interfaces:**
- Consumes: `limiter_for_provider`, `MultiBucketRateLimiter` from Task 1
- Produces: `ParallelHistoryFetcher(brokers, max_workers=4, rate_limiter=None)` — the fetcher acquires the `historical` bucket token before each `broker.history(...)` call

- [ ] **Step 1: Write a failing test proving fan-out is throttled**

Add to `trading/tests/datalake/test_parallel_fetcher.py`:

```python
def test_fetch_respects_historical_rate_bucket() -> None:
    """Fan-out across N workers must still throttle to the historical rate."""
    import time
    from tradex_brokers.common.resilience import limiter_for_provider

    class _SlowBroker:
        def history(self, inst, timeframe, start, end):
            return _series(inst, 5)  # use the existing _series helper

    fetcher = ParallelHistoryFetcher({"dhan": _SlowBroker()}, max_workers=8)
    # The fetcher builds its own limiter via limiter_for_provider("dhan"),
    # whose "historical" bucket is 5/s cap 10. A 12-instrument batch must
    # take >= ~0.4s (12 tokens / 5 per sec) rather than finishing instantly.
    t0 = time.monotonic()
    fetcher.fetch([_eq(i) for i in range(12)], Timeframe.M1, _now(), _now())
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.35
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest trading/tests/datalake/test_parallel_fetcher.py::test_fetch_respects_historical_rate_bucket --tb=short`
Expected: FAIL — finishes in < 0.05s (no throttling)

- [ ] **Step 3: Add the limiter to the fetcher**

In `trading/src/tradex_trading/datalake/parallel_fetcher.py`, import and wire:

```python
from tradex_brokers.common.resilience import (
    MultiBucketRateLimiter,
    limiter_for_provider,
)
```

In `__init__`:
```python
    def __init__(
        self,
        brokers: dict[str, Any],
        max_workers: int = 4,
        rate_limiter: MultiBucketRateLimiter | None = None,
    ) -> None:
        self._brokers = brokers
        self._max_workers = max_workers
        # Shared per-provider limiter: fan-out throttles to the broker's
        # documented historical rate instead of firing N workers unbounded.
        self._rate_limiter = rate_limiter or limiter_for_provider("dhan")
```

In `_fetch_one`, before each `broker.history(...)` call (both the primary and the failover path):
```python
            self._rate_limiter.acquire("historical", timeout=30.0)
```
Match the existing `_fetch_one` structure — acquire before the primary call and before the failover call.

- [ ] **Step 4: Update the stale docstring**

Replace the module docstring line:
```
Each broker's existing rate limiter handles throttling — the fetcher just
fans out work across ThreadPoolExecutor.
```
with:
```
The fetcher throttles through a shared per-provider historical rate-limit
bucket (5/s for Dhan/Upstox) so fan-out never exceeds the broker's documented
historical-data quota; broker failures still trigger bounded failover.
```

- [ ] **Step 5: Run the new + existing fetcher tests**

Run: `python -m pytest trading/tests/datalake/test_parallel_fetcher.py -q`
Expected: all PASS (including the 90-day guard tests — the limiter must not interfere)

- [ ] **Step 6: Commit**

```bash
git add trading/src/tradex_trading/datalake/parallel_fetcher.py trading/tests/datalake/test_parallel_fetcher.py
git commit -m "fix(datalake): throttle ParallelHistoryFetcher fan-out to historical rate bucket"
```

### Task 4: Restore resilience tests

**Files:**
- Create: `brokers/tests/common/test_resilience.py`

**Interfaces:**
- Consumes: everything from Task 1

- [ ] **Step 1: Write the token-bucket tests**

```python
from __future__ import annotations

import time

import pytest

from tradex_brokers.common.resilience import (
    CircuitBreaker,
    CircuitState,
    MultiBucketRateLimiter,
    RateLimitConfig,
    ResiliencePipeline,
    RetryableHttpClient,
    TokenBucketRateLimiter,
    bucket_for_path,
    limiter_for_provider,
)


def test_token_bucket_blocks_until_refill() -> None:
    limiter = TokenBucketRateLimiter(rate=10.0, burst=1)
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False
    time.sleep(0.12)  # ~1 token refilled at 10/s
    assert limiter.try_acquire() is True


def test_token_bucket_cooldown_blocks() -> None:
    limiter = TokenBucketRateLimiter(rate=10.0, burst=5, cooldown_seconds=60.0)
    limiter.trigger_cooldown()
    assert limiter.try_acquire() is False
    with pytest.raises(TimeoutError):
        limiter.acquire(timeout=0.01)
```

- [ ] **Step 2: Run the token-bucket tests**

Run: `python -m pytest brokers/tests/common/test_resilience.py -k "token_bucket" -v`
Expected: 2 PASS

- [ ] **Step 3: Write the multi-bucket + bucket_for_path tests**

```python
def test_bucket_for_path_classifies() -> None:
    assert bucket_for_path("/v2/historical/foo", "GET") == "historical"
    assert bucket_for_path("/v2/charts/intraday", "GET") == "historical"
    assert bucket_for_path("/v2/order", "POST") == "orders"
    assert bucket_for_path("/v2/orders/super", "POST") == "orders"
    assert bucket_for_path("/v2/marketfeed", "GET") == "quotes"
    assert bucket_for_path("/v2/positions", "GET") == "admin"


def test_multibucket_respects_categories() -> None:
    limiter = limiter_for_provider("dhan")
    assert "historical" in limiter.categories()
    assert "orders" in limiter.categories()
```

- [ ] **Step 4: Run the multi-bucket tests**

Run: `python -m pytest brokers/tests/common/test_resilience.py -k "bucket_for_path or multibucket" -v`
Expected: PASS

- [ ] **Step 5: Write the circuit-breaker tests**

```python
def test_circuit_breaker_trips_and_recovers() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
    calls = {"n": 0}

    def failing() -> dict:
        calls["n"] += 1
        return {"_http_status": 500}

    with pytest.raises(Exception):
        try:
            breaker.request(failing)
        except Exception:
            raise
    # 2 failures trip OPEN; next call fails fast without invoking fn
    try:
        breaker.request(failing)
    except Exception:
        pass
    assert breaker.state == CircuitState.OPEN.value
    with pytest.raises(Exception):
        breaker.request(failing)
    assert calls["n"] == 2  # fn not called while OPEN
    time.sleep(0.06)
    # HALF_OPEN probe passes; success closes the breaker
    def ok() -> dict:
        return {"_http_status": 200}
    breaker.request(ok)
    assert breaker.state == CircuitState.CLOSED.value
```

- [ ] **Step 6: Run the circuit-breaker tests**

Run: `python -m pytest brokers/tests/common/test_resilience.py -k "circuit_breaker" -v`
Expected: PASS

- [ ] **Step 7: Write the safe-retry tests**

```python
def test_retry_safe_method_retries_transient() -> None:
    attempts = {"n": 0}

    class _Flaky:
        def __init__(self):
            self.client = RetryableHttpClient()
        def send(self, method, url, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ConnectionError("transient")
            return {"data": {"ok": True}, "_http_status": 200}

    # Simulate via a transport callable that fails once then succeeds
    def transport(method, url, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise ConnectionError("transient")
        return 200, {"ok": True}

    client = RetryableHttpClient(transport=transport)
    result = client.send("GET", "https://x.test")
    assert result["data"]["ok"] is True
    assert attempts["n"] >= 2


def test_retry_never_retries_mutation() -> None:
    attempts = {"n": 0}

    def transport(method, url, **kwargs):
        attempts["n"] += 1
        return 500, {"error": "boom"}

    client = RetryableHttpClient(transport=transport)
    with pytest.raises(Exception):
        client.send("POST", "https://x.test/order")
    assert attempts["n"] == 1  # POST never auto-retried
```

NOTE: `RetryableHttpClient` must accept `transport: Callable | None = None` (added in Task 2 Step 3). If the `transport` param isn't there, add it to `RetryableHttpClient.__init__` now and re-verify Task 1 imports.

- [ ] **Step 8: Run the safe-retry tests**

Run: `python -m pytest brokers/tests/common/test_resilience.py -k "retry" -v`
Expected: PASS

- [ ] **Step 9: Write the composite pipeline test**

```python
def test_pipeline_orders_rate_then_send() -> None:
    order = []

    def transport(method, url, **kwargs):
        order.append("send")
        return 200, {"ok": True}

    limiter = TokenBucketRateLimiter(rate=1000.0, burst=1000)

    def gate() -> None:
        order.append("gate")
        limiter.acquire(timeout=0.1)

    # Patch acquire order via a subclasses bucket limiter
    class _Gated(MultiBucketRateLimiter):
        def acquire(self, category, tokens=1, timeout=None):
            order.append("gate")
            return super().acquire(category, tokens, timeout)

    pipeline = ResiliencePipeline(
        rate_limiter=_Gated(default=RateLimitConfig(rate_per_second=1000.0, capacity=1000)),
        retry=RetryableHttpClient(transport=transport),
        breaker=CircuitBreaker(),
    )
    pipeline.send("GET", "https://x.test")
    assert order == ["gate", "send"]
```

- [ ] **Step 10: Run the pipeline test**

Run: `python -m pytest brokers/tests/common/test_resilience.py -k "pipeline" -v`
Expected: PASS

- [ ] **Step 11: Full broker suite + commit**

Run: `python -m pytest brokers/tests -q`
Then:
```bash
git add brokers/tests/common/test_resilience.py
git commit -m "test(brokers): resilience stack tests (rate limit, circuit breaker, safe retry)"
```

---

## Orchestrator verification (after all tasks merged)

- [ ] `python -m pytest domain/tests brokers/tests trading/tests -q` — all green
- [ ] `ruff check domain/src brokers/src trading/src` — clean
- [ ] `mypy domain/src/tradex_domain --ignore-missing-imports` — clean
- [ ] `python -c "from tradex_brokers.common import ResiliencePipeline, limiter_for_provider; print('ok')"` — ok
- [ ] `graphify update .`

## Self-Review Notes

- **Spec coverage:** Spec decisions (full stack, safe retry, restore tables as-is, consolidated module) → Tasks 1-4. Wiring spec → Task 2. Fetcher gap → Task 3. Testing spec → Task 4.
- **Placeholder scan:** The `_PipelineAdapter`/transport-injection resolution in Task 2 Step 3 names the concrete options and the exact seam to preserve (FetchResiliencePipeline.send behavior); the implementer resolves it against the current file, which the task reads.
- **Type consistency:** `RetryableHttpClient(transport=...)`, `ResiliencePipeline(rate_limiter, retry, breaker, *, rate_limit_timeout=None)`, `limiter_for_provider(provider)`, `MultiBucketRateLimiter.acquire(category, ...)` are consistent across Tasks 1-4. The `Transport` callable returns `(status, body)` tuples matching the injected-fetch contract.
