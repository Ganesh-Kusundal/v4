# Architecture Simplification — Remove Dead Forwarding Layers

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove unnecessary forwarding layers (MarketService, ExtensionService, session.account alias, _client/_transport duplication) so callers use the broker directly.

**Architecture:** The broker already implements all market data, extension, and portfolio methods with capability gating in BaseBroker. Services that merely forward to the broker add indirection without domain behavior. After removal, callers use `session.broker.*` for reads and `session.trade.*` for order orchestration (the one service that adds real value via ExecutionEngine).

**Tech Stack:** Python 3.12+, tradex_domain (protocols, capabilities), tradex_brokers (BaseBroker, DhanBroker, UpstoxBroker, PaperBroker), tradex_trading (TradingSession, services, FastAPI interface)

---

## Dependency Graph

```
Task 1 (BaseBroker cleanup) ──────────┐
                                      ├──→ Task 3 (remove MarketService)
Task 2 (session.account removal) ─────┤
                                      │
Task 4 (verify_connection fix) ───────┘   (independent)
                                      
Task 3 ──→ Task 5 (remove ExtensionService)
Task 3 ──→ Task 6 (simplify TradingSession construction)
Task 3 ──→ Task 7 (update all tests)
```

**Parallel groups:**
- **Group A** (parallel): Tasks 1, 2, 4
- **Group B** (after A): Task 3
- **Group C** (after B, parallel): Tasks 5, 6, 7

---

## Task 1: BaseBroker Cleanup — Remove _client Alias + Fix verify_connection

**Files:**
- Modify: `brokers/src/tradex_brokers/common/base.py`
- Test: `brokers/tests/common/test_broker_contracts.py`

**Rationale:** `_client` and `_transport` are aliases for the same object (line 67-70). Subclass adapters use `_transport` exclusively (25+ references). `verify_connection()` invalidates the read cache and makes a real API call just to check health — destructive and rate-limit-consuming.

- [ ] **Step 1: Remove `_client` alias, unify on `_transport`**

In `brokers/src/tradex_brokers/common/base.py`:

```python
# BEFORE (lines 51, 67-70):
_client: Any
# ...
self._client = transport
# ``_transport`` kept as an alias so broker streaming code that already
# references it continues to work after extraction.
self._transport = transport

# AFTER:
_transport: Any
# ...
self._transport = transport
```

Update all `_client` references in base.py to `_transport`:

```python
# Line 98 (connect):
# BEFORE: if self._client is None:
# AFTER:  if self._transport is None:

# Line 143 (_require):
# BEFORE: if self._client is None or not self._connected:
# AFTER:  if self._transport is None or not self._connected:

# Line 147 (_require return):
# BEFORE: return self._client
# AFTER:  return self._transport
```

Also update the class-level annotation:
```python
# BEFORE:
_client: Any
# AFTER:
_transport: Any
```

Remove the now-redundant comment about _transport alias.

- [ ] **Step 2: Fix verify_connection() — non-destructive local check**

Replace the current verify_connection that invalidates cache and makes an API call:

```python
# BEFORE (lines 120-129):
def verify_connection(self) -> bool:
    """Auth-verification probe; returns True on success."""
    transport = self._require()
    if hasattr(transport, "invalidate_read_cache"):
        transport.invalidate_read_cache()
    try:
        transport.get_account()
        return True
    except Exception:
        return False

# AFTER:
def verify_connection(self) -> bool:
    """Non-destructive health check: local state first, cheap probe second.

    Returns True only when the broker is connected AND the transport
    can reach the provider account endpoint.  Does NOT invalidate the
    read cache — verification must not mutate state.
    """
    if self._transport is None or not self._connected:
        return False
    try:
        self._transport.get_account()
        return True
    except Exception:
        return False
```

Key changes: removed `invalidate_read_cache()` call, removed `_require()` (which raises — verify should return False, not raise), explicit local check first.

- [ ] **Step 3: Run existing broker tests**

```bash
cd brokers && python -m pytest tests/common/test_broker_contracts.py -v
```

Expected: All pass. The `test_verify_connection_probes_account` test should still pass — it verifies that verify_connection calls `get_account()` and returns True/False.

- [ ] **Step 4: Run full broker test suite**

```bash
cd brokers && python -m pytest -v
```

Expected: All pass. No external code references `_client` except base.py itself.

- [ ] **Step 5: Commit**

```bash
git add brokers/src/tradex_brokers/common/base.py
git commit -m "refactor: unify _client/_transport, fix verify_connection to be non-destructive"
```

---

## Task 2: Remove session.account Alias

**Files:**
- Modify: `trading/src/tradex_trading/sdk/session.py`
- Modify: `trading/tests/sdk/test_session_services.py`

**Rationale:** `session.account` is an alias of `session.portfolio`. Two names for one concept is confusion, not convenience.

- [ ] **Step 1: Remove the `account` property from TradingSession**

In `trading/src/tradex_trading/sdk/session.py`, delete lines 212-222:

```python
# DELETE THIS ENTIRE PROPERTY:
@property
def account(self) -> PortfolioService:
    """Account/portfolio facade — positions, holdings, funds.

    Alias of :attr:`portfolio` so ``session.account.positions()``,
    ``session.account.funds()`` and ``session.account.holdings()`` read
    the same surface as ``session.portfolio.*`` (v3-style convenience;
    ``session.portfolio.account()`` remains the canonical account
    snapshot call).
    """
    return self.portfolio
```

- [ ] **Step 2: Update test**

In `trading/tests/sdk/test_session_services.py`, find the test class for `session.account` (around line 917):

```python
# DELETE the entire test class for session.account alias:
class TestSessionAccountAlias:
    """session.account is the portfolio facade (positions/funds/holdings)."""
    # ... (all test methods in this class)
```

Replace with a single assertion in the PortfolioService test that `session.account` is NOT present:

```python
def test_session_has_no_account_alias():
    """session.account was removed — use session.portfolio."""
    session = TradingSession.paper()
    assert not hasattr(session, "account") or not isinstance(
        getattr(type(session), "account", None), property
    )
    session.stop()
```

- [ ] **Step 3: Run tests**

```bash
cd trading && python -m pytest tests/sdk/test_session_services.py -v -k "account or portfolio"
```

- [ ] **Step 4: Commit**

```bash
git add trading/src/tradex_trading/sdk/session.py trading/tests/sdk/test_session_services.py
git commit -m "refactor: remove session.account alias — use session.portfolio"
```

---

## Task 3: Remove MarketService — Callers Use Broker Directly

**Files:**
- Modify: `trading/src/tradex_trading/sdk/session.py`
- Modify: `trading/src/tradex_trading/interface/fastapi_app.py`
- Modify: `trading/src/tradex_trading/interface/cli.py`
- Modify: `trading/src/tradex_trading/sdk/services/__init__.py`
- Modify: `trading/src/tradex_trading/sdk/__init__.py`
- Modify: `trading/src/tradex_trading/__init__.py`
- Delete: `trading/src/tradex_trading/sdk/services/market.py`
- Modify: `trading/scripts/quick_start_live.py`

**Rationale:** MarketService (169 lines) is pure forwarding. `quote()`, `ltp()`, `depth()`, `search()` delegate directly to the broker. `option_chain()`, `future_chain()`, `ltp_batch()`, `quote_batch()` add capability checks already present in BaseBroker. The one useful behavior — `history()` parameter normalization — moves to BaseBroker.

**Caller migration map:**

| Old (MarketService) | New (broker) |
|---------------------|-------------|
| `session.market.quote(inst)` | `session.broker.get_quote(inst)` |
| `session.market.ltp(inst)` | `session.broker.ltp(inst)` |
| `session.market.depth(inst)` | `session.broker.depth(inst)` |
| `session.market.history(...)` | `session.broker.history(...)` |
| `session.market.search(q)` | `session.broker.search(q)` |
| `session.market.option_chain(u, e)` | `session.broker.get_option_chain(u, e)` |
| `session.market.future_chain(u)` | `session.broker.future_chain(u)` |
| `session.market.ltp_batch(insts)` | `session.broker.ltp_batch(insts)` |
| `session.market.quote_batch(insts)` | `session.broker.quote_batch(insts)` |

- [ ] **Step 1: Move history() normalization to BaseBroker**

In `brokers/src/tradex_brokers/common/base.py`, replace the current `history()` method:

```python
# BEFORE (lines 287-294):
def history(
    self,
    instrument: Instrument,
    timeframe: object,
    start: datetime,
    end: datetime,
) -> HistoricalSeries:
    return self._require().history(instrument, timeframe, start, end)

# AFTER:
def history(
    self,
    instrument: Instrument,
    timeframe: object = None,
    start: datetime | None = None,
    end: datetime | None = None,
    *,
    interval: str | None = None,
    lookback_days: int | None = None,
) -> HistoricalSeries:
    """Get historical data.

    Canonical: ``history(inst, timeframe, start, end)``.
    Convenience: ``history(inst, interval="5m", lookback_days=5)``.
    """
    from tradex_domain.enums import Timeframe

    if interval is not None:
        timeframe = Timeframe(interval)
    if timeframe is None:
        raise ValueError("history requires a timeframe or interval")
    if not isinstance(timeframe, Timeframe):
        timeframe = Timeframe(timeframe)
    if start is None or end is None:
        if lookback_days is None:
            lookback_days = 30
        from datetime import UTC
        end = end or datetime.now(UTC)
        start = start or (end - timedelta(days=lookback_days))
    return self._require().history(instrument, timeframe, start, end)
```

Add required imports at top of base.py: `from datetime import UTC` (if not present).

- [ ] **Step 2: Run broker tests to verify history normalization**

```bash
cd brokers && python -m pytest -v -k "history"
```

- [ ] **Step 3: Remove `market` property from TradingSession**

In `trading/src/tradex_trading/sdk/session.py`:

1. Remove the import of `MarketService` (line 39)
2. Remove the `market` cached_property (lines 192-196)
3. Remove `"MarketService"` from `__all__` (line 551)

```python
# DELETE:
from tradex_trading.sdk.services import (
    ...
    MarketService,    # ← remove this import
    ...
)

# DELETE:
@cached_property
def market(self) -> MarketService:
    """MarketService — quotes, depth, history, batch, search, chains."""
    self._check_ready()
    return MarketService(self._broker, _broker_capabilities(self._broker))
```

- [ ] **Step 4: Update FastAPI app callers**

In `trading/src/tradex_trading/interface/fastapi_app.py`:

```python
# Line 365: s.market.quote(exchange=exchange, symbol=symbol)
# → resolve instrument first, then:
# → s.broker.get_quote(instrument)

# Line 376: s.market.search(q)
# → s.broker.search(q)

# Line 397: s.market.history(iid, tf)
# → resolve iid to Instrument, then: s.broker.history(instrument, tf)

# Line 433: s.market.option_chain(inst, expiry)
# → s.broker.get_option_chain(inst, expiry)

# Line 452: s.market.future_chain(inst)
# → s.broker.future_chain(inst)

# Line 859: session.market.search(stripped)
# → session.broker.search(stripped)

# Line 920: session.market.quote_batch(instruments)
# → session.broker.quote_batch(instruments)

# Line 936: session.market.ltp(inst)
# → session.broker.ltp(inst)
```

Note: For the `/quotes/{exchange}:{symbol}` endpoint (line 365), the current call `s.market.quote(exchange=exchange, symbol=symbol)` doesn't match `MarketService.quote(instrument)` anyway. Fix it to resolve the instrument from the registry first, then call `s.broker.get_quote(instrument)`.

- [ ] **Step 5: Update CLI callers**

In `trading/src/tradex_trading/interface/cli.py`:

```python
# Line 154: session.market.ltp(eq)
# → session.broker.ltp(eq)

# Line 400: session.market.quote(instrument)
# → session.broker.get_quote(instrument)
```

- [ ] **Step 6: Update re-exports**

In `trading/src/tradex_trading/sdk/services/__init__.py`:
- Remove `from tradex_trading.sdk.services.market import MarketService`
- Remove `"MarketService"` from `__all__`

In `trading/src/tradex_trading/sdk/__init__.py`:
- Remove `MarketService` from import and `__all__`

In `trading/src/tradex_trading/__init__.py`:
- Remove `MarketService` from import and `__all__`

- [ ] **Step 7: Delete MarketService file**

```bash
rm trading/src/tradex_trading/sdk/services/market.py
```

- [ ] **Step 8: Update quick_start_live.py script**

```python
# Line 168: print("session.market.history(...), ...")
# → print("session.broker.history(...), ...")
```

- [ ] **Step 9: Run trading tests (expect failures in market-related tests)**

```bash
cd trading && python -m pytest tests/ -v --tb=short 2>&1 | head -100
```

Expected: Tests that reference `session.market` will fail. These are fixed in Task 7.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "refactor: remove MarketService — callers use session.broker directly

- Moved history() parameter normalization to BaseBroker
- Updated FastAPI, CLI, and script callers
- Removed MarketService file and all re-exports"
```

---

## Task 4: Remove ExtensionService — Callers Use Broker Directly

**Files:**
- Modify: `trading/src/tradex_trading/sdk/session.py`
- Modify: `trading/src/tradex_trading/sdk/services/__init__.py`
- Modify: `trading/src/tradex_trading/sdk/__init__.py`
- Modify: `trading/src/tradex_trading/__init__.py`
- Delete: `trading/src/tradex_trading/sdk/services/extension.py`

**Rationale:** ExtensionService (210 lines) wraps every broker extension method with: order_gate + capability_check + delegate. BaseBroker already does both the gate and the capability check. The service adds zero behavior. Result type wrappers (KillSwitchResult, EdisStatus, OrderReceipt) wrap raw dicts that callers can use directly.

**Note:** No FastAPI or CLI code uses extension methods. Only tests reference ExtensionService.

- [ ] **Step 1: Remove `extension` property from TradingSession**

In `trading/src/tradex_trading/sdk/session.py`:

```python
# DELETE import:
from tradex_trading.sdk.services import (
    ...
    ExtensionService,   # ← remove
    EdisStatus,         # ← remove
    KillSwitchResult,   # ← remove
    TpinResult,         # ← remove
    OrderResult,        # ← remove (if only used by extension)
    ...
)

# DELETE property (lines 241-247):
@cached_property
def extension(self) -> ExtensionService:
    """ExtensionService — broker-specific extensions."""
    self._check_ready()
    caps = _broker_capabilities(self._broker)
    gate = self._make_order_gate()
    return ExtensionService(self._broker, caps, order_gate=gate)
```

Remove `ExtensionService`, `EdisStatus`, `KillSwitchResult`, `TpinResult` from `__all__`.

- [ ] **Step 2: Update re-exports**

In `trading/src/tradex_trading/sdk/services/__init__.py`:
- Remove all ExtensionService-related imports and __all__ entries

In `trading/src/tradex_trading/sdk/__init__.py`:
- Remove ExtensionService, EdisStatus, KillSwitchResult, TpinResult from imports and __all__

In `trading/src/tradex_trading/__init__.py`:
- Remove ExtensionService from imports and __all__

- [ ] **Step 3: Delete ExtensionService file**

```bash
rm trading/src/tradex_trading/sdk/services/extension.py
```

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: remove ExtensionService — callers use session.broker for extensions

BaseBroker already gates extension methods with capability checks and
order gates. ExtensionService was pure forwarding with duplicate gating."
```

---

## Task 5: Simplify TradingSession Construction

**Files:**
- Modify: `trading/src/tradex_trading/sdk/session.py`

**Rationale:** `TradingSession.paper()` (55 lines) and `TradingSession.live()` (100 lines) contain wiring logic that duplicates `runtime.startup.boot()`. The session class should own lifecycle + service access, not construction.

**Ponytail ultra assessment:** Extracting to a separate builder file adds a new file for marginal benefit. The lazy approach: inline the construction into `runtime.startup.boot()` (which already exists) and make `paper()`/`live()` thin wrappers that call `boot()`.

**However:** `paper()` is widely used in tests and scripts as a one-liner. Breaking that API is not lazy — it's churn. The lazy fix: keep `paper()` and `live()` but reduce them to calling `boot()` with the right config.

- [ ] **Step 1: Simplify `paper()` to delegate to `boot()`**

```python
@classmethod
def paper(
    cls,
    broker_id: str = "PAPER",
    *,
    bus: ReactiveBus | None = None,
    config: AppConfig | None = None,
) -> TradingSession:
    """Create a paper trading session."""
    from tradex_trading.runtime.startup import boot

    cfg = config or AppConfig()
    return boot(cfg, broker_id=broker_id, bus=bus)
```

**Note:** This requires `boot()` to support a `broker_id` override and paper mode. Check `runtime/startup.py` first. If `boot()` doesn't support this cleanly, keep the current `paper()` implementation but extract the common wiring into a private `_build_session()` classmethod shared with `live()`.

- [ ] **Step 2: If boot() doesn't support paper mode cleanly, extract shared wiring**

Alternative if Step 1 doesn't fit:

```python
@classmethod
def paper(cls, broker_id="PAPER", *, bus=None, config=None):
    cfg = config or AppConfig()
    # ... existing wiring, unchanged ...
    # ponytail: construction logic shared with live() — extract to
    # SessionBuilder if a third mode appears.

@classmethod  
def live(cls, broker_id, *, bus=None, confirm=False):
    if not confirm:
        raise ValueError("Live trading requires confirm=True")
    # ... existing wiring, unchanged ...
    # ponytail: same TODO as paper()
```

This is a `ponytail:` marker — the duplication is acknowledged and bounded.

- [ ] **Step 3: Run session tests**

```bash
cd trading && python -m pytest tests/sdk/ -v -k "session or paper or live"
```

- [ ] **Step 4: Commit**

```bash
git add trading/src/tradex_trading/sdk/session.py
git commit -m "refactor: simplify TradingSession construction (ponytail: extract if third mode appears)"
```

---

## Task 6: Update All Tests

**Files:**
- Modify: `trading/tests/sdk/test_session_services.py`
- Modify: `trading/tests/sdk/test_session_edges.py`
- Modify: `trading/tests/runtime/test_live_wiring.py`
- Modify: `trading/tests/contracts/test_session_smoke.py`
- Modify: `trading/tests/integration/test_full_stack.py`
- Modify: `trading/tests/interface/test_fastapi_app.py`
- Delete: `trading/tests/sdk/test_service_protocol_type_safety.py` (tests MarketService directly)

**Rationale:** Tests that mock `session.market.*` must now mock `session.broker.*`. Tests that directly test MarketService/ExtensionService are deleted — those classes no longer exist.

- [ ] **Step 1: Delete MarketService-specific tests**

Delete `trading/tests/sdk/test_service_protocol_type_safety.py` — it tests `MarketService` protocol safety which no longer exists.

- [ ] **Step 2: Update test_session_services.py**

Remove:
- `TestMarketService` class (all methods)
- `TestExtensionService` class (all methods)
- `TestSessionAccountAlias` class (already removed in Task 2)

Update:
- `TestPortfolioService` — change `session.market.*` references to `session.broker.*`
- `TestTradeService` — no changes needed (uses `session.trade.*`)
- `TestStreamService` — no changes needed (uses `session.stream.*`)

- [ ] **Step 3: Update test_session_edges.py**

Remove `TestExtensionServiceEdges` class.

- [ ] **Step 4: Update test_full_stack.py**

```python
# Line 102: session.market.quote(reliance) → session.broker.get_quote(reliance)
# Line 109: session.market.ltp(reliance) → session.broker.ltp(reliance)
# Line 122: session.market.history(...) → session.broker.history(...)
# Line 137: session.market.search("REL") → session.broker.search("REL")
```

- [ ] **Step 5: Update test_fastapi_app.py**

```python
# Lines 324-329: session.market.* mocks → session.broker.* mocks
# session.market.option_chain → session.broker.get_option_chain
# session.market.future_chain → session.broker.future_chain
# session.market.search → session.broker.search
# session.market.ltp → session.broker.ltp
# session.market.quote_batch → session.broker.quote_batch
```

- [ ] **Step 6: Update test_live_wiring.py**

```python
# Line 72: session.market.quote(...) → session.broker.get_quote(...)
```

- [ ] **Step 7: Update test_session_smoke.py**

```python
# Line 148: session.market.quote(...) → session.broker.get_quote(...)
# Line 158: session.market.ltp(...) → session.broker.ltp(...)
```

- [ ] **Step 8: Run full test suite**

```bash
cd trading && python -m pytest tests/ -v --tb=short
```

Expected: All pass. No references to `session.market` or `session.extension` remain.

- [ ] **Step 9: Run broker tests**

```bash
cd brokers && python -m pytest -v
```

Expected: All pass (Task 1 changes only).

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "test: update all tests for broker-direct access (no MarketService/ExtensionService)"
```

---

## Task 7: Final Verification + Cleanup

- [ ] **Step 1: Run the 8-point interface check**

```bash
cd brokers && python -m pytest tests/ -v -k "interface or parity or probe"
```

- [ ] **Step 2: Run full test suite across all packages**

```bash
python -m pytest domain/ brokers/ trading/ -v --tb=short
```

- [ ] **Step 3: Grep for any remaining references to removed abstractions**

```bash
grep -r "MarketService\|ExtensionService\|session\.market\|session\.extension\|session\.account\|_client" --include="*.py" .
```

Expected: No matches (except in git history and plan docs).

- [ ] **Step 4: Verify no import errors**

```bash
python -c "from tradex_trading import TradingSession; s = TradingSession.paper(); print(s.broker); print(s.trade); print(s.portfolio); print(s.stream); s.stop()"
```

Expected: Session creates, broker accessible, services work, session stops cleanly.

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "chore: architecture simplification complete

Removed: MarketService, ExtensionService, session.account alias, _client alias
Fixed: verify_connection() non-destructive, history() normalization in BaseBroker
Callers now use session.broker directly for market data and extensions.
TradeService, StreamService, PortfolioService retained (add real value)."
```

---

## What Was NOT Changed (and why)

| Finding | Decision | Rationale |
|---------|----------|-----------|
| PortfolioService.funds() `getattr` probe | KEEP | 5 lines, works correctly, fixing adds complexity |
| BaseBroker pass-through methods | KEEP | Protocol conformance requires explicit methods; __getattr__ breaks type checking |
| BrokerAdapter protocol split | SKIP | Structural typing works; YAGNI |
| Explicit lifecycle states | SKIP | Boolean works today; YAGNI |
| Concurrent lifecycle lock | SKIP | No evidence of concurrent connect() in production; YAGNI |
| Option chain fallback extraction | SKIP | Two copies of 15 lines; extracting saves nothing |
| Cross-process rate limit sharing | SKIP | No evidence of need |

## Summary of Deletions

| Removed | Lines deleted | Why |
|---------|-------------:|-----|
| MarketService | ~169 | Pure forwarding — broker has all methods |
| ExtensionService | ~210 | Duplicate gating — BaseBroker already gates |
| session.account alias | ~12 | Two names for one concept |
| _client alias | ~3 | Two names for one object |
| test_service_protocol_type_safety.py | ~131 | Tested removed MarketService |
| **Total** | **~525** | **Net deletion** |
