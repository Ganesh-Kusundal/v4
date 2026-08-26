# Shared Rate-Limiter Implementation Plan (revised)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Share one `MultiBucketRateLimiter` per broker between `ParallelHistoryFetcher` and the broker's HTTP client so Dhan/Upstox cooldowns (set by the HTTP layer on 429) are visible to the fetcher and short-circuit its 30s `acquire` waits.

**Architecture:** Three-layer exposure of the same `MultiBucketRateLimiter` instance. `ProviderHttpClient.rate_limiter` is the *actual* source of truth (one hop from `self._pipeline._rate_limiter`). `DhanApiClient.rate_limiter` and `UpstoxApiClient.rate_limiter` are one-hop proxies through `self._http.rate_limiter`. `DhanBroker.rate_limiter` and `UpstoxBroker.rate_limiter` are one-hop proxies through `self._transport.rate_limiter`. `ParallelHistoryFetcher.__init__` reads `broker.rate_limiter`. Test mocks (no `.rate_limiter`) fall back to `limiter_for_provider(_provider_for(name))`.

**Tech Stack:** Python 3.12, `pytest`, `unittest.mock.MagicMock`, `MultiBucketRateLimiter`, `DhanApiClient` / `UpstoxApiClient` / `ProviderHttpClient`.

## Global Constraints

- Files modified: `brokers/src/tradex_brokers/common/provider_client.py`, `brokers/src/tradex_brokers/dhan/client.py`, `brokers/src/tradex_brokers/upstox/client.py`, `brokers/src/tradex_brokers/dhan/adapter.py`, `brokers/src/tradex_brokers/upstox/adapter.py`, `trading/src/tradex_trading/datalake/parallel_fetcher.py`, `trading/tests/datalake/test_parallel_fetcher.py`, plus one broker test file per new client/broker attribute (find or create).
- Run broker tests from `brokers/` with `uv run --no-sync pytest <path> -q`. Run fetcher tests from `trading/` with the same flag. (Confirmed in prior plan: `--no-sync` works around the venv/registry mismatch.)
- Production code change budget: ~15 lines across 6 files.
- Test code change budget: ~80 lines across 3 files.
- Do not change the public API. `client.rate_limiter` and `broker.rate_limiter` are new read-only attributes; `ParallelHistoryFetcher(brokers, max_workers, rate_limiter)` signature is unchanged.
- Use the exact `ponytail:` comment from Task 3 of the prior fetcher plan where the brief calls for it.
- The `2d5c8a6` K-threshold logic must continue to work against the shared limiter (regression test).
- `ERROR_LOG_CAP = 100`, `ACQUIRE_TIMEOUT_S = 30.0`, `BROKER_HEALTH_THRESHOLD = 3` are unchanged.

---

## Task 0: Expose `.rate_limiter` on `ProviderHttpClient`

**Files:**
- Modify: `brokers/src/tradex_brokers/common/provider_client.py:104-122` (`ProviderHttpClient.__init__`)
- Test: closest existing test file for `provider_client.py`, or new `brokers/tests/test_provider_client_smoke.py`

**Why:** This is the actual source of truth. Every HTTP client now exposes its limiter at one hop, so ApiClient and Broker become thin proxies.

- [ ] **Step 1: Find the test home**

Run: `ls /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/tests/`
Pick the most appropriate test file (likely `brokers/tests/test_provider_client.py` if it exists, otherwise the closest common-module test). If nothing fits, create `brokers/tests/test_provider_client_smoke.py` (a new tiny file with only this test + needed imports). Fill `<chosen-file>` into the test commands below.

- [ ] **Step 2: Write the failing test**

In the chosen test file, add:

```python
def test_provider_http_client_exposes_rate_limiter():
    """ProviderHttpClient.rate_limiter is the same instance the pipeline holds."""
    from unittest.mock import MagicMock
    from tradex_brokers.common.provider_client import ProviderHttpClient
    from tradex_brokers.common.resilience import MultiBucketRateLimiter, RateLimitConfig

    limiter = MultiBucketRateLimiter(default=RateLimitConfig())
    pipeline = MagicMock()
    pipeline._rate_limiter = limiter
    client = ProviderHttpClient(transport=MagicMock(), pipeline=pipeline)

    assert client.rate_limiter is limiter
```

Add `from unittest.mock import MagicMock` to imports if not present.

- [ ] **Step 3: Run test to verify it fails**

Run: `cd brokers && uv run --no-sync pytest <chosen-file>::test_provider_http_client_exposes_rate_limiter -q`
Expected: FAIL with `AttributeError: 'ProviderHttpClient' object has no attribute 'rate_limiter'`.

- [ ] **Step 4: Add the attribute**

In `brokers/src/tradex_brokers/common/provider_client.py`, at the end of `ProviderHttpClient.__init__` (after the `self._cache` block at line 120-122), add one line:

```python
self.rate_limiter = pipeline._rate_limiter
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd brokers && uv run --no-sync pytest <chosen-file>::test_provider_http_client_exposes_rate_limiter -q`
Expected: PASS.

- [ ] **Step 6: Run full broker suite**

Run: `cd brokers && uv run --no-sync pytest tests/ -q 2>&1 | tail -3`
Expected: previous count + 1, all green.

- [ ] **Step 7: Commit**

```bash
git add brokers/src/tradex_brokers/common/provider_client.py brokers/tests/<chosen-file>
git commit -m "feat(brokers): expose .rate_limiter on ProviderHttpClient"
```

---

## Task 1: Expose `.rate_limiter` on `DhanApiClient` and `UpstoxApiClient`

**Files:**
- Modify: `brokers/src/tradex_brokers/dhan/client.py:157-169` (`DhanApiClient.__init__`)
- Modify: `brokers/src/tradex_brokers/upstox/client.py` (`UpstoxApiClient.__init__` — find the line)
- Test: `brokers/tests/test_dhan_adapter.py` (existing per the prior failed Task 1; same for Upstox)

**Why:** Thin proxy layer so brokers can read `self._transport.rate_limiter` without knowing about `_http`.

- [ ] **Step 1: Write the failing test for DhanApiClient**

In `brokers/tests/test_dhan_adapter.py` (or whichever Dhan test file was used in the prior failed Task 1), add:

```python
def test_dhan_api_client_exposes_rate_limiter():
    """DhanApiClient.rate_limiter is the same instance its HTTP client holds."""
    from unittest.mock import MagicMock
    from tradex_brokers.dhan.client import DhanApiClient

    limiter = MultiBucketRateLimiter(default=RateLimitConfig())
    http = MagicMock()
    http.rate_limiter = limiter
    client = DhanApiClient(http=http, registry=MagicMock())

    assert client.rate_limiter is limiter
```

(Add `MultiBucketRateLimiter, RateLimitConfig` to the imports at the top of the test file if not already imported.)

- [ ] **Step 2: Write the failing test for UpstoxApiClient**

In the equivalent Upstox test file, add the same test (substituting `UpstoxApiClient`):

```python
def test_upstox_api_client_exposes_rate_limiter():
    from unittest.mock import MagicMock
    from tradex_brokers.upstox.client import UpstoxApiClient

    limiter = MultiBucketRateLimiter(default=RateLimitConfig())
    http = MagicMock()
    http.rate_limiter = limiter
    client = UpstoxApiClient(http=http, registry=MagicMock())

    assert client.rate_limiter is limiter
```

- [ ] **Step 3: Run new tests to verify they fail**

Run both:
- `cd brokers && uv run --no-sync pytest <dhan-test-file>::test_dhan_api_client_exposes_rate_limiter -q`
- `cd brokers && uv run --no-sync pytest <upstox-test-file>::test_upstox_api_client_exposes_rate_limiter -q`
Expected: both FAIL with `AttributeError`.

- [ ] **Step 4: Add the attribute on DhanApiClient**

In `brokers/src/tradex_brokers/dhan/client.py`, in `DhanApiClient.__init__` (after `self._http = http` at line 165), add:

```python
self.rate_limiter = self._http.rate_limiter
```

- [ ] **Step 5: Add the attribute on UpstoxApiClient**

In `brokers/src/tradex_brokers/upstox/client.py`, in `UpstoxApiClient.__init__` (find the equivalent of `self._http = http` and add the same line after it):

```python
self.rate_limiter = self._http.rate_limiter
```

- [ ] **Step 6: Run new tests — they should pass**

Run the two commands from Step 3 again.
Expected: both PASS.

- [ ] **Step 7: Run full broker suite**

Run: `cd brokers && uv run --no-sync pytest tests/ -q 2>&1 | tail -3`
Expected: previous count + 3 (Task 0 + Tasks 1×2), all green.

- [ ] **Step 8: Commit**

```bash
git add brokers/src/tradex_brokers/dhan/client.py brokers/src/tradex_brokers/upstox/client.py brokers/tests/<dhan-test-file> brokers/tests/<upstox-test-file>
git commit -m "feat(brokers): expose .rate_limiter on Dhan/Upstox ApiClient"
```

---

## Task 2: Expose `.rate_limiter` on `DhanBroker` and `UpstoxBroker`

**Files:**
- Modify: `brokers/src/tradex_brokers/dhan/adapter.py:58-71` (`DhanBroker.__init__`)
- Modify: `brokers/src/tradex_brokers/upstox/adapter.py:60-73` (`UpstoxBroker.__init__`)
- Test: same Dhan/Upstox test files as Task 1 (add broker-level tests)

- [ ] **Step 1: Write the failing test for DhanBroker**

In the Dhan test file, add:

```python
def test_dhan_broker_exposes_rate_limiter():
    """DhanBroker.rate_limiter is the same instance its transport holds."""
    from unittest.mock import MagicMock
    from tradex_brokers.dhan.adapter import DhanBroker

    limiter = MultiBucketRateLimiter(default=RateLimitConfig())
    transport = MagicMock()
    transport.rate_limiter = limiter
    broker = DhanBroker(transport=transport)

    assert broker.rate_limiter is limiter
```

- [ ] **Step 2: Write the failing test for UpstoxBroker**

In the Upstox test file, add the equivalent test (substituting `UpstoxBroker`).

- [ ] **Step 3: Run new tests — they should fail**

Run:
- `cd brokers && uv run --no-sync pytest <dhan-test-file>::test_dhan_broker_exposes_rate_limiter -q`
- `cd brokers && uv run --no-sync pytest <upstox-test-file>::test_upstox_broker_exposes_rate_limiter -q`
Expected: both FAIL with `AttributeError`.

- [ ] **Step 4: Add the attribute on DhanBroker**

In `brokers/src/tradex_brokers/dhan/adapter.py`, at the end of `DhanBroker.__init__` (after the `super().__init__(...)` call at line 65-71), add:

```python
self.rate_limiter = (
    self._transport.rate_limiter if self._transport is not None else None
)
```

- [ ] **Step 5: Add the attribute on UpstoxBroker**

In `brokers/src/tradex_brokers/upstox/adapter.py`, at the end of `UpstoxBroker.__init__` (after `super().__init__(...)` at line 67-73), add:

```python
self.rate_limiter = (
    self._transport.rate_limiter if self._transport is not None else None
)
```

- [ ] **Step 6: Run new tests — they should pass**

Run the two commands from Step 3 again.
Expected: both PASS.

- [ ] **Step 7: Run full broker suite**

Run: `cd brokers && uv run --no-sync pytest tests/ -q 2>&1 | tail -3`
Expected: previous count + 2, all green.

- [ ] **Step 8: Commit**

```bash
git add brokers/src/tradex_brokers/dhan/adapter.py brokers/src/tradex_brokers/upstox/adapter.py brokers/tests/<dhan-test-file> brokers/tests/<upstox-test-file>
git commit -m "feat(brokers): expose .rate_limiter on Dhan/Upstox broker"
```

---

## Task 3: Fetcher reads `.rate_limiter` from broker

**Files:**
- Modify: `trading/src/tradex_trading/datalake/parallel_fetcher.py:138-157` (constructor limiter construction)
- Test: `trading/tests/datalake/test_parallel_fetcher.py` (new test class `TestSharedLimiter`)

**Why:** The consumer side. The fetcher's limiter construction simplifies from "build per-broker" to "read from broker, fallback for mocks".

- [ ] **Step 1: Write the failing test**

Add to `trading/tests/datalake/test_parallel_fetcher.py` (import `MultiBucketRateLimiter` at the top of the test file if not already present — check the existing imports):

```python
class TestSharedLimiter:
    def test_fetcher_uses_broker_owned_limiter(self):
        """Fetcher must read the same MultiBucketRateLimiter the broker holds."""
        from tradex_brokers.common.resilience import MultiBucketRateLimiter, RateLimitConfig

        shared_limiter = MultiBucketRateLimiter(default=RateLimitConfig())
        dhan = _make_broker("dhan")
        dhan.rate_limiter = shared_limiter
        upstox = _make_broker("upstox")
        upstox.rate_limiter = MultiBucketRateLimiter(default=RateLimitConfig())

        fetcher = ParallelHistoryFetcher({"dhan": dhan, "upstox": upstox})
        # Same instance the broker holds, not a fresh one built by the fetcher.
        assert fetcher._limiters["dhan"] is shared_limiter
        assert fetcher._limiters["upstox"] is upstox.rate_limiter

    def test_fetcher_falls_back_when_broker_lacks_limiter(self):
        """Mocks without .rate_limiter keep the old per-broker fallback."""
        from tradex_brokers.common.resilience import MultiBucketRateLimiter

        dhan = _make_broker("dhan")
        # No .rate_limiter set on the mock — must not raise.
        fetcher = ParallelHistoryFetcher({"dhan": dhan})
        # Result is a MultiBucketRateLimiter (whatever the fallback chose).
        assert isinstance(fetcher._limiters["dhan"], MultiBucketRateLimiter)
        # And it's not the same object as any explicitly-set broker attr
        # (proves the fetcher built a fresh one for this mock).
        assert not hasattr(dhan, "rate_limiter") or \
            fetcher._limiters["dhan"] is not dhan.rate_limiter
```

(The last assertion is best-effort: `_make_broker` returns a `MagicMock` so `hasattr` is always True on a Mock, but the `is not` check still proves the fetcher built fresh.)

- [ ] **Step 2: Run new tests to verify they fail**

Run: `cd trading && uv run --no-sync pytest tests/datalake/test_parallel_fetcher.py::TestSharedLimiter -q`
Expected: the first test FAILS (fetcher builds a fresh limiter instead of reading the broker's). The second test PASSES already (current code already falls back for any broker key without an explicit override).

- [ ] **Step 3: Rewrite limiter construction in `ParallelHistoryFetcher.__init__`**

In `trading/src/tradex_trading/datalake/parallel_fetcher.py`, find the block at lines 152-157 (read the file to confirm — search for `self._limiters`):

```python
self._rate_limiter = rate_limiter
self._limiters: dict[str, MultiBucketRateLimiter] = (
    {name: rate_limiter for name in brokers}
    if rate_limiter is not None
    else {name: limiter_for_provider(_provider_for(name)) for name in brokers}
)
```

Replace with:

```python
self._rate_limiter = rate_limiter
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

- [ ] **Step 4: Run new tests to verify they pass**

Run: `cd trading && uv run --no-sync pytest tests/datalake/test_parallel_fetcher.py::TestSharedLimiter -q`
Expected: 2 passed.

- [ ] **Step 5: Run full fetcher suite to confirm no regression**

Run: `cd trading && uv run --no-sync pytest tests/datalake/test_parallel_fetcher.py -q`
Expected: 27 + 2 = 29 passed, all green.

Watch specifically:
- `TestBrokerHealth::test_per_instrument_failure_does_not_blacklist_broker` (K-threshold still works)
- `TestBrokerHealth::test_broker_wide_outage_still_blacklists`
- `TestFailoverFanout::test_broker_wide_outage_does_not_fan_out_n_times_m` (bound ≤ 10)
- `TestFailoverFanout::test_single_broker_outage_still_fails_over`
- `test_failover_throttled_by_failover_broker_limiter` (the monkey-patched `UPSTOX_RATE_LIMITS` no longer affects the fetcher — see Task 4 for the update)

- [ ] **Step 6: Commit**

```bash
git add trading/src/tradex_trading/datalake/parallel_fetcher.py trading/tests/datalake/test_parallel_fetcher.py
git commit -m "refactor(datalake): fetcher reads .rate_limiter from broker"
```

---

## Task 4: Update `test_failover_throttled_by_failover_broker_limiter` for the new wiring

**Files:**
- Modify: `trading/tests/datalake/test_parallel_fetcher.py:299-326`

**Why:** The existing test patches `UPSTOX_RATE_LIMITS["historical"]` to slow the *fallback* provider's limiter. Post-fix, the fetcher reads `upstox.rate_limiter` from the broker, not a fresh limiter built via `limiter_for_provider`. The monkey-patch on the rate table no longer affects the fetcher's limiter. The test must set `upstox.rate_limiter` to a slow limiter instead.

- [ ] **Step 1: Read the existing test**

Open `trading/tests/datalake/test_parallel_fetcher.py:299-326` and confirm the structure: it constructs two brokers, sets `dhan` to fail for all symbols, sets `upstox` to succeed, monkey-patches `UPSTOX_RATE_LIMITS["historical"]` to `1.0/s capacity 1`, runs `fetcher.fetch(INSTRUMENTS[:2], ...)`, asserts `elapsed >= 0.5`.

- [ ] **Step 2: Replace the monkey-patch with explicit limiter injection**

The new test body should construct Upstox's `MultiBucketRateLimiter` directly and assign it to `upstox.rate_limiter`. Keep the same intent (1 token/s, capacity 1, second Upstox call must wait ~1s for refill) and the same assertion (`elapsed >= 0.5`):

```python
def test_failover_throttled_by_failover_broker_limiter() -> None:
    """A symbol that fails over to Upstox is throttled by Upstox's limiter.

    Post-fix: the fetcher reads the broker's .rate_limiter, so the test
    injects a slow limiter directly on the upstox mock instead of patching
    the UPSTOX_RATE_LIMITS table (which the fetcher no longer consults).
    """
    from tradex_brokers.common.resilience import limiter_from_table

    slow = limiter_from_table({
        "historical": {
            "rate_per_second": 1.0, "capacity": 1,
            "min_interval": 0.0, "cooldown_seconds": 0.0,
        }
    })
    all_symbols = {str(i.instrument_id) for i in INSTRUMENTS}
    dhan = _make_broker("dhan", fail_symbols=all_symbols)
    upstox = _make_broker("upstox")
    upstox.rate_limiter = slow
    fetcher = ParallelHistoryFetcher(
        {"dhan": dhan, "upstox": upstox},
        max_workers=1,
    )
    end = BASE + timedelta(days=7)
    t0 = time.monotonic()
    results = fetcher.fetch(INSTRUMENTS[:2], Timeframe.M1, BASE, end)
    elapsed = time.monotonic() - t0

    assert len(results) == 2  # both symbols served via Upstox (failover)
    assert elapsed >= 0.5
```

- [ ] **Step 3: Run the test to verify it passes**

Run: `cd trading && uv run --no-sync pytest tests/datalake/test_parallel_fetcher.py::test_failover_throttled_by_failover_broker_limiter -q`
Expected: PASS (the new wiring is in place from Task 3, the new test body uses it correctly).

- [ ] **Step 4: Run full suite**

Run: `cd trading && uv run --no-sync pytest tests/datalake/test_parallel_fetcher.py -q`
Expected: 30 passed (29 from Task 3 + 1 updated).

- [ ] **Step 5: Commit**

```bash
git add trading/tests/datalake/test_parallel_fetcher.py
git commit -m "test(datalake): inject Upstox limiter via broker.rate_limiter"
```

---

## Task 5: Cooldown short-circuits fetcher `acquire` (regression test)

**Files:**
- Test: `trading/tests/datalake/test_parallel_fetcher.py` (add to `TestSharedLimiter`)

**Why:** The whole point of the design. A `trigger_cooldown("historical")` on the shared limiter must make the fetcher's `acquire` return `False` in <1s, not block the full 30s `ACQUIRE_TIMEOUT_S`. This test proves the *correctness* of the shared-state wiring — without it, the test suite would still pass on a broken design (each side builds its own limiter, fetcher would block 30s on a cooldown it can't see, but no test would catch it because no test triggers a cooldown).

- [ ] **Step 1: Write the test**

Add to `TestSharedLimiter` in `trading/tests/datalake/test_parallel_fetcher.py`:

```python
def test_cooldown_short_circuits_fetcher_acquire(self):
    """trigger_cooldown on the shared limiter must make fetcher acquire fail fast.

    The fetcher's 30s ACQUIRE_TIMEOUT_S only matters when the bucket is
    empty, not when it's in cooldown. With a shared limiter, the HTTP
    layer's trigger_cooldown() is visible to the fetcher immediately —
    the next acquire returns False without blocking 30s for tokens that
    won't arrive until cooldown expires.
    """
    from tradex_brokers.common.resilience import MultiBucketRateLimiter, RateLimitConfig

    shared = MultiBucketRateLimiter(default=RateLimitConfig())
    dhan = _make_broker("dhan")
    dhan.rate_limiter = shared
    fetcher = ParallelHistoryFetcher({"dhan": dhan})

    # Simulate a 429 hitting the HTTP layer.
    shared.trigger_cooldown("historical")

    t0 = time.monotonic()
    ok = fetcher._limiters["dhan"].acquire("historical", timeout=30.0)
    elapsed = time.monotonic() - t0

    assert ok is False
    assert elapsed < 1.0, f"acquire blocked {elapsed:.2f}s during cooldown"
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `cd trading && uv run --no-sync pytest tests/datalake/test_parallel_fetcher.py::TestSharedLimiter::test_cooldown_short_circuits_fetcher_acquire -q`
Expected: PASS. The bucket's `acquire` short-circuits when `cooldown_until > now` (it never reaches the token-wait loop), so the test passes immediately.

If the test FAILS with `elapsed >= 30.0`: the `MultiBucketRateLimiter.acquire` doesn't check cooldown before the token-wait. Read `brokers/src/tradex_brokers/common/resilience.py:TokenBucketRateLimiter.acquire` and confirm the cooldown check happens *before* the wait. If the order is wrong, the test is a real regression — escalate to the controller, do not paper over with a higher `elapsed` threshold.

- [ ] **Step 3: Run full suite**

Run: `cd trading && uv run --no-sync pytest tests/datalake/test_parallel_fetcher.py -q`
Expected: 31 passed (30 from Task 4 + 1 new).

- [ ] **Step 4: Commit**

```bash
git add trading/tests/datalake/test_parallel_fetcher.py
git commit -m "test(datalake): cooldown short-circuits fetcher acquire"
```

---

## Skipped (ponytail)

- **Process-global `limiter_for_provider` cache.** Tempting but kills test isolation. Broker-owned is the right unit.
- **Expose `.rate_limiter` on the `Broker` ABC.** Two production brokers are the only ones with real APIs. Paper and mocks stay duck-typed.
- **Remove the `rate_limiter=...` constructor arg on the fetcher.** Kept as an explicit override for synthetic tests. Not used in production.
- **Cooldown-notice log helper** to dedupe the 4 warning sites. The 4 sites fire under different conditions and the messages already differ in context. No deduplication gain.
- **`trigger_cooldown` on the fetcher itself.** The fetcher doesn't return 429s; it consumes the cooldown state the HTTP layer sets. Symmetric backfill is YAGNI.
- **Manual smoke against real Dhan quota.** The plan's tasks 0-5 cover the design correctness via unit tests. A manual smoke is valuable for confirming runtime behavior, but it's a one-off `python3 trading/scripts/sync_today.py` run, not a regression-prone area. The user can run it themselves after merge; if it reveals new failure modes, those become a follow-up plan.

## Self-Review

- **Spec coverage:** Every spec section maps to a task. §1 (ProviderHttpClient exposes) = Task 0. §2 (ApiClient exposes) = Task 1. §3 (Broker exposes) = Task 2. §5 (fetcher reads) = Task 3. §4 (cooldown fail-fast) = Task 5. Testing = Tasks 0-5. Skipped = explicit in plan.
- **Placeholder scan:** No "TODO", no "add appropriate handling", every code block has real content.
- **Type consistency:** `*.rate_limiter` is read as `MultiBucketRateLimiter | None` everywhere. `MultiBucketRateLimiter(default=RateLimitConfig(), ...)` is the required construction signature (no positional `default`). `MultiBucketRateLimiter.acquire(category, timeout) -> bool` matches across all tasks. `trigger_cooldown(category: str)` matches the spec.
- **Risk for `test_failover_throttled_by_failover_broker_limiter`:** Task 4 updates the test. The rate table patch was load-bearing only for the fetcher's old code path; the new explicit injection via `broker.rate_limiter` is sufficient.
- **Prior bug captured:** the first Task 1 attempt (now reverted at `b93e0b0`) used `self._transport._pipeline._rate_limiter` which doesn't exist on a real `DhanApiClient` (the actual chain is `DhanApiClient._http._pipeline._rate_limiter`). The revised plan fixes this by adding a `ProviderHttpClient.rate_limiter` attribute at the source, so each proxy is one hop.
