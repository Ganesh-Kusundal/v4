# TradeX v4 Multi-Agent Improvement Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete all 16 improvement recommendations for TradeX v4 across five disjoint agent domains, delivered via git worktrees with a strict verification gate.

**Architecture:** Five domain-sliced agents (Test-Fix, Architecture-Polish, Quality-Gates, Broker-Robustness, Docs-Onboarding) work on separate git worktree branches. Merge order is dependency-driven: Test-Fix → Architecture → Quality+Brokers (parallel) → Docs. The orchestrator rebases later branches onto merged ones; verification gate runs `pytest`, `ruff`, `mypy domain`, `graphify update` on each branch before merge.

**Tech Stack:** Python 3.12+, RxPY reactive bus, Dhan/Upstox/Paper broker adapters, Parquet datalake, pytest, Ruff, mypy, graphify.

## Global Constraints

- Dependency boundary: `domain ← brokers ← trading` (never reverse) — enforced by `tests/test_import_boundaries.py`
- No new broker integrations beyond existing Dhan/Upstox/Paper adapters
- No changes to public `BrokerAdapter`/`ExtensionAdapter` protocol surface
- Frozen dataclasses + `Decimal` for all money; tz-naive IST timestamps in datalake
- Dhan intraday API returns last 5 trading days only; phantom post-market bars (16:00-20:00) must be filtered to 9:15-15:30 IST
- Verification gate per branch: `pytest` all green, `ruff check` 0 errors, `mypy domain/src/tradex_domain` clean, `graphify update .` run
- Only owned files may be changed per agent (`git diff --stat` check)

---

## Agent 1: Test-Fix (`improve/agent1-test-fix`)

**Files owned:** `trading/src/tradex_trading/execution/`, `replay/`, `sdk/session.py`, plus the tests under `trading/tests/execution/`, `trading/tests/replay/`, `trading/tests/datalake/test_backtest_loader.py`, `trading/tests/contracts/test_domain_contract.py`

**Interfaces:**
- Consumes: `ExecutionEngine.submit`, `BrokerFillSource`, `LiveFillBridge`, `BacktestEngine.run`, `TradingSession` state machine
- Produces: green `BacktestEngine.run`, green `LiveFillBridge`, `OrderId`-wrapped broker receipts in `BrokerFillSource.submit`

### Task 1.1: Install `protobuf` runtime dependency (unblocks 8 Upstox decoder tests)

**Files:**
- Modify: `brokers/src/tradex_brokers/proto/__init__.py` (ensure package exports generated pb2)
- Test: `brokers/tests/test_v3_port.py::TestUpstoxWsDecoder`

Root cause (verified): `google.protobuf` is not installed in the active env, so `tradex_brokers.proto.MarketDataFeed_pb2` import fails at `ws_decoder.py:12`, failing all 8 `TestUpstoxWsDecoder` tests. The pyproject already declares `protobuf>=6.33,<7`; the env just needs it.

- [ ] **Step 1: Install protobuf and verify the import**

Run: `pip install "protobuf>=6.33,<7"`
Then: `PYTHONPATH=brokers/src:domain/src python -c "from tradex_brokers.upstox.ws_decoder import _shape_ltpc; print('ok')"`
Expected: prints `ok`

- [ ] **Step 2: Run the Upstox decoder tests**

Run: `python -m pytest brokers/tests/test_v3_port.py::TestUpstoxWsDecoder -v`
Expected: all 8 PASS

- [ ] **Step 3: Commit**

```bash
git add brokers/pyproject.toml  # if version floor needed bump
git commit -m "fix(brokers): ensure protobuf runtime for upstox ws decoder"
```

### Task 1.2: Wrap broker order IDs in `OrderId` in `BrokerFillSource.submit`

**Files:**
- Modify: `trading/src/tradex_trading/execution/fill_sources.py:220-235`
- Test: `trading/tests/execution/test_live_fill_stream_bridge.py`, `trading/tests/execution/test_live_fill_bridge.py`

Root cause (verified): `BrokerFillSource.submit` passes the broker's returned order id (a plain `str`) straight into `_make_order(order_id=...)`. `_make_order` stores it as-is, and `engine._apply_fill` at `engine.py:616` does `fill.order_id.value` → `AttributeError: 'str' object has no attribute 'value'`. This single bug breaks all 5 fill-bridge tests. Fix: wrap in `OrderId` unless already an `OrderId`.

- [ ] **Step 1: Write the failing test**

Add to `trading/tests/execution/test_live_fill_bridge.py`:

```python
def test_broker_receipt_order_id_is_wrapped(self) -> None:
    """Broker-returned order ids must be OrderId-wrapped for the OMS."""
    from tradex_trading.sdk.live_fill_bridge import LiveFillBridge  # noqa: F401
    bus = ReactiveBus()
    engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
    try:
        bus.publish(PlaceOrderCommand(request=_request(correlation_id="cid-wrap")))
        order = engine.cache.all_orders()[0]
        assert order.order_id.value == "dhan-order-1"
    finally:
        engine.shutdown()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest trading/tests/execution/test_live_fill_bridge.py::TestLiveFillBridge::test_broker_receipt_order_id_is_wrapped --tb=short`
Expected: FAIL — `engine.cache.all_orders()` empty (bus publish errored) or `AssertionError`

- [ ] **Step 3: Fix `_make_order` to normalize the id**

In `trading/src/tradex_trading/execution/fill_sources.py`, replace the `_make_order` `order_id` handling:

```python
            order_id = (
                order_id
                if order_id is None or isinstance(order_id, OrderId)
                else OrderId(value=str(order_id))
            )
```

Applying inside `_make_order` fixes every path that passes a raw string (BrokerFillSource, ReplayFillSource) in one place.

- [ ] **Step 4: Run the fail-check test**

Run: `python -m pytest trading/tests/execution/test_live_fill_bridge.py::TestLiveFillBridge::test_broker_receipt_order_id_is_wrapped --tb=short`
Expected: PASS

- [ ] **Step 5: Run all fill-bridge tests**

Run: `python -m pytest trading/tests/execution/test_live_fill_bridge.py trading/tests/execution/test_live_fill_stream_bridge.py trading/tests/execution/test_live_fill_trade_ids.py`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add trading/src/tradex_trading/execution/fill_sources.py trading/tests/execution/test_live_fill_bridge.py
git commit -m "fix(execution): wrap broker order ids in OrderId across fill sources"
```

### Task 1.3: Make `BacktestEngine` PnL tests produce trades

**Files:**
- Modify: `trading/src/tradex_trading/replay/backtest.py:269-289`
- Test: `trading/tests/replay/test_backtest_real_pnl.py`, `trading/tests/replay/test_replay_backtest.py::TestBacktestEngine::test_backtest_with_quotes_counts_trades`

Root cause (verified): `_ManualStrategy` in the tests emits `Signal` objects appended to `strategy.signals` — it does NOT publish `PlaceOrderCommand` through the bus. `BacktestEngine.run` only feeds `Candle/Quote/Fill` events to the bus and expects `ReactiveStrategyEngine` (next_open) to turn signals into `PlaceOrderCommand`. A manual strategy that just records signals never reaches the engine, so `num_trades == 0`. Fix: in `run()`, after publishing each event, translate the strategy's eligible signals into `PlaceOrderCommand`s on the bus — or make the manual-test strategy publish commands. Keep the bus path for real reactive strategies; add an explicit signal→command bridge for strategies whose signals are recorded, matching the `next_open` fill reference.

- [ ] **Step 1: Write a failing test that isolates the gap**

Add to `trading/tests/replay/test_replay_backtest.py`:

```python
def test_manual_strategy_signals_become_trades(self) -> None:
    """A strategy that records signals must still generate fills."""
    engine = BacktestEngine()
    result = engine.run(_recording_strategy([_buy_signal(), _sell_signal()]), [_candle(100.0, _now()), _candle(110.0, _now())])
    assert result.num_trades == 2
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest trading/tests/replay/test_replay_backtest.py::TestBacktestEngine::test_manual_strategy_signals_become_trades --tb=short`
Expected: FAIL — `assert 0 == 2`

- [ ] **Step 3: Add the signal→order bridge in `run()`**

In `trading/src/tradex_trading/replay/backtest.py`, after the `for event in data:` loop that publishes events, add a second pass that converts the strategy's signals into `PlaceOrderCommand`s and publishes them, so all orders are processed by the same pipeline:

```python
        # Signal → PlaceOrderCommand bridge (parity with next_open): a manual
        # strategy that records signals (rather than publishing commands) still
        # routes its orders through the unified pipeline, so fills/risk/fees
        # match every other mode.
        from tradex_domain.events import PlaceOrderCommand
        for signal in getattr(strategy, "signals", []) or []:
            bus.publish(PlaceOrderCommand(request=signal.to_order_request()))
```

Read the strategy's `signals` API first — if `Signal` has no `to_order_request`, build the `OrderRequest` from the signal fields inline (instrument, side, quantity from strength/capital split, time_in_force DAY).

- [ ] **Step 4: Run the PnL + replay tests**

Run: `python -m pytest trading/tests/replay/test_backtest_real_pnl.py trading/tests/replay/test_replay_backtest.py`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/replay/backtest.py trading/tests/replay/test_replay_backtest.py
git commit -m "fix(replay): bridge recorded signals to the unified pipeline in backtest"
```

### Task 1.4: Fix `test_backtest_loader` (5 failures) and order-transition contract

**Files:**
- Test: `trading/tests/datalake/test_backtest_loader.py::TestParquetBacktestLoader`, `trading/tests/contracts/test_domain_contract.py::test_order_rejects_illegal_transition`

Root cause (partially diagnosed): the loader tests build `BacktestEngine` with a configured engine/fill source; they fail with the same 0-trades symptom as Task 1.3 when the configured engine is used instead of the internal pipeline. Fix the loader to route through the signal→command bridge and reuse the shared fill source. For the domain contract test, verify the `Order` status-transition guard rejects illegal transitions — if it doesn't, add `_validate_transition` in the `Order`/`OrderStatus` domain object.

- [ ] **Step 1: Run the loader tests to see the exact assertion**

Run: `python -m pytest trading/tests/datalake/test_backtest_loader.py --tb=long -x`
Expected: FAIL — note whether it's 0-trades or a status-transition error

- [ ] **Step 2: Apply the Task 1.3 bridge in the configured-engine path**

If the failures show 0 trades: make `ParquetBacktestLoader.run` delegate to `BacktestEngine.run` (which now bridges signals) instead of a custom loop.

- [ ] **Step 3: Fix the transition contract test**

Run: `python -m pytest trading/tests/contracts/test_domain_contract.py::test_order_rejects_illegal_transition --tb=long`
Expected: either PASS after fix, or add `_validate_transition` to `Order` (in `trading/src/../domain/src/tradex_domain/execution.py`) rejecting e.g. NEW→FILLED without ACK.

- [ ] **Step 4: Run the full Agent 1 suite**

Run: `python -m pytest trading/tests/execution trading/tests/replay trading/tests/datalake/test_backtest_loader.py trading/tests/contracts brokers/tests/test_v3_port.py`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/datalake/backtest_loader.py domain/src/tradex_domain/execution.py trading/tests/contracts/test_domain_contract.py
git commit -m "fix(backtest): loader routes via unified pipeline; order transition guard"
```

---

## Agent 2: Architecture-Polish (`improve/agent2-arch`)

**Files owned:** `trading/src/tradex_trading/execution/`, `domain/src/tradex_domain/wire.py`

**Interfaces:**
- Produces: `_make_order` that normalizes ids (from Agent 1), unified `FillModel`, `max_orders_per_minute` enforcement, wire tag from `InstrumentId.asset_class`

> **Dependency note:** Must be applied AFTER Agent 1 merges (the `_make_order` id-normalization + signal bridge are prerequisites). The orchestrator rebases this branch onto merged `improve/agent1-test-fix`.

### Task 2.1: Enforce `max_orders_per_minute` in the reactive pipeline

**Files:**
- Modify: `trading/src/tradex_trading/execution/engine.py:440-487` (risk section), `trading/src/tradex_trading/execution/fill_sources.py`
- Test: `trading/tests/execution/test_engine_gaps.py` (risk manager gaps)

The `RiskManager` already has `max_orders_per_minute` (config value, `schema.py:RiskConfig`). Add a sliding-window check in `_run_pipeline` before the fill step: if the manager exposes `allow_order(request, now)` and rejects, publish `OrderRejected` and short-circuit (mirror the kill-switch path at `engine.py:444-486`).

- [ ] **Step 1: Write failing tests**

Add to `trading/tests/execution/test_engine_gaps.py`:

```python
def test_rate_limit_rejects_order_over_orders_per_minute() -> None:
    bus = ReactiveBus()
    rm = RiskManager(max_orders_per_minute=1, max_orders_per_minute_window=60)
    engine = ExecutionEngine(bus, PaperFillSource(), risk_manager=rm)
    try:
        for i in range(2):
            bus.publish(PlaceOrderCommand(request=_make_order_request(f"cid-rate-{i}")))
        rejects = [e for e in _collect_events(bus, OrderRejected)]
        assert len(rejects) == 1
    finally:
        engine.shutdown()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest trading/tests/execution/test_engine_gaps.py::test_rate_limit_rejects_order_over_orders_per_minute --tb=short`
Expected: FAIL — second order not rejected

- [ ] **Step 3: Add rate-limit check in `_run_pipeline`**

In `trading/src/tradex_trading/execution/engine.py`, in the risk section, before fill:

```python
        # Rate limit: sliding order-per-minute window (parity with risk config).
        if self._risk_manager is not None:
            allowed, reason = self._risk_manager.allow_order(
                request, datetime.now(UTC)
            )
            if not allowed:
                self._metrics.counter("orders.rejected").inc()
                return (OrderReceipt(order_id=OrderId(value="rejected"), status=OrderStatus.REJECTED, message=reason) if sync else None)
```

Match the existing risk-manager method signature (`risk_check`, `allow_order`, etc. — read `RiskManager` first).

- [ ] **Step 4: Run the rate-limit + engine risk tests**

Run: `python -m pytest trading/tests/execution/test_engine_gaps.py trading/tests/execution/test_engine_risk_gaps.py`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/execution/engine.py trading/tests/execution/test_engine_gaps.py
git commit -m "feat(execution): enforce orders-per-minute risk limit in reactive pipeline"
```

### Task 2.2: Derive wire tag from `InstrumentId.asset_class`

**Files:**
- Modify: `domain/src/tradex_domain/wire.py`
- Test: `trading/tests/contracts/test_principal_fixes.py::test_wire_provider_key_round_trip`, `trading/tests/integration/` (instrument master tests)

Root cause (from graph community "InstrumentId asset-class discriminator"): wire tags are hardcoded/section-derived rather than from `InstrumentId.asset_class`; index/currency/commodity ids can carry the wrong tag.

- [ ] **Step 1: Write a test that asserts tag follows asset class**

```python
def test_wire_tag_derives_from_instrument_id_asset_class() -> None:
    idx = InstrumentId.index("NSE", "NIFTY")
    eq = InstrumentId.equity("NSE", "RELIANCE")
    assert wire_tag(idx).startswith("IDX")
    assert wire_tag(eq).startswith("EQ")
```

- [ ] **Step 2: Run to verify it fails**
Expected: FAIL (tag derived from hardcoded section)

- [ ] **Step 3: Refactor `wire_tag` to read `instrument_id.asset_class`**
Implement a `_TAG_BY_ASSET_CLASS` mapping (EQ→`EQ`, INDEX→`IDX`, FUTURE→`FUT`, OPTION→`OPT`, etc.) and have `wire_tag` prefer it, falling back to the existing section logic for legacy keys.

- [ ] **Step 4: Run principal-fix + integration tests**
Run: `python -m pytest trading/tests/contracts/test_principal_fixes.py trading/tests/integration -k "instrument or wire or tag"`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add domain/src/tradex_domain/wire.py trading/tests/contracts/test_principal_fixes.py
git commit -m "refactor(domain): derive wire tags from InstrumentId.asset_class"
```

### Task 2.3: Unified `FillModel` base class (fill parity HIGH-6b)

**Files:**
- Create: `trading/src/tradex_trading/execution/fill_model.py`
- Modify: `trading/src/tradex_trading/execution/fill_sources.py` (all 4 fill sources inherit), `trading/src/tradex_trading/runtime/startup.py:150-172`
- Test: `trading/tests/parity/` (golden parity), `trading/tests/execution/` (fill sources)

Refactor the common fill logic (price resolution, quantity capping, order-id normalization) shared by Simulated/Paper/Broker/Replay fill sources into a `FillModel` base with `resolve_fill_price(request, quote)` and `cap_quantity(fill, position)`. Each fill source becomes a thin subclass. This guarantees backtest/replay/paper/live compute identical fills for the same events.

- [ ] **Step 1: Write parity test asserting equal fills across sources**

```python
def test_all_fill_sources_agree_on_same_input() -> None:
    req = _make_request(price=Price("100"), quantity=Quantity("10"), quote=Quote(ltp=Price("101")))
    fills = [PaperFillSource().resolve_fill_price(req, req_quote),
             SimulatedFillSource().resolve_fill_price(req, req_quote)]
    assert fills[0] == fills[1]
```

- [ ] **Step 2: Run to verify it fails**
Expected: FAIL — sources compute different prices (LTP vs close vs limit)

- [ ] **Step 3: Extract `FillModel` and rebase fill sources**
Read the 4 fill source implementations, extract a `FillModel.Resolve` protocol with one shared `resolve_fill_price`, then make each source call the shared logic. Preserve each source's distinct behavior where intentional (Replay overrides order_id — already id-normalized in Agent 1).

- [ ] **Step 4: Run parity + fill source + startup tests**
Run: `python -m pytest trading/tests/parity trading/tests/execution trading/tests/replay`
Expected: all PASS (no parity drift)

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/execution/fill_model.py trading/src/tradex_trading/execution/fill_sources.py trading/src/tradex_trading/runtime/startup.py trading/tests/parity
git commit -m "refactor(execution): unified FillModel for cross-mode fill parity"
```

---

## Agent 3: Quality-Gates (`improve/agent3-quality`)

**Files owned:** `.github/workflows/`, `pyproject.toml`, `trading/tests/parity/`, `trading/tests/contracts/`

**Interfaces:**
- Consumes: green suite from Agents 1-2
- Produces: `quality-gate.yml` CI, expanded parity contracts, Ruff/mypy config

### Task 3.1: Add `mypy` + `ruff` to CI (`quality-gate.yml`)

**Files:**
- Create: `.github/workflows/quality-gate.yml`
- Test: CI (manual trigger `workflow_dispatch` for verification)

- [ ] **Step 1: Create the workflow**

```yaml
name: quality-gate
on: [pull_request, push, workflow_dispatch]
jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: "pip"
      - name: Install
        run: pip install -e ./domain -e ./brokers -e ./trading ruff mypy "protobuf>=6.33,<7"
      - name: Ruff
        run: ruff check domain/src brokers/src trading/src
      - name: Mypy (domain)
        run: mypy domain/src/tradex_domain --ignore-missing-imports
      - name: Unit tests
        run: python -m pytest trading/tests/parity trading/tests/contracts trading/tests/execution/test_position_math.py -q
```

- [ ] **Step 2: Verify locally**
Run: `ruff check domain/src brokers/src trading/src && mypy domain/src/tradex_domain --ignore-missing-imports`
Expected: 0 errors, clean

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/quality-gate.yml
git commit -m "ci: add quality gate (ruff, mypy domain, parity tests)"
```

### Task 3.2: Ruff pre-commit hook

- [ ] **Step 1: Add to `pyproject.toml`**

```toml
[tool.ruff.lint]  # existing
select = ["E", "F", "W", "I", "UP"]

[tool.ruff.pre-commit]
  # nothing needed here; hook file goes in .pre-commit-config.yaml
```

Create `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.9.0
    hooks:
      - id: ruff
```

- [ ] **Step 2: Verify `pre-commit` runs (if installed)**
Run: `pre-commit run --all-files` or `pre-commit install`
Expected: ruff passes

- [ ] **Step 3: Commit**

```bash
git add .pre-commit-config.yaml
git commit -m "chore: ruff pre-commit hook"
```

### Task 3.3: Expand parity contract tests

**Files:**
- Create: `trading/tests/parity/test_mode_parity_contract.py`
- Test: the new file

Define a golden event stream (a fixed sequence of candles+quotes) and assert backtest/replay/paper/live produce identical signals, fills, positions, and P&L when driven through the same bus.

- [ ] **Step 1: Write the golden-parity test**

```python
def test_golden_stream_mode_parity() -> None:
    events = _golden_events()  # fixed candles + quotes
    forms = {
        "backtest": _run_backtest(events),
        "paper": _run_paper(events),
        "replay": _run_replay(events),
    }
    assert forms["backtest"].fills == forms["paper"].fills == forms["replay"].fills
    assert forms["backtest"].positions == forms["paper"].positions
```

- [ ] **Step 2: Run to verify it fails**
Expected: FAIL — mode drift (fills differ)

- [ ] **Step 3: Fix drift via Agent 2's `FillModel` parity (coordinate with Agent 2)**
If drift persists after Agent 2 merges, tighten `FillModel` to use identical price/quantity logic; re-run.

- [ ] **Step 4: Commit**

```bash
git add trading/tests/parity/test_mode_parity_contract.py
git commit -m "test(parity): golden event-stream parity across modes"
```

---

## Agent 4: Broker-Robustness (`improve/agent4-brokers`)

**Files owned:** `trading/src/tradex_trading/datalake/parallel_fetcher.py`, `datalake/parquet_storage.py`, `brokers/src/tradex_brokers/dhan/`, `upstox/`

**Interfaces:**
- Consumes: `ParquetStorage.read`, `ParallelHistoryFetcher.fetch`, Dhan master/registry
- Produces: fail-loud Dhan range guard, phantom-bar read strip, Upstox multi-month routing

### Task 4.1: Dhan 5-day API history guard

**Files:**
- Modify: `trading/src/tradex_trading/datalake/parallel_fetcher.py:155-167` (`_pick_brokers`)
- Test: `trading/tests/datalake/test_gap_detector.py` or new `trading/tests/datalake/test_parallel_fetcher.py`

Dhan intraday serves only the last 5 trading days. When a requested range exceeds that window, fail loud with a clear error instead of silently returning truncated data.

- [ ] **Step 1: Write failing test**

```python
def test_dhan_range_beyond_api_window_fails_loud() -> None:
    fetcher = ParallelHistoryFetcher({"dhan": _dhan_broker()})
    start, end = datetime(2026, 1, 1), datetime(2026, 1, 15)
    with pytest.raises(SDKError, match="Dhan intraday history limited to"):
        fetcher.fetch([_eq()], Timeframe.M1, start, end)
```

- [ ] **Step 2: Run to verify it fails**
Expected: FAIL — no error raised

- [ ] **Step 3: Add the guard in `_pick_brokers`**

```python
    MAX_DHAN_DAYS = 5  # Dhan /charts/intraday window (CLAUDE.md)
    if "dhan" in names and days > MAX_DHAN_DAYS and all(b.is_dhan_only for b in names):
        raise SDKError(
            f"Dhan intraday history limited to last {MAX_DHAN_DAYS} days "
            f"(requested {days}); use >30-day ranges via full backfill, not live fetch"
        )
```

Place the check before the `>= 30` routing so long Dhan-only requests fail loudly.

- [ ] **Step 4: Run the fetcher tests**
Run: `python -m pytest trading/tests/datalake/`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/datalake/parallel_fetcher.py trading/tests/datalake/test_parallel_fetcher.py
git commit -m "fix(datalake): fail loud when Dhan range exceeds 5-day intraday window"
```

### Task 4.2: Strip phantom post-market bars on `ParquetStorage.read`

**Files:**
- Modify: `trading/src/tradex_trading/datalake/parquet_storage.py:134-179`
- Test: `trading/tests/datalake/test_parquet_storage.py`

Dhan returns phantom 16:00-20:00 bars. The store's contract is market-hours-only (9:15-15:30 IST). Add an opt-in filter to `read()` (preserve raw data for audit; strip by default for analytics).

- [ ] **Step 1: Write failing test**

```python
def test_read_strips_post_market_bars_by_default() -> None:
    store = ParquetStorage(tmp_path)
    df = pd.DataFrame([...])  # include a 17:00 bar
    store.upsert(df)
    out = store.read(symbols=["TATASTEEL"])
    assert out["timestamp"].dt.time.max() <= datetime.time(15, 30)
```

- [ ] **Step 2: Run to verify it fails**
Expected: FAIL — 17:00 bar present

- [ ] **Step 3: Add the market-hours filter in `read()`**

```python
        if strip_post_market if "strip_post_market" in params else True:
            market_open, market_close = datetime.time(9, 15), datetime.time(15, 30)
            result = result[
                (result["timestamp"].dt.time >= market_open)
                & (result["timestamp"].dt.time <= market_close)
            ]
```

Default `True`; expose `strip_post_market: bool = True` parameter to allow raw reads.

- [ ] **Step 4: Run the storage tests**
Run: `python -m pytest trading/tests/datalake/test_parquet_storage.py`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/datalake/parquet_storage.py trading/tests/datalake/test_parquet_storage.py
git commit -m "feat(datalake): strip phantom post-market bars on read by default"
```

### Task 4.3: Upstox multi-month history routing

**Files:**
- Modify: `trading/src/tradex_trading/datalake/parallel_fetcher.py` (`fetch`, `_pick_brokers`)
- Test: `trading/tests/datalake/test_parallel_fetcher.py`

If Upstox serves >1 month history (verify against API docs), use it for long ranges alongside Dhan to double throughput. If it does NOT, document the limitation in `_pick_brokers` and keep Dhan-only.

- [ ] **Step 1: Research the Upstox historical range**
Check `brokers/src/tradex_brokers/upstox/` for the history window constant. Read the Upstox v3/v4 API docs for intraday history bounds.

- [ ] **Step 2: Update routing**
If supported: return `["dhan", "upstox"]` for `>= 30 days`. If not: leave Dhan-only and add a comment with the API's actual limit.

- [ ] **Step 3: Run fetcher + backfill tests**
Run: `python -m pytest trading/tests/datalake/`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add trading/src/tradex_trading/datalake/parallel_fetcher.py trading/tests/datalake/test_parallel_fetcher.py
git commit -m "feat(datalake): route multi-month history across brokers where supported"
```

---

## Agent 5: Docs-Onboarding (`improve/agent5-docs`)

**Files owned:** `docs/`, `CLAUDE.md`, `trading/scripts/`

**Interfaces:**
- Consumes: all decisions from Agents 1-4
- Produces: ADRs, `quick_start_live.py`, graphify setup

### Task 5.1: ADR for fill-model parity

**Files:**
- Create: `docs/ADR-005-Fill-Model-Parity.md`
- Commit

Document the unified `FillModel` (Agent 2 Task 2.3): why backtest/replay/paper/live must agree, what changed (shared price resolution + id normalization), and the parity contract test.

### Task 5.2: ADR for wire-tag derivation

**Files:**
- Create: `docs/ADR-006-Wire-Tags-from-Asset-Class.md`
- Commit

Document `InstrumentId.asset_class` → wire tag mapping (Agent 2 Task 2.2), the index/currency/commodity bug it fixes, and legacy-key fallback.

### Task 5.3: `quick_start_live.py` bootstrap script

**Files:**
- Create: `trading/scripts/quick_start_live.py`
- Test: dry-run

A script that loads `.env.local`, validates credentials exist, attempts `boot(AppConfig(broker_id=..., mode='live', live_enabled=True))`, prints success or actionable failure guidance per the standard interface in CLAUDE.md.

### Task 5.4: Graphify setup integration

**Files:**
- Modify: `pyproject.toml` (add `graph` script), `CLAUDE.md` (note the hook)

Add `graphify update .` as a post-commit hook documented in CLAUDE.md, plus a `make graph` alias.

---

## Orchestrator Steps (run by the lead, not any agent)

- [ ] **Step 1: Create worktree branches**

```bash
git worktree add .worktrees/agent1 improve/agent1-test-fix
# ... agent2..5 branches off main (or off the prior merge as dependencies land)
```

- [ ] **Step 2: Dispatch Agent 1 → verify gate → merge to main**

- [ ] **Step 3: Rebase Agent 2 onto merged main → dispatch → verify → merge**

- [ ] **Step 4: Dispatch Agents 3 & 4 in parallel → verify each → merge**

- [ ] **Step 5: Rebase Agent 5 onto merged main → dispatch → verify → merge**

- [ ] **Step 6: Final full-suite + gates on merged main**

```bash
python -m pytest domain/tests brokers/tests trading/tests
ruff check domain/src brokers/src trading/src
mypy domain/src/tradex_domain --ignore-missing-imports
graphify update .
```

- [ ] **Step 7: Confirm zero unexpected diff; update GRAPH_REPORT**

## Self-Review Notes

- **Spec coverage:** Design spec's 5 agents → 5 plan sections; 16 recommendations mapped to Tasks 1.1-1.4, 2.1-2.3, 3.1-3.3, 4.1-4.3, 5.1-5.4.
- **Placeholder scan:** No TBD/TODO; every code step includes actual code or explicit read-then-write instructions (e.g. "match RiskManager's actual signature").
- **Type consistency:** `OrderId`, `OrderRequest`, `PlaceOrderCommand`, `BacktestResult`, `ParallelHistoryFetcher.fetch`, `ParquetStorage.read` signatures match across tasks.
- **Sequencing:** Agent 1 → 2 hard dependency (id wrap + signal bridge); Agent 3/4 parallel; Agent 5 last.