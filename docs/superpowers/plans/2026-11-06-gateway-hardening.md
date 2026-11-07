# tradex_gateway Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remediate the gateway review findings — concurrency, WS input validation, security/DoS bounds, the broker-specific depth branch, data-correctness gaps, structural debt and dead code — without changing the `/v1` contract except where explicitly stated.

**Architecture:** Keep the gateway as a data-only composition root. P0 changes isolate state transitions from broker I/O, type and validate the WebSocket edge, bound every in-memory map, and move broker-specific depth selection behind capability logic. P1 changes split the oversized modules, extract shared cache utilities, and consolidate error policy. P2 removes dead code and polishes observability.

**Tech Stack:** Python 3.12+, FastAPI, Pydantic, asyncio + threads, existing `tradex_*` workspace packages, pytest, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-11-06-broker-data-gateway-design.md` and the review findings recorded in this session.

## Global Constraints

- No order operations; brokers built with `allow_order_operations=False`.
- One process only; `workers > 1` refused.
- Canonical IDs: `NSE:RELIANCE`, `IDX:NIFTY`, `NFO:NIFTY:20261112:FUT`, `NFO:NIFTY:20261112:25000:CE`.
- Timestamps returned to clients are epoch milliseconds UTC. Inbound timestamps must include a UTC offset (P0 change; naive input becomes 400).
- No broker-name branches outside adapters. Capability flags drive selection.
- Never hold a lock across broker network I/O.
- Every cache, token-bucket key map, single-flight map and connection registry must be bounded.
- Single API key; loopback bind by default; `/metrics` requires the API key when one is configured.
- TDD: failing test → implementation → tests green → commit. Commit per task; do not push.
- Baseline: `pytest gateway/tests` = **40 passed**. The full suite has **6 known pre-existing failures** (2 hermetic conftest-shadowing, 4 replay optimization) that are out of scope.
- Commands:
  - `.venv/bin/python -m pytest gateway/tests -p no:cacheprovider --import-mode=importlib -c pyproject.toml -q`
  - `.venv/bin/ruff check gateway/src gateway/tests && .venv/bin/ruff format --check gateway/src gateway/tests`
  - `.venv/bin/mypy gateway/src`
  - `.venv/bin/python scripts/mypy_ratchet.py`

## Target File Map

```
gateway/src/tradex_gateway/
  cache.py              NEW  shared TTLCache + SingleFlight (public)
  app.py                     app factory only; mounts routers
  routes/               NEW  health, instruments, history, quotes, depth,
                             chains, metrics, stream (WS)
  live.py                    state machine + feed; no lock across I/O
  market.py                  services; cache imports from cache.py
  history.py                 fetch policy; cache imports from cache.py
  router.py                  failure policy table
  brokers.py                 BrokerRuntime + lot_size + known()
  directory.py               resolution + metadata
  config.py                  immutable validated config
  models.py                  inbound WS discriminated union
gateway/tests/
  test_cache.py         NEW
  test_live.py              non-blocking switch + staleness + depth method
  test_api.py               WS validation + /metrics auth + generic 500
  test_router.py            failure policy + no quote fan-out on outage
  test_market.py            depth freshness + chain preferred broker
  test_directory.py         availability + lot-size normalization
```

---

## Phase 1 — P0 correctness, concurrency and security

### Task 1: Make LiveHub switches non-blocking

**Files:**
- Modify: `gateway/src/tradex_gateway/live.py` (`_switch_to`, `_stop_feed`, `__init__`)
- Test: `gateway/tests/test_live.py`

**Interfaces:**
- Produces: `self._switch_lock: threading.RLock` serializing switches/teardown; it may span broker calls, while the state lock `_lock` is never held across broker I/O.
- Consumes: existing `_BrokerFeed.stop/sync_quotes/sync_depth`, `_wanted`, `_broadcast`.

- [ ] **Step 1: Add the failing regression test**

```python
def test_switch_does_not_block_quote_publish():
    async def scenario() -> None:
        class BlockingBroker(FakeBroker):
            def __init__(self, name: str) -> None:
                super().__init__(name)
                self.entered = threading.Event()
                self.release = threading.Event()
                self.block = False

            def subscribe_quotes(self, instruments, handler):
                if self.block:
                    self.entered.set()
                    self.release.wait(2.0)
                return super().subscribe_quotes(instruments, handler)

        dhan, upstox = BlockingBroker("dhan"), FakeBroker("upstox")
        runtimes = {
            "dhan": BrokerRuntime("dhan", dhan),
            "upstox": BrokerRuntime("upstox", upstox),
        }
        config = gateway_config()
        directory = InstrumentDirectory(runtimes)
        hub = LiveHub(config, MarketDataRouter(config, runtimes), directory, runtimes)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        control: asyncio.Queue = asyncio.Queue()
        hub.add_client("c1", loop, queue, control)
        hub.subscribe("c1", ["NSE:RELIANCE"], "quote", 5)

        dhan.block = True
        switch = threading.Thread(
            target=lambda: hub._switch_to("dhan", reason="test"), daemon=True
        )
        switch.start()
        assert dhan.entered.wait(1.0)

        started = time.monotonic()
        hub._publish_quote(make_quote(equity()), "dhan")
        assert time.monotonic() - started < 0.5

        dhan.release.set()
        switch.join(2.0)
        assert hub.status()["broker"] == "dhan"
        hub.stop()

    asyncio.run(scenario())
```

- [ ] **Step 2: Run it and observe the hang**

Run: `pytest gateway/tests/test_live.py::test_switch_does_not_block_quote_publish -v`
Expected: FAIL — the publish blocks on `_lock` for 2 s.

- [ ] **Step 3: Split state mutation from I/O**

In `LiveHub.__init__` add `self._switch_lock = threading.RLock()`.
In `_switch_to`, replace the single `with self._lock:` block with:

```python
with self._switch_lock:
    self._broadcast({"op": "status", "state": "switching", "broker": broker_name})
    with self._lock:
        previous = self._active
        old_feed = self._feed
        quotes, depths, levels = self._wanted()
        self._feed = None
        self._active = None

    if old_feed is not None:
        old_feed.stop()  # I/O outside the state lock

    runtime = self._runtimes.get(broker_name)
    if runtime is None or runtime.broker is None:
        self._broadcast({"op": "status", "state": "halted", "broker": None})
        return

    feed = _BrokerFeed(runtime, self)
    effective = min(levels, int(getattr(runtime.capabilities, "depth_levels", 0) or 5))
    try:
        feed.sync_quotes(quotes)
        if depths:
            feed.sync_depth(depths, effective)
    except Exception as exc:  # noqa: BLE001
        runtime.health.record_failure("live", 15.0, str(exc))
        feed.stop()
        target = self._other_broker(broker_name, exclude=attempted)
        if target is not None:
            self._switch_to(target, reason=reason, attempted=attempted)
        else:
            self._broadcast({"op": "status", "state": "halted", "broker": None})
        return

    with self._lock:
        self._feed = feed
        self._active = broker_name

    self._broadcast({"op": "status", "state": "ready", "broker": broker_name})
    # existing gap-frame block stays here
    self._ensure_monitor()
```

Make `_stop_feed` acquire `_switch_lock` then `_lock`, stop the feed outside `_lock`, and clear state.

- [ ] **Step 4: Run the test and the live suite**

Run: `pytest gateway/tests/test_live.py gateway/tests/test_live_failover.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/src/tradex_gateway/live.py gateway/tests/test_live.py
git commit -m "fix(gateway): never hold the hub lock across broker I/O"
```

### Task 2: Validate WebSocket input with the existing models

**Files:**
- Modify: `gateway/src/tradex_gateway/models.py`, `gateway/src/tradex_gateway/app.py`
- Test: `gateway/tests/test_api.py`

**Interfaces:**
- Produces: `ClientMessage` discriminated union (`subscribe`, `unsubscribe`, `ping`, `status`).
- Produces: reader sends `{"op":"error","code":"invalid_request"}` and stays connected.

- [ ] **Step 1: Add the failing tests**

```python
def test_websocket_rejects_invalid_payload_and_stays_connected():
    app, _, _ = _app()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/stream") as ws:
            ws.receive_json()  # initial status
            ws.send_text("not-json")
            error = ws.receive_json()
            assert error["op"] == "error"
            assert error["code"] == "invalid_request"

            ws.send_json({"op": "subscribe", "instruments": ["NSE:RELIANCE"],
                          "mode": "quote", "levels": "bad"})
            error = ws.receive_json()
            assert error["op"] == "error"

            ws.send_json({"op": "ping"})
            assert _receive_op(ws, "pong")["op"] == "pong"
```

Add a `_receive_op(ws, op)` helper that drains frames until the requested op.

- [ ] **Step 2: Run and observe the reader crash**

Run: `pytest gateway/tests/test_api.py::test_websocket_rejects_invalid_payload_and_stays_connected -v`
Expected: FAIL/connection closed.

- [ ] **Step 3: Add the discriminated union**

```python
class PingMessage(BaseModel):
    op: Literal["ping"]


class StatusMessage(BaseModel):
    op: Literal["status"]


ClientMessage = Annotated[
    Union[SubscribeMessage, UnsubscribeMessage, PingMessage, StatusMessage],
    Field(discriminator="op"),
]
```

In `app.reader()`:

```python
_ADAPTER = TypeAdapter(ClientMessage)

try:
    raw = await ws.receive_json()
except json.JSONDecodeError:
    await control.put({"op": "error", "code": "invalid_request",
                       "message": "invalid JSON"})
    continue
try:
    message = _ADAPTER.validate_python(raw)
except ValidationError as exc:
    await control.put({"op": "error", "code": "invalid_request",
                       "message": exc.errors()[0]["msg"]})
    continue

match message:
    case SubscribeMessage():
        ack = await ws.app.state.run(
            hub.subscribe, client_id, message.instruments, message.mode,
            message.levels)
        await control.put(ack)
    case UnsubscribeMessage():
        ack = await ws.app.state.run(
            hub.unsubscribe, client_id, message.instruments,
            message.subscription_id)
        await control.put(ack)
    case PingMessage():
        await control.put({"op": "pong", "ts": _now_ms()})
    case StatusMessage():
        await control.put({"op": "status", **hub.status()})
```

- [ ] **Step 4: Run the API suite**

Run: `pytest gateway/tests/test_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/src/tradex_gateway/models.py gateway/src/tradex_gateway/app.py gateway/tests/test_api.py
git commit -m "fix(gateway): validate WebSocket frames and keep the connection alive"
```

### Task 3: Reject stale live depth

**Files:**
- Modify: `gateway/src/tradex_gateway/market.py`
- Test: `gateway/tests/test_market.py`

**Interfaces:**
- Produces: `DepthService(directory, router, live_depth=None, live_depth_ttl=5.0, clock=None)`; cached depth older than TTL falls through to REST with `degraded=true`.

- [ ] **Step 1: Add the failing test**

```python
def test_depth_cache_older_than_ttl_falls_back_to_rest():
    directory, router = _setup(FakeBroker("dhan"), FakeBroker("upstox"))
    stale = dataclasses.replace(
        make_depth(directory.resolve("IDX:NIFTY").instrument, levels=20),
        timestamp=datetime.now(UTC) - timedelta(minutes=1),
    )
    service = DepthService(
        directory, router,
        live_depth=lambda _: (stale, "dhan"),
        live_depth_ttl=5.0,
    )
    result = service.depth("IDX:NIFTY", 10)
    assert result["source"] == "rest"
    assert result["degraded"] is True
```

- [ ] **Step 2: Run and observe `ws_cache`**

Run: `pytest gateway/tests/test_market.py::test_depth_cache_older_than_ttl_falls_back_to_rest -v`
Expected: FAIL — source is `ws_cache`.

- [ ] **Step 3: Implement the freshness predicate**

```python
def _fresh(depth: Depth, now: datetime, ttl: float) -> bool:
    if depth.timestamp is None:
        return False
    return (now - depth.timestamp).total_seconds() <= ttl
```

In `DepthService.__init__`, store `live_depth_ttl` and `clock` (default `lambda: datetime.now(UTC)`). In `depth()`, only use the cache when `levels > 5`, the cache exists, and `_fresh(...)`; otherwise fall through to the REST branch with `degraded=levels > 5`.

- [ ] **Step 4: Run the market suite**

Run: `pytest gateway/tests/test_market.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/src/tradex_gateway/market.py gateway/tests/test_market.py
git commit -m "fix(gateway): refuse stale live depth snapshots"
```

### Task 4: Per-instrument staleness and a resilient monitor

**Files:**
- Modify: `gateway/src/tradex_gateway/live.py`, `gateway/src/tradex_gateway/market.py`
- Test: `gateway/tests/test_live.py`, `gateway/tests/test_live_failover.py`, `gateway/tests/test_market.py`

**Interfaces:**
- Produces: `LiveHub._stale_instruments(now, wanted) -> list[str]`; monitor switches only when **all** wanted instruments are stale and broadcasts degraded status when a subset changes.
- Produces: `CachedDepth(depth: Depth, broker: str, received_at: datetime)` in `market.py`; `LiveHub.latest_depth` returns it; `DepthService` freshness uses `received_at` (wall-clock receipt), not the exchange `depth.timestamp`.
- Carried from the Task 3 review (binding): the exchange last-trade timestamp is not a freshness clock for illiquid symbols; the hub must stamp receipt time when caching depth. `depth_payload` keeps `ts = depth.timestamp` (exchange time).

- [ ] **Step 1: Add the failing test**

```python
def test_staleness_is_reported_per_instrument():
    config = gateway_config(stale_after_seconds=15.0)
    dhan = FakeBroker("dhan")
    runtimes = {"dhan": BrokerRuntime("dhan", dhan)}
    hub = LiveHub(
        config,
        MarketDataRouter(config, runtimes),
        InstrumentDirectory(runtimes),
        runtimes,
    )
    hub._last_tick["IDX:NIFTY"] = 100.0
    hub._last_tick["NSE:RELIANCE"] = 50.0
    assert hub._stale_instruments(110.0, {"IDX:NIFTY", "NSE:RELIANCE"}) == [
        "NSE:RELIANCE"
    ]
    assert hub._stale_instruments(120.0, {"IDX:NIFTY", "NSE:RELIANCE"}) == [
        "IDX:NIFTY", "NSE:RELIANCE"
    ]
```

- [ ] **Step 2: Run and observe the missing method**

Run: `pytest gateway/tests/test_live.py::test_staleness_is_reported_per_instrument -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement the predicate and monitor cycle**

```python
def _stale_instruments(self, now: float, wanted: set[str]) -> list[str]:
    return sorted(
        key for key in wanted
        if now - self._last_tick.get(key, 0.0) > self._config.stale_after_seconds
    )

def _monitor_cycle(self) -> None:
    quotes, depths, _ = self._wanted()
    wanted = set(quotes) | set(depths)
    if not wanted:
        return
    now = self._clock()
    stale = self._stale_instruments(now, wanted)
    if set(stale) != self._stale_snapshot:
        self._stale_snapshot = set(stale)
        if stale:
            self._broadcast({
                "op": "status", "state": "degraded",
                "stale": stale, "broker": self._active,
            })
    if len(stale) != len(wanted) or now < self._next_switch_at:
        return
    stale_broker = self._active
    if stale_broker is None:
        self._next_switch_at = now + 10.0
        self._rebalance()
        return
    self._runtimes[stale_broker].health.record_failure("live", 15.0, "stale feed")
    self._next_switch_at = now + 10.0
    target = self._other_broker(stale_broker)
    if target is not None:
        self._switch_to(target, reason="stale_feed")
```

The loop becomes:

```python
while not self._stop.wait(interval):
    try:
        self._monitor_cycle()
    except Exception:  # noqa: BLE001
        log.exception("live monitor iteration failed")
```

Add `self._stale_snapshot: set[str] = set()` in `__init__`.

- [ ] **Step 4: Move depth freshness to hub receipt time (carried from the Task 3 review)**

In `market.py`:

```python
@dataclass(frozen=True)
class CachedDepth:
    depth: Depth
    broker: str
    received_at: datetime
```

Change `DepthService.__init__` to accept `live_depth: Callable[[str], CachedDepth | None] | None`; `depth()` uses `cached.depth`/`cached.broker` and `_fresh(cached.received_at, now, ttl)`. Keep the `_fresh` guards: `received_at` naive/None or future → stale.

In `live.py`, import `CachedDepth` from `market`; change `_last_depth` to `dict[str, CachedDepth]`; in `_publish_quote`/`_publish_depth` store `CachedDepth(depth, broker_name, received_at=datetime.now(UTC))`; `latest_depth` returns the `CachedDepth`. The `depth_payload` `ts` remains `depth.timestamp`.

Update `test_market.py` to construct `CachedDepth` with an explicit `received_at` for the fresh, stale, naive and future cases (e.g. `CachedDepth(depth=make_depth(...), broker="dhan", received_at=datetime.now(UTC) - timedelta(minutes=1))`). Also correct the `_switch_lock` comment in `live.py` (Task 1 deferred minor): it may span broker calls; the state lock `_lock` is never held across I/O.

- [ ] **Step 5: Run the live and market suites plus the full gateway suite**

Run: `pytest gateway/tests/test_live.py gateway/tests/test_live_failover.py gateway/tests/test_market.py gateway/tests -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add gateway/src/tradex_gateway/live.py gateway/src/tradex_gateway/market.py gateway/tests/test_live.py gateway/tests/test_market.py
git commit -m "fix(gateway): per-instrument staleness and receipt-time depth freshness"
```

### Task 5: Bound the token bucket and single-flight caches

**Files:**
- Create: `gateway/src/tradex_gateway/cache.py`
- Modify: `gateway/src/tradex_gateway/auth.py`, `history.py`, `market.py`, `live.py`
- Test: `gateway/tests/test_cache.py`, `gateway/tests/test_config.py`, `gateway/tests/test_live.py`

**Interfaces:**
- Produces: `TTLCache(maxsize=512)` and `SingleFlight` with `lock(key)` context manager; entries are removed when the last waiter leaves.
- Produces: `TokenBucket(rate, burst, max_keys=10_000, clock=...)` with LRU eviction.
- Carried from the Task 4 review (binding): `LiveHub._last_tick` and `_last_depth` must be pruned for instruments no longer wanted. Add `LiveHub._prune_state(wanted: set[str])` and call it from `_rebalance` after `_wanted()`; test unsubscribe removes both entries while keeping the still-wanted key.

- [ ] **Step 1: Add failing tests**

```python
def test_single_flight_removes_idle_entries():
    flight = SingleFlight()
    with flight.lock("k"):
        pass
    assert flight.entries == {}

def test_token_bucket_evicts_least_recently_used_key():
    clock = FakeClock(0.0)
    bucket = TokenBucket(rate=1.0, burst=1, max_keys=2, clock=clock)
    bucket.consume("a"); bucket.consume("b"); bucket.consume("c")
    assert len(bucket._buckets) == 2
    assert "a" not in bucket._buckets
```

- [ ] **Step 2: Run and observe failures**

Run: `pytest gateway/tests/test_cache.py gateway/tests/test_config.py -v`
Expected: FAIL — no `cache.py`, no `max_keys`.

- [ ] **Step 3: Extract and bound**

`cache.py`:

```python
@contextmanager
def lock(self, key: Hashable) -> Iterator[None]:
    with self._guard:
        entry = self._entries.setdefault(key, _Entry())
        entry.users += 1
    entry.lock.acquire()
    try:
        yield
    finally:
        entry.lock.release()
        with self._guard:
            entry.users -= 1
            if entry.users == 0:
                self._entries.pop(key, None)
```

`auth.py` uses `OrderedDict` with `max_keys`, evicting `popitem(last=False)` on insert and `move_to_end` on access. Update `history.py` and `market.py` to import from `cache.py` and use `with self._flight.lock(key):`.

- [ ] **Step 4: Prune hub state for unsubscribed instruments (carried from the Task 4 review)**

```python
def _prune_state(self, wanted: set[str]) -> None:
    with self._lock:
        for key in list(self._last_tick):
            if key not in wanted:
                self._last_tick.pop(key, None)
        for key in list(self._last_depth):
            if key not in wanted:
                self._last_depth.pop(key, None)
```

Call `self._prune_state(set(quotes) | set(depths))` from `_rebalance` after `quotes, depths, levels = self._wanted()`. Add a test in `test_live.py` that subscribes to two instruments, seeds `_last_tick`/`_last_depth` for both, unsubscribes one, and asserts the removed key is gone from both maps while the still-wanted key remains.

- [ ] **Step 5: Run the gateway suite**

Run: `pytest gateway/tests -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add gateway/src/tradex_gateway/cache.py gateway/src/tradex_gateway/auth.py gateway/src/tradex_gateway/history.py gateway/src/tradex_gateway/market.py gateway/src/tradex_gateway/live.py gateway/tests/test_cache.py gateway/tests/test_config.py gateway/tests/test_live.py
git commit -m "fix(gateway): bound cache state and prune stale hub entries"
```

### Task 6: Protect `/metrics` and stop leaking 500 details

**Files:**
- Modify: `gateway/src/tradex_gateway/app.py`
- Test: `gateway/tests/test_api.py`

- [ ] **Step 1: Add failing tests**

```python
def test_metrics_requires_api_key_when_configured():
    app, _, _ = _app(api_key="secret", allow_anon=False)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"X-API-Key": "secret"}).status_code == 200

def test_500_does_not_leak_exception_text():
    app, _, _ = _app()
    with TestClient(app) as client:
        app.state.history.history = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("secret-internal-detail")
        )
        response = client.get(
            "/v1/history/IDX:NIFTY",
            params={"from": "2026-11-06T09:15:00Z", "to": "2026-11-06T09:16:00Z"},
        )
        assert response.status_code == 500
        assert "secret-internal-detail" not in response.text
```

- [ ] **Step 2: Run and observe failures**

Run: `pytest gateway/tests/test_api.py -k "metrics or 500" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Middleware: guard `/metrics` with `check_api_key` when `cfg.api_key` is set (keep `/v1/health` open; do not apply the consumer bucket to `/metrics`). 500 handler: `log.exception(...)`, return `"message": "internal server error"`.

- [ ] **Step 4: Run the API suite**

Run: `pytest gateway/tests/test_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/src/tradex_gateway/app.py gateway/tests/test_api.py
git commit -m "fix(gateway): require auth for metrics and hide 500 internals"
```

### Task 7: Remove the broker-specific depth branch

**Files:**
- Modify: `gateway/src/tradex_gateway/live.py`
- Modify: `gateway/tests/gateway_support.py` (add `depth30_sub_calls`)
- Test: `gateway/tests/test_live.py`

- [ ] **Step 1: Add the failing test**

```python
def test_depth_30_is_selected_by_capability_not_broker_name():
    async def scenario() -> None:
        broker = FakeBroker("custom", depth_levels=30)
        runtime = BrokerRuntime("custom", broker)
        config = gateway_config(brokers=("custom",), primary_live="custom")
        hub = LiveHub(config, MarketDataRouter(config, {"custom": runtime}),
                      InstrumentDirectory({"custom": runtime}), {"custom": runtime})
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        control: asyncio.Queue = asyncio.Queue()
        hub.add_client("c1", loop, queue, control)
        hub.subscribe("c1", ["NSE:RELIANCE"], "depth", 10)
        assert broker.depth30_sub_calls == 1
        assert broker.depth_sub_calls == 0
        hub.stop()

    asyncio.run(scenario())
```

- [ ] **Step 2: Run and observe the name-based behavior**

Run: `pytest gateway/tests/test_live.py::test_depth_30_is_selected_by_capability_not_broker_name -v`
Expected: FAIL — `depth` is used because the name is not "upstox".

- [ ] **Step 3: Replace the branch**

```python
def _subscribe_depth(self, instruments, levels):
    declared = int(getattr(self.runtime.capabilities, "depth_levels", 0) or 0)
    deep = getattr(self.broker, "subscribe_depth_30", None)
    if levels > 5 and declared >= 30 and callable(deep):
        return deep(instruments, self._depth_cb)
    return self.broker.subscribe_depth(instruments, self._depth_cb)
```

Use it from `sync_depth`.

- [ ] **Step 4: Run the live suites**

Run: `pytest gateway/tests/test_live.py gateway/tests/test_live_failover.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/src/tradex_gateway/live.py gateway/tests/gateway_support.py gateway/tests/test_live.py
git commit -m "fix(gateway): choose depth stream by capability, not broker name"
```

### Task 8: Correct resolve availability and timestamp consistency

**Files:**
- Modify: `gateway/src/tradex_gateway/brokers.py`, `directory.py`, `app.py`, `history.py`
- Modify: `gateway/tests/gateway_support.py`, `gateway/tests/load_harness.py`
- Test: `gateway/tests/test_directory.py`, `gateway/tests/test_api.py`, `gateway/tests/test_history.py`
- Modify: `docs/superpowers/specs/2026-11-06-broker-data-gateway-design.md`

- [ ] **Step 1: Add failing tests**

```python
def test_resolve_only_reports_brokers_that_know_the_instrument():
    dhan = FakeBroker("dhan", universe=[equity("RELIANCE")])
    upstox = FakeBroker("upstox", universe=[])
    directory = _directory(dhan, upstox)
    resolved = directory.resolve("NSE:RELIANCE")
    assert resolved.brokers == ("dhan",)

def test_naive_history_timestamps_are_rejected():
    app, _, _ = _app()
    with TestClient(app) as client:
        response = client.get(
            "/v1/history/IDX:NIFTY",
            params={"from": "2026-11-06T09:15:00", "to": "2026-11-06T09:16:00"},
        )
        assert response.status_code == 400
```

- [ ] **Step 2: Run and observe failures**

Run: `pytest gateway/tests/test_directory.py gateway/tests/test_api.py -k "lot_size or naive" -v`
Expected: FAIL.

- [ ] **Step 3: Implement `known()` and reject naive input**

`BrokerRuntime.known(instrument_id) -> bool` returns `bool(registry.meta(instrument_id))` (guarded). `directory.resolve` appends a broker only when `runtime.available and runtime.known(id)`.
`FakeRegistry` gains a `known: set[str]` and returns `{"known": True}` for known IDs; `FakeBroker` seeds it from its universe; `ChaosBroker` does the same.
`app._query_moment` and `history._parse_moment` raise `BadRequest("timestamp must include a UTC offset")` when `tzinfo is None`.

- [ ] **Step 4: Run the gateway suite**

Run: `pytest gateway/tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/src/tradex_gateway/brokers.py gateway/src/tradex_gateway/directory.py gateway/src/tradex_gateway/app.py gateway/src/tradex_gateway/history.py gateway/tests/gateway_support.py gateway/tests/load_harness.py gateway/tests/test_directory.py gateway/tests/test_api.py gateway/tests/test_history.py docs/superpowers/specs/2026-11-06-broker-data-gateway-design.md
git commit -m "fix(gateway): report only known brokers and require offset timestamps"
```

---

## Phase 2 — P1 structure and policy

### Task 9: Split `app.py` into routers (move-only)

**Files:**
- Create: `gateway/src/tradex_gateway/routes/__init__.py`, `deps.py`, `health.py`, `instruments.py`, `history.py`, `quotes.py`, `depth.py`, `chains.py`, `metrics.py`, `stream.py`
- Modify: `gateway/src/tradex_gateway/app.py`

**Rule:** move-only; no behavior changes. The route functions keep their names and signatures; they read services from `request.app.state`.
- `health` → `/v1/health`, `/v1/brokers`
- `instruments` → search/resolve
- `history` → single/batch
- `quotes` → quote/batch
- `depth` → depth
- `chains` → option-chain/futures
- `metrics` → `/metrics`
- `stream` → `/v1/stream` (the WS handler)
- `deps.py` → `state(request)`, `require_api_key` helpers, connection limiter.

- [ ] Move each nested route body verbatim; mount `APIRouter`s in `create_app`.
- [ ] Run `pytest gateway/tests -v` → 40+ pass.
- [ ] Run ruff and mypy.
- [ ] Commit: `refactor(gateway): split the app factory into routers`.

### Task 10: Router failure policy table and narrow fallbacks

**Files:**
- Modify: `gateway/src/tradex_gateway/router.py`, `market.py`
- Test: `gateway/tests/test_router.py`, `gateway/tests/test_market.py`

- [ ] Replace the repeated `except` branches with a policy table:
  ```python
  _POLICIES: tuple[tuple[tuple[type[BaseException], ...], float], ...] = (
      ((RateLimitError,), 30.0),
      ((ConnectionTimeoutError, BrokerUnavailableError), 15.0),
      ((AuthenticationError,), 60.0),
      ((SDKError,), 5.0),
  )
  ```
  Unknown `Exception` logs with `log.exception` and records 5 s. Keep `ValueError` / `InstrumentNotFoundError` / `CapabilityNotSupportedError` as non-cooling fallthrough.
- [ ] Remove `_DEFAULT_COOLDOWNS` and the unused `_lock`.
- [ ] `QuoteService.quotes` catches **only** `CapabilityUnsupported` for the per-instrument fallback; `NoHealthyBroker`/`BrokerRateLimited` return the error.
- [ ] `ChainService._enrich` passes `preferred=name` to the quote router.
- [ ] Tests: quote-batch outage does not fan out (fake `get_quote` calls stay 0); chain enrichment uses the chain broker; existing router tests pass.
- [ ] Run the gateway suite; commit: `refactor(gateway): consolidate failure policy and narrow quote fallback`.

### Task 11: Broker-owned lot-size normalization, per-broker chunk caps, shared batch executor

**Files:**
- Modify: `brokers/src/tradex_brokers/dhan/adapter.py`, `upstox/adapter.py` (normalize lot size to a canonical string)
- Modify: `domain/src/tradex_domain/capabilities.py` (add `max_history_days: int | None`)
- Modify: `brokers/src/tradex_brokers/dhan/capabilities.py`, `upstox/capabilities.py` (90 / 30)
- Modify: `gateway/src/tradex_gateway/history.py`, `app.py`
- Tests: `brokers/tests/test_v3_port.py`, `gateway/tests/test_history.py`

- [ ] Normalize `"500.0"` to `"500"` in `_extra_row_meta` or the master normalizer; keep the gateway coercion as defense.
- [ ] Add the capability and use it in `_fetch_chunks` (pass the selected broker’s cap; no `min(90, 30)` for Dhan).
- [ ] Pass `app.state.executor` into `HistoryService` and have `batch` submit to it instead of creating a nested pool.
- [ ] Tests: capability table values; Dhan 90-day window count; batch uses the injected executor.
- [ ] Run brokers + gateway suites; commit.

### Task 12: Dead-code and config cleanup

**Files:**
- Modify: `gateway/src/tradex_gateway/directory.py`, `config.py`, `history.py`, `router.py`, `gateway/pyproject.toml`
- Test: `gateway/tests/test_config.py`

- [ ] Delete `directory.candidates`, `directory.asset_class_of`, `GatewayConfig.metadata`, the `history` `per_broker` parameters, and any remaining unused router state.
- [ ] Make `GatewayConfig.validate` return `dataclasses.replace(self, ...)`; validate `broker_workers > 0`, `stale_after_seconds > 0`, `max_subscriptions > 0`, and unknown broker names.
- [ ] Move `httpx` out of runtime dependencies into a test-only extra (or the root dev group); ensure `uv lock` updates.
- [ ] Run the gateway suite; commit: `chore(gateway): remove dead code and harden config validation`.

---

## Phase 3 — P2 polish

### Task 13: Observability and frame polish

- [ ] Add wall-clock `last_success` to `BrokerHealth.snapshot` (or label the monotonic field).
- [ ] Make WS `gap` frames carry a real `from_ms` (store wall-clock last-tick per instrument) or drop the field.
- [ ] Document `lot_size` precedence (first broker in `brokers` order) and the per-broker map.
- [ ] Replace the WS writer’s per-frame task pair with persistent consumers or a priority queue.
- [ ] Parallelize `build_runtimes` startup (thread per broker) and keep per-broker failure isolation.
- [ ] Run the full gateway suite + ruff + mypy; commit.

### Task 14: Final verification

- [ ] `pytest gateway/tests -q` → all pass.
- [ ] `ruff check` + `ruff format --check` → clean.
- [ ] `mypy gateway/src` → success; `python scripts/mypy_ratchet.py` → at or below baseline.
- [ ] Full suite: expect the **6 known pre-existing failures** only; confirm no new failures.
- [ ] Opt-in live smoke: one index quote/history, one RELIANCE future/option for both brokers; verify lot_size and endpoint shapes.
- [ ] Update the design spec with the new timestamp rule and depth-cache freshness behavior.
- [ ] Commit the final verification notes (if any) and report.

## Self-Review

- **Spec coverage:** concurrency (Task 1), WS validation (Task 2), depth freshness (Task 3), staleness/monitor (Task 4), bounds/security (Tasks 5–6), broker leak (Task 7), availability/timestamps (Task 8), structure (Task 9), policy/fallbacks (Task 10), broker normalization/chunking/batch (Task 11), dead code/config (Task 12), polish (Task 13), verification (Task 14).
- **No placeholders:** every task names files, interfaces, a failing test or a concrete move list, and the verification command.
- **Type consistency:** `known()`, `_stale_instruments()`, `lock()`, `max_history_days`, `ClientMessage`, `live_depth_ttl` are defined where first used and match later references.
- **Out of scope:** the 6 pre-existing suite failures, the legacy `interfaces` app, multi-process deployment, live WS failover drills.
