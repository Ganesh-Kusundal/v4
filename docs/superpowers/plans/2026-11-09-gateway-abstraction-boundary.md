# Gateway Abstraction Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The gateway serves the broker standard interface through trading sessions only, using canonical domain naming on every input and output — no provider keys, security IDs, or broker-internal error text ever reach the wire.

**Architecture:** Route broker construction through `boot()` → `TradingSession` → `session.broker` (the standard interface that manages mapping, token generation, and registry internally), replacing the gateway's direct `build_broker_from_env` calls. Sanitize at the gateway boundary (router/directory/health), never in brokers — brokers keep internal messages for logs; the gateway translates to canonical wire messages.

**Tech Stack:** Python, FastAPI, Pydantic, pytest (gateway/tests with FakeBroker/FakeSession doubles).

**Spec:** `docs/superpowers/plans/2026-11-06-broker-data-gateway.md` (gateway design) + operator requirements from 2026-11-09 session: (1) gateway must use the broker standard interface as exposed through the trading session; (2) security IDs are internal to the broker and must not be exposed by the API; (3) input/output must respect domain objects and canonical naming.

## Global Constraints

- The gateway stays **read-only**: sessions boot with `live_orders_enabled=False` (the `serve_config` default), so `allow_order_operations` is False on every session broker. No order routes are added.
- Broker **names** (`dhan`/`upstox`) are public operator config, not internal IDs — allowed in responses (`"broker":"upstox"`, `per_broker` keys).
- Canonical ID format is `EXCHANGE:SYMBOL[:EXPIRY:STRIKE:RIGHT]` via `InstrumentId.parse` / `str(instrument_id)` — the only ID spelling on the wire, in either direction.
- Live `boot()` requires risk limits (AC-10): the operator must set `TRADEX_RISK_*` env vars, exactly as for `tradex serve`. The gateway does not invent defaults.
- Live `boot()` requires a feed tape (AC-20): if `TRADEX_FEED_INSTRUMENTS` is unset, the gateway boots each session with a minimal default tape (`IDX:NIFTY`) so the gate passes; the session feed itself is unused by the gateway (LiveHub owns subscriptions).
- All 87 existing gateway tests must keep passing (adapted only where the session-routing seam changes their doubles).

## Review Focus

The five failure modes most likely to bite, most likely first:

1. **Unknown instrument** (`GET /v1/quote/NSE:NONEXISTENT123`) — today returns `"no provider key for NSE:NONEXISTENT123"`, leaking internal resolution mechanics. Expected: canonical `"unknown instrument: NSE:NONEXISTENT123"`.
2. **Broker failure** (rate limit / auth / timeout) — today the router passes `str(last)` (the broker exception text, which can carry URLs and provider details) into `NoHealthyBroker`/`BrokerUnavailable`. Expected: canonical message naming the capability, no exception text.
3. **Numeric security ID as input** (`GET /v1/quote/NSE:14219`) — today parses as canonical (exchange=NSE, underlying=14219) and fails downstream. Expected: rejected at the boundary as non-canonical (`invalid instrument id`), since a purely numeric underlying is a security ID, not a trading symbol.
4. **Health/brokers endpoints** — today `/v1/health` exposes `last_error` (broker exception text) and `/v1/brokers` exposes build exception text. Expected: error *class* only, never message text.
5. **Malformed derivative ID** (option without strike, e.g. `NFO:NIFTY:20261109:CE`) — today `directory.resolve` passes `instrument_from_id`'s message through (`"option id has no strike: ..."`). Expected: canonical `invalid instrument id` message.

---

## File Structure

| File | Responsibility |
|---|---|
| `gateway/src/tradex_gateway/brokers.py` | `build_runtimes` (session factory seam), `BrokerRuntime` (holds session, `close()` → `session.stop()`), `BrokerHealth` (error class only) |
| `gateway/src/tradex_gateway/router.py` | Canonical error messages; no `str(exc)` pass-through |
| `gateway/src/tradex_gateway/directory.py` | `parse_canonical` (reject numeric underlyings), canonical `InstrumentNotFound` messages |
| `gateway/src/tradex_gateway/routes/health.py` | No change expected (reads sanitized snapshot/error) |
| `gateway/tests/gateway_support.py` | `FakeSession` double (`.broker`, `.stop()`) |
| `gateway/tests/test_router.py` | Router sanitization tests |
| `gateway/tests/test_directory.py` | Input canonicalization tests |
| `gateway/tests/test_health.py` | Health sanitization tests |
| `gateway/tests/test_api.py` | Output canonicalization + parity tests |
| `gateway/tests/test_brokers.py` (new) | Session-routing tests for `build_runtimes` |

---

### Task 1: Route broker construction through trading sessions

**Files:**
- Modify: `gateway/src/tradex_gateway/brokers.py:90-192` (`BrokerRuntime`, `build_runtimes`)
- Modify: `gateway/tests/gateway_support.py` (add `FakeSession`)
- Test: `gateway/tests/test_brokers.py` (new)

**Interfaces:**
- Consumes: `tradex_config.env.from_env`, `tradex_config.env.serve_config`, `tradex_runtime.startup.boot`, `tradex_domain.BrokerId`
- Produces: `build_runtimes(config, *, session_factory=None) -> dict[str, BrokerRuntime]`; `BrokerRuntime(name, broker, *, session=None, error=None)`; `BrokerRuntime.close()` stops the session when present, else closes the broker

- [ ] **Step 1: Write the failing tests**

```python
# gateway/tests/test_brokers.py
from gateway_support import FakeBroker, FakeSession, gateway_config
from tradex_gateway.brokers import BrokerRuntime, build_runtimes


def test_build_runtimes_uses_session_factory_and_exposes_session_broker():
    broker = FakeBroker("dhan")
    session = FakeSession(broker)
    runtimes = build_runtimes(gateway_config(brokers=("dhan",)), session_factory=lambda name: session)
    assert runtimes["dhan"].broker is broker
    assert runtimes["dhan"].available


def test_runtime_close_stops_the_session():
    broker = FakeBroker("dhan")
    session = FakeSession(broker)
    runtime = BrokerRuntime("dhan", broker, session=session)
    runtime.close()
    assert session.stopped


def test_failed_session_boot_leaves_broker_unavailable():
    def boom(name):
        raise RuntimeError("boot failed")

    runtimes = build_runtimes(gateway_config(brokers=("dhan",)), session_factory=boom)
    assert runtimes["dhan"].available is False
    assert runtimes["dhan"].error is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest gateway/tests/test_brokers.py -v`
Expected: FAIL — `build_runtimes` has no `session_factory` parameter; `BrokerRuntime` has no `session` parameter; `FakeSession` does not exist.

- [ ] **Step 3: Add `FakeSession` to `gateway/tests/gateway_support.py`**

```python
class FakeSession:
    """TradingSession double: exposes the standard broker interface."""

    def __init__(self, broker):
        self.broker = broker
        self.stopped = False

    def stop(self):
        self.stopped = True
```

- [ ] **Step 4: Implement session routing in `gateway/src/tradex_gateway/brokers.py`**

`BrokerRuntime.__init__` gains `session: Any | None = None`, stored as `self._session`. `close()` calls `self._session.stop()` when a session is present, else `self.broker.close()`.

`build_runtimes` gains `session_factory: Callable[[str], Any] | None = None`. When None, it uses `_boot_session(name)`:

```python
def _boot_session(name: str):
    """Boot one live trading session; the session owns broker mapping/token."""
    from tradex_config.env import from_env, serve_config
    from tradex_domain import BrokerId
    from tradex_runtime.startup import boot

    config = serve_config(from_env(), broker_id=BrokerId(name.upper()), mode="live")
    # Live boot refuses an empty tape (AC-20). The session feed is unused by
    # the gateway (LiveHub owns subscriptions), so a minimal default tape
    # satisfies the gate when the operator named none.
    tape = config.feed_instruments or parse_feed_instruments("IDX:NIFTY")
    return boot(config, wire_strategies=False, feed_instruments=tape)
```

`build_runtimes` calls `session = factory(name)` then `BrokerRuntime(name, session.broker, session=session)`. The `builder` parameter is removed. `master_cache_dir` is no longer passed (boot owns the cache location); note it in the module docstring.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest gateway/tests/test_brokers.py gateway/tests/test_api.py gateway/tests/test_live.py -v`
Expected: PASS. (Existing suites pass `builder=` nowhere — verify with `grep -rn "builder=" gateway/tests/`; if a test uses it, update it to `session_factory=` with a `FakeSession`.)

- [ ] **Step 6: Commit**

```bash
git add gateway/src/tradex_gateway/brokers.py gateway/tests/gateway_support.py gateway/tests/test_brokers.py
git commit -m "gateway: build brokers through trading sessions"
```

---

### Task 2: Sanitize router error pass-throughs

**Files:**
- Modify: `gateway/src/tradex_gateway/router.py:131-153`
- Test: `gateway/tests/test_router.py`

**Interfaces:**
- Consumes: `tradex_gateway.errors.InstrumentNotFound`, `NoHealthyBroker`, `BrokerUnavailable`
- Produces: canonical wire messages — `"unknown instrument"`, `"all {capability} brokers failed"`, `"no broker is configured for {capability}"` (unchanged)

- [ ] **Step 1: Write the failing tests**

```python
def test_unknown_instrument_message_is_canonical():
    clock = FakeClock()
    dhan = FakeBroker("dhan")
    upstox = FakeBroker("upstox")
    runtimes = {
        "dhan": BrokerRuntime("dhan", dhan, clock=clock),
        "upstox": BrokerRuntime("upstox", upstox, clock=clock),
    }
    router = MarketDataRouter(gateway_config(), runtimes, clock=clock)

    def call(broker):
        raise InstrumentNotFoundError("no provider key for NSE:NONEXISTENT123")

    with pytest.raises(InstrumentNotFound) as excinfo:
        router.call("quotes", call)
    assert "provider key" not in excinfo.value.message
    assert excinfo.value.message == "unknown instrument"


def test_broker_failure_message_hides_exception_text():
    clock = FakeClock()
    dhan = FakeBroker("dhan")
    upstox = FakeBroker("upstox")
    runtimes = {
        "dhan": BrokerRuntime("dhan", dhan, clock=clock),
        "upstox": BrokerRuntime("upstox", upstox, clock=clock),
    }
    router = MarketDataRouter(gateway_config(), runtimes, clock=clock)

    def call(broker):
        raise RateLimitError("https://api.example.com/secret?key=abc rate limited")

    with pytest.raises(NoHealthyBroker) as excinfo:
        router.call("quotes", call)
    assert "api.example.com" not in excinfo.value.message
    assert "secret" not in excinfo.value.message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest gateway/tests/test_router.py::test_unknown_instrument_message_is_canonical gateway/tests/test_router.py::test_broker_failure_message_hides_exception_text -v`
Expected: FAIL — messages contain `"no provider key"` / the URL.

- [ ] **Step 3: Implement canonical messages in `router.py`**

Replace the three `str(last)` pass-throughs:
- `InstrumentNotFound(str(last))` → `InstrumentNotFound("unknown instrument")`
- `NoHealthyBroker(str(last) if last else ...)` → `NoHealthyBroker(f"all {capability} brokers failed", retry_after=retry_after)`
- `BrokerUnavailable(str(last) if last else ...)` → `BrokerUnavailable(f"all {capability} brokers failed", retry_after=retry_after)`

The full exception stays in logs (`log.exception` at the failure site already records it).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest gateway/tests/test_router.py -v`
Expected: PASS (all existing router tests + the two new ones).

- [ ] **Step 5: Commit**

```bash
git add gateway/src/tradex_gateway/router.py gateway/tests/test_router.py
git commit -m "gateway: canonical router error messages"
```

---

### Task 3: Canonical directory errors + input canonicalization

**Files:**
- Modify: `gateway/src/tradex_gateway/directory.py:20-38` (`parse_canonical`), `:81-86` (`resolve`)
- Test: `gateway/tests/test_directory.py`

**Interfaces:**
- Consumes: `tradex_gateway.errors.BadRequest`, `InstrumentNotFound`
- Produces: `parse_canonical(text, asset_class=None) -> InstrumentId` rejecting purely numeric underlyings; `resolve` raising canonical `InstrumentNotFound("unknown instrument")`

- [ ] **Step 1: Write the failing tests**

```python
def test_numeric_underlying_is_rejected_as_non_canonical():
    with pytest.raises(BadRequest):
        parse_canonical("NSE:14219")


def test_provider_key_and_isin_inputs_are_rejected():
    with pytest.raises(BadRequest):
        parse_canonical("NSE_EQ|INE121A01024")
    with pytest.raises(BadRequest):
        parse_canonical("INE121A01024")


def test_malformed_derivative_id_message_is_canonical():
    directory = InstrumentDirectory({})
    with pytest.raises(InstrumentNotFound) as excinfo:
        directory.resolve("NFO:NIFTY:20261109:CE")
    assert "no strike" not in excinfo.value.message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest gateway/tests/test_directory.py -v -k "numeric or provider_key or malformed"`
Expected: FAIL — `NSE:14219` parses; malformed derivative message contains `"no strike"`.

- [ ] **Step 3: Implement in `directory.py`**

In `parse_canonical`, after `InstrumentId.parse(raw)` succeeds, reject a purely numeric underlying:

```python
parsed = InstrumentId.parse(raw)
if parsed.underlying.isdigit():
    raise BadRequest(f"invalid instrument id: {text!r}")
return parsed
```

In `resolve`, replace `raise InstrumentNotFound(str(exc))` with `raise InstrumentNotFound("unknown instrument")`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest gateway/tests/test_directory.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/src/tradex_gateway/directory.py gateway/tests/test_directory.py
git commit -m "gateway: canonical instrument id input"
```

---

### Task 4: Sanitize health/build error exposure

**Files:**
- Modify: `gateway/src/tradex_gateway/brokers.py:69-74` (`record_failure`), `:190-191` (`build_runtimes` error)
- Test: `gateway/tests/test_health.py`

**Interfaces:**
- Consumes: `BrokerHealth.record_failure(capability, seconds, message)` — signature unchanged; the *stored* value changes to the exception class name
- Produces: `snapshot()["last_error"]` and `BrokerRuntime.error` carry error class names only (e.g. `"RateLimitError"`), never message text

- [ ] **Step 1: Write the failing tests**

```python
def test_health_snapshot_hides_exception_text():
    health = BrokerHealth()
    health.record_failure("quotes", 30.0, "https://api.example.com/secret?key=abc")
    snapshot = health.snapshot()
    assert "api.example.com" not in str(snapshot)
    assert snapshot["quotes"]["last_error"] == "RateLimitError"  # or the class passed in
```

Note: `record_failure` receives a message string today. Change the call sites to pass the exception *class name* (`type(exc).__name__`) instead of `str(exc)`. Update the test to record a class name and assert the snapshot contains no URL/secret text.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest gateway/tests/test_health.py -v -k hides_exception`
Expected: FAIL — snapshot contains the URL.

- [ ] **Step 3: Implement in `brokers.py` + call sites**

- `record_failure`: store the message as given (callers now pass class names).
- `router.py:118`: `health.record_failure(capability, cooldown, type(exc).__name__)`
- `live.py:428,478,556`: same change (`type(exc).__name__` / `"StaleFeed"` for the stale case).
- `build_runtimes`: `error=type(exc).__name__` instead of `f"{type(exc).__name__}: {exc}"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest gateway/tests/ -q`
Expected: PASS (all 87 existing + new).

- [ ] **Step 5: Commit**

```bash
git add gateway/src/tradex_gateway/brokers.py gateway/src/tradex_gateway/router.py gateway/src/tradex_gateway/live.py gateway/tests/test_health.py
git commit -m "gateway: error classes only in health surface"
```

---

### Task 5: Output canonicalization + interface parity tests

**Files:**
- Test: `gateway/tests/test_api.py` (append)

**Interfaces:**
- Consumes: the full route table (`/v1/quote`, `/v1/quotes`, `/v1/depth`, `/v1/history`, `/v1/option-chain`, `/v1/futures`, `/v1/instruments/search`, `/v1/instruments/resolve`, `/v1/health`, `/v1/brokers`, `/v1/stream`)
- Produces: regression tests pinning (a) no internal IDs in any response, (b) every market-data `BaseBroker` method has a gateway route

- [ ] **Step 1: Write the failing tests**

```python
INTERNAL_ID_PATTERNS = [r"NSE_EQ\|", r"NSE_OPT\|", r"\bINE\d{5}[A-Z]{2}\d{4}\b", r"provider key"]


def test_no_response_contains_internal_ids(client):
    import re
    urls = [
        "/v1/quote/NSE:RELIANCE",
        "/v1/depth/NSE:RELIANCE",
        "/v1/history/NSE:RELIANCE?timeframe=1m&from=2026-11-09T09:15:00%2B05:30&to=2026-11-09T09:20:00%2B05:30",
        "/v1/option-chain/NSE:NIFTY",
        "/v1/futures/NSE:NIFTY",
        "/v1/instruments/search?q=REL",
        "/v1/instruments/resolve?q=NSE:RELIANCE",
        "/v1/health",
        "/v1/brokers",
    ]
    for url in urls:
        response = client.get(url)
        body = response.text
        for pattern in INTERNAL_ID_PATTERNS:
            assert not re.search(pattern, body), f"{pattern} leaked in {url}"


def test_market_data_surface_parity():
    """Every market-data BaseBroker method has a gateway route."""
    from tradex_brokers.common.base import BaseBroker
    expected = {
        "get_quote": "/v1/quote/",
        "quote_batch": "/v1/quotes",
        "depth": "/v1/depth/",
        "history": "/v1/history/",
        "search": "/v1/instruments/search",
        "get_option_chain": "/v1/option-chain/",
        "future_chain": "/v1/futures/",
    }
    for method, route in expected.items():
        assert hasattr(BaseBroker, method), method
        assert route in _route_paths(), route
```

(`_route_paths()` reads the FastAPI app's route table from the test client fixture.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest gateway/tests/test_api.py -v -k "internal_ids or parity"`
Expected: FAIL if any leak remains (e.g. `"provider key"` in an error body); PASS for parity if the route table matches. Fix any leak found by the test in the owning task's file, not here.

- [ ] **Step 3: Run the full gateway suite**

Run: `.venv/bin/python -m pytest gateway/tests/ -q`
Expected: PASS (all tests).

- [ ] **Step 4: Live verification against the running gateway**

Restart the gateway (`tradex-gateway serve` with `GATEWAY_ALLOW_ANON=true` and `TRADEX_RISK_*` set) and confirm:
- `GET /v1/quote/NSE:NONEXISTENT123` → `"unknown instrument: ..."` (no "provider key")
- `GET /v1/quote/NSE:14219` → `invalid_request`
- `GET /v1/health` → `last_error` is a class name or null
- `GET /v1/quote/NSE:RELIANCE` → 200 with canonical ID

- [ ] **Step 5: Commit**

```bash
git add gateway/tests/test_api.py
git commit -m "gateway: canonical output + parity regression tests"
```

---

## Self-Review

**1. Spec coverage:**
- "Gateway must use the broker standard interface through the trading session" → Task 1 (session routing; `session.broker` is the standard interface; mapping/token managed inside `boot()`).
- "Security IDs are internal to the broker" → Tasks 2–5 (error sanitization, input rejection, output regression tests).
- "Input/output must respect domain objects and canonical naming" → Tasks 3 (input) and 5 (output).
- "Same standard interface exposed through the API" → Task 5 parity test (market-data surface); account/orders/master-refresh explicitly stay session-side (Global Constraints: gateway is read-only).

**2. Step scan:** Each step is one action (write test / run / implement / run / commit). Code steps carry exact signatures and the one-line approach; bodies appear only where the signature doesn't determine them (the `_boot_session` helper, the numeric-underlying check).

**3. Type consistency:** `build_runtimes(config, *, session_factory=None)` and `BrokerRuntime(name, broker, *, session=None, error=None)` are used identically in Tasks 1 and 4. `record_failure(capability, seconds, message)` signature is unchanged (Task 4 changes what callers pass). `parse_canonical` signature unchanged (Task 3).

**4. Review Focus:** All five failure modes map to tasks: (1)→Task 2, (2)→Task 2, (3)→Task 3, (4)→Task 4, (5)→Task 3.

**5. Proportion:** Six tasks for a boundary-hardening change. Each task is independently testable and committable. No task restates another's code.

## Known consequences (not tasks)

- `GATEWAY_MASTER_CACHE_DIR` becomes unused (boot owns the master cache location). Remove it from `GatewayConfig` in a follow-up, or leave as a documented no-op.
- Two live sessions in one process acquire two writer locks (one per broker) — correct, but running `tradex serve --broker DHAN` alongside the gateway will fail the second boot. Document in the gateway README/runbook.
- The session's MarketFeed subscribes to the minimal tape (`IDX:NIFTY` default) in addition to LiveHub subscriptions — one extra broker subscription per session, accepted for gate compliance.
