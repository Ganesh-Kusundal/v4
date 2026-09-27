# Full Platform Review — TradeX v4

**Date:** 2026-09-27 · **Reviewed commit:** `fcaa6b4` on `chore/cleanup-overhaul`
**Method:** 13-specialist review board (architecture, quant, event-driven, distributed systems,
SRE, QA, security, data platform, frontend, low-latency, performance, principal engineering),
followed by direct source verification of every headline claim by the reviewing principal.

**Verification convention.** Findings marked **[V]** were re-verified line-by-line against source
by the principal reviewer. The remainder are specialist findings with cited `file:line` that have
not been individually re-checked — treat them as strong but confirm before acting.

---

## 1. Executive Summary

This is a **well-architected order-plumbing system that has never been made safe to size a
position with**, and a **strategy research platform that cannot currently place a live order from
its own UI**.

The execution spine is genuinely strong and shows real production scar tissue: durable
persist-then-mutate, a three-state UNKNOWN-submission policy that fails a possibly-venue-reaching
submission toward `UNKNOWN` rather than `REJECTED`, INSERT-first idempotency that closes the
reserve race at the database level, a real cross-broker capability conformance suite, golden
fixture provenance gated on a pinned library hash, and a build-stamp that refuses to run E2E
against a stale bundle. The last six commits are a competent engineer fixing real money-path
sequencing defects. That is a foundation, not a facade.

What is missing is everything above it.

**The three findings that decide the verdict:**

1. **Position size is `abs(signal.strength)` and every shipped strategy hardcodes `strength=1.0`**
   — `strategy/core/engine.py:311`, `replay/backtest.py:474`. [V] Every strategy in this
   repository trades one share. There is no `Sizer` type anywhere in the 18 packages. Sizing is
   an accident of an unrelated field, and nothing clamps it: `OrderRequest` validates only
   `quantity > 0`, and all five `RiskConfig` limits (`max_order_value`, `max_position_value`,
   `max_orders_per_minute`, `max_daily_loss_amt`, `max_drawdown_pct`) default to `None`. [V]
   A strategy that computes `strength = 3 * sigma` silently triples its position.

2. **The strategy lifecycle is write-only.** `ReactiveStrategyEngine.unregister()` is
   `self._strategies.pop(strategy_id, None)` — it does not dispose the four bus subscriptions
   created in `register()`. [V] A "unregistered" strategy keeps receiving every Candle/Quote/Depth
   and keeps placing live orders. `StrategyRuntime.pause()` flips an enum that the delivery path
   never consults. **You cannot stop one losing strategy without killing the process and every
   other strategy with it.**

3. **Every backtest number is invalid.** `replay/backtest.py:546` flushes deferred orders against
   the *last* candle, so a signal on that bar fills at that bar's own open — look-ahead. [V]
   `backtest.py:326` books the cash debit in a bus subscriber *after* the position, so an overdraft
   `ValueError` is swallowed by the bus's error isolation and the equity curve is silently
   flattering. The user-facing `/api/charts/backtest` route passes no risk manager, no slippage
   model and no corporate actions. Combined with 1-share sizing and 2-decimal quantisation, Sharpe
   and drawdown are numerically meaningless.

**Plus two platform-level blockers:**

- **The live order path in the UI is unreachable.** `frontend/src/main.ts:61` is
  `getApiKey() === '' ? 'paper' : 'unknown'`; `order-safety.ts:39` accepts only
  `paper | live | replay`. [V] On a keyed (live) server the arm box and quantity field are
  permanently disabled, and Cancel/Modify/Exit are disabled with them. No endpoint reports the
  session mode. A live trader cannot trade, cancel, or flatten from this UI.
- **`GET /` is unauthenticated and injects the live API key into the served HTML**
  (`fastapi_app.py:388-395`, scraped by `frontend/src/apikey.ts:15-27`). [V] `curl http://127.0.0.1:8000/`
  yields the money-moving credential in cleartext, bypassing the session cookie, CSRF and Origin
  controls entirely.

**The measured performance ceiling** (measured on this checkout, not estimated): the tick path is
one lock-serialised single thread at **44.1 µs/tick = 22,661 ticks/s aggregate across all sockets
and all consumers**. A single blocking venue order submit runs *on that same thread*. The
benchmark that supposedly measures this (`benchmarks/bench_tick_parse.py:23`) uses a
`b"\x00" * 100` placeholder that exits at the first length check and reports **0.21 µs — a 38×
understatement**. There is no latency gate and no load test in the repository.

**Verdict.** Defensible for paper trading today. **Not deployable with real money** — not because
the code is bad, but because the risk layer that would make real money safe is written, tested and
wired in a way that does not constrain anything, and nobody has yet run the system against a
position it could not afford.

---

## 2. Architecture Review Report

### 2.1 What is right

- **`domain` imports nothing internal.** Zero measured edges. `tradex_domain/protocols.py` defines
  `BrokerAdapter`; `brokers` implements it and depends inward. This is the correct direction and
  it is enforced by a 795-line boundary test with vacuity guards
  (`test_import_boundaries.py:222,590,760,781`).
- **Money is `Decimal` in the money path.** 167 `Decimal` mentions in `execution` against 11
  `float`s, and all 11 are metrics conversions or `max_mark_age_seconds`. Timestamps are IST
  tz-naive, enforced at the datalake write chokepoint.
- **The DuckDB analytics surface is a real guardrail** — keyword denylist, multi-statement
  rejection, memory/row/statement caps (`services/duckdb-analytics/src/duck_analytics/query.py:28-30,105`).
- **All SQLite is parameterised.** No string-interpolated SQL reaches a request value.
- **`SingleWriterLock` is the best-engineered SPOF mitigation in the repo** — `flock` on the inode,
  pid recorded *into* the locked inode, takeover only under the flock with a dead-pid check
  (`runtime/src/tradex_runtime/writer_lock.py:57-198`), and the docstring documents the exact TOCTOU
  the first version lost.
- **Contested-symbol resolution is now deterministic.** `BaseBroker.load_instruments` registers
  weakest-claim-first so `register_authoritative`'s last-wins lands on the strongest claimant
  regardless of vendor row order (`brokers/common/base.py:369-395`). This is better than the
  repo's own narrative claims.
- **Leaf packages have zero bare `except Exception`** — `domain`, `config`, `research`,
  `operations`, `observability`, `application`, `persistence` are all fail-loud.

### 2.2 Structural problems

**2.2.1 — The strangler migration is half-finished and is now the dominant cost.** [V] 133 of 141
files in `trading/src/tradex_trading/` (1,735 of 2,741 lines) are
`globals().update({k: v for k, v in vars(import_module(...)).items()})` re-export shims. Only 5
files are real (`sdk/session.py` 516 LOC, `sdk/live_fill_bridge.py` 283, `__init__.py` 102, and
three 4-line shims). **Zero production code uses the shim surface.** Meanwhile 516 test-file
import sites still route through it, and `trading/tests` is 53,800 LOC against 2,741 LOC of
source — a 20.6× test-to-source ratio in the one package that owns nothing. The migration cannot
finish until the tests move. `bc37cc4 "delete the dead compatibility shims"` removed only the
zero-importer subset.

**2.2.2 — The cycle guard has a structural blind spot.** `tests/test_import_boundaries.py`
proves the graph is a DAG using AST `ast.Import`/`ast.ImportFrom` nodes only. **Every one of the
133 shim edges is a string to `import_module()`.** The "DAG" is an artifact of what the scanner
cannot see. The repo therefore has two dependency systems and the only guard between them is
blind to half.

**2.2.3 — `runtime ↔ trading` is a real ring, not a closed one.** `runtime/src/tradex_runtime/session.py:7`
is a hard top-level `from tradex_trading.sdk.session import ...` — an infrastructure package
importing the composition root — and the boundary test **allowlists it**
(`test_import_boundaries.py:98,287-290`). The reverse edge `trading → runtime` exists in 15 shim
files. Commit `810585f`'s claim that the cycle is broken is true only for the AST-visible subset.

**2.2.4 — 18 packages for one single-process system is over-decomposed.** Eight packages own ≥8
src files; six own ~1 module; three own one file of 19-108 LOC (`operations` 41,
`persistence` 19, `observability` 178). Package count now exceeds the number of architectural
seams worth defending: 17 `pyproject.toml` files, 17 `[tool.mypy]` blocks and a 795-line boundary
test to defend three decisions (pure types / money path / infrastructure).

**2.2.5 — 25 tracked Python files are outside every gate.** `poc/` (20 files) and
`apps/top_gainers/app.py` (1,989 LOC — the second-largest Python file in the repo) are tracked in
git but excluded from ruff, pytest, mypy and (nonexistent) security scanning. `poc/` additionally
contains three copies of `_sql_paths()` and `sys.path` shims pointing at a path that does not
exist. Five workspace members (`research`, `operations`, `observability`, `persistence`,
`services/duckdb-analytics`) get **no mypy at all**.

**2.2.6 — Adding a broker requires editing 15 files with ~30 literal string branches.**
`runtime/live.py` alone has 8 hardcoded `"dhan"`/`"upstox"` branches; `parallel_fetcher.py` has 4;
`interfaces/cli.py` has 5. The OCP violation is measured, not estimated.

**2.2.7 — No read model. There is no CQRS read side.** Every UI read hits the in-memory write
model: `routes/portfolio.py:25` reads `session.engine.cache.all_positions()` per request. The
`persistence` package that was supposed to hold it is a 19-LOC stub re-exporting an execution type.
There is no event→read-model projector; `recover_trading_cache` rebuilds the *write* model from the
log. This is a write model with a replay, not CQRS.

### 2.3 CQRS / event-sourcing suitability

Event sourcing is *shaped* correctly and *scaled* wrong. `execution/src/tradex_execution/sqlite_event_store.py`
is append-only, sequence-ordered, and the folds are pure-ish — but `recovery.py:335` does
`events = tuple(event_store.replay("orders"))`, unpickling the entire history into RAM for three
passes, with **no snapshot, no checkpoint, no compaction, and no retention**. Boot cost is
O(total history) in time and peak RSS and grows without bound. A year of intraday trading is ~10⁶
pickled rows, each containing a complete `Order` with a complete `Instrument`, base64-inflated 33%.

### 2.4 Scanner architecture

`ScannerEngine` (`strategy/core/scanner.py:30-59`) holds **no reference to the OMS cache, positions,
cash or risk**. `ScannerResult` carries no exposure or "already held". A scan returns the same 20
symbols whether you are flat, 90% deployed, or maxed on the sector.

Worse, the streaming buffer is decorative: `run()` is never called in live, and the HTTP route
constructs a **fresh** `ScannerEngine` per request (`interfaces/routes/chart.py:897`) that never
calls `consume()`, so every scan does a `BulkPrefetchMarketProvider` read of all 500 symbols from
parquet. And `_DEFAULT_MAX_BARS = 30` with `deque(maxlen=30)` means any indicator with period >29
returns all-`None` over the streaming buffer, `_value` returns `0.0` (`scanner.py:160`), and
`rsi < 30` matches **every instrument in the universe**.

### 2.5 Plugin architecture

Effectively absent. Strategies are resolved through a string catalog +
`importlib.import_module(f"...{module_name}")` at `chart.py:150`. That is the *only* mechanism
keeping `bracket_breakout.py` (261 LOC) alive, and it is invisible to every gate in the repository.

---

## 3. Quant Platform Review Report

### 3.1 Multi-strategy execution

**Runs, with zero isolation.** Every strategy subscribes to the same bus with **no instrument
filter**, and the frame counter `_bar_count` (`strategy/core/engine.py:129`) is **global across all
instruments** — so a single-symbol strategy's pending-order age is measured in multi-symbol bars.
There is no `portfolio_id` anywhere, no capital allocation, and no P&L attribution: `Position` has
no strategy dimension, and `Order.tag` (which carries `strategy_id@version`) is never used to split
PnL. `execution/src/tradex_execution/envelopes.py` (433 LOC) — the *only* module in the repo
carrying `event_id`, `schema_version`, `strategy_id`, `policy_version`, `code_revision` — has
**zero production importers**. [V, confirmed by two independent scans]

The per-strategy `RiskBudget` that would fix this is fully implemented and **completely dead**:
`RiskConfig` has no `budgets` field, `startup.py:382` never passes it, so `_budgets` is always
`{}`. And it would not work if enabled — `_active_budget` looks up `request.tag` verbatim
(`risk.py:146-147`) while the engine stamps `"sma_cross@1.0.0"`, so a budget named `"sma_cross"`
can never match. The runbook `docs/operations/runbooks/risk-halt.md:54` documents a control that
cannot fire.

### 3.2 Position sizing — the central defect

`qty_value = abs(result.strength) if result.strength else 1.0` [V], duplicated verbatim in
strategy-engine and backtest. `Signal.strength` is documented as signal *quality*
(`domain/strategy.py:21`). Every reference strategy emits `strength=1.0`. There is no
`PositionSizer` type in any of the 18 packages (exhaustive grep). The only quantity validation in
the entire stack is `OrderRequest.__post_init__` → `quantity > 0` (`domain/execution.py:89-90`).
No lot-size check (the data is loaded: `brokers/dhan/master.py:125`), no tick-size check, no cap.

### 3.3 Risk management

**Present and good:** kill switch with UNKNOWN-aware cancel-all; the three-state
`reaches_venue` UNKNOWN policy (`engine.py:613-645`) — the best-designed control in the repo;
persist-then-mutate with `require_durable_events`; fresh-mark gate; re-risk on modify before venue
dispatch; fail-closed cash gate.

**Absent by default, because every limit is `None`:** [V]

| Control | Status |
|---|---|
| Per-order notional cap | `risk.py:416-424`, only if `max_order_value` set — never |
| Portfolio exposure cap | `risk.py:452-455`, only if set — never |
| Orders per minute | `risk.py:475-483`, only if set — never |
| Daily loss stop | `risk.py:459-472`, only if set — never; **baseline resets on restart** |
| Drawdown stop | same; **baseline resets on restart** |
| Per-symbol / sector concentration | **does not exist** (no sector metadata in the domain instrument) |
| Gross short exposure | **unbounded** — `risk.py:431` gates BUY only |
| Product type (MIS/CNC/NRML) in accounting | `ProductType` reaches the broker payload and **never touches the accounting layer**; `Fill` has no `product_type` |

Additional live defects:

- **The session-level order gate is dead code.** `trading/src/tradex_trading/sdk/session.py:317-325`
  defines `_make_order_gate()` and it is **never called**; `AppConfig.live_orders_enabled` is read
  by `from_dict` and nothing else; `boot()` never passes it. The documented "one config flip stops
  all order flow" control does not exist.
- **Daily-loss and drawdown baselines reset on every restart** (`risk.py:100-102,536-549`). Crash-
  restart at 11:00 after losing ₹50k against a ₹30k limit, and the breaker is gone.
- **A fill resets `unrealized_pnl` to a false zero** (`position_math.py:145-150`) and the daily-loss
  guard reads exactly that field. In a circuit-limited or halted name there is no further tick, so
  the drawdown gauge reports a frozen, fully-loss-making position as **flat** — precisely when the
  market is most dangerous. The runbook tells operators to read `/positions`, which shows `0`.
- **The fresh-mark gate is bypassed by any order that nets down** (`risk.py:411-413` gates on
  `_increases_exposure`, which is a *net* test). A SELL of 150 against a long 100 is classified as
  a reduction, so it passes the mark gate, the daily-loss gate and the drawdown gate while opening
  a 50-lot short.
- **No leverage or margin model.** The cash gate binds to `Account.balance`, which for Dhan is
  `availabelBalance` and for Upstox is `available_margin` — **margin-utilisation-adjusted buying
  power, not cash**. On a Dhan MIS account that is ~5× settled cash; Upstox NRML ~20×. That inflated
  number is used both as the risk cash gate *and* as the opening anchor for the engine's own
  `CashLedger` (whose `require_debit` floor is therefore also 5-20× too permissive). The platforms'
  `margin_calculator` endpoints exist and are never called from any pre-trade path.
- **CNC holdings are invisible to the book.** `get_positions()` returns short-term positions only;
  `get_holdings()` is read by nothing in `boot()`, the `RiskManager`, or reconciliation. A delivery
  position created outside this engine is absent from the risk book, while `/holdings` proxies the
  broker and shows it to the operator.
- **Risk is bypassable through the bus.** The bus is public, so any strategy can
  `bus.publish(PlaceOrderCommand(...))` and skip `protective_request()` (silently dropping SL/TP —
  the exact failure the repo's own guide warns about), skip the `next_open` deferral, and submit CNC
  with no cash gate at all.

### 3.4 PnL and accounting

- **Live partial fills are booked at the cumulative average price, not the incremental fill price**
  (`trading/src/tradex_trading/sdk/live_fill_bridge.py:205-206,212,231`). Fill 1 = 100@₹100,
  fill 2 = 100@₹110 → the bridge books 100@₹100 then 100@₹105. WAC becomes ₹102.50 instead of ₹105.
  **Every subsequent realised P&L on that position is wrong.** The trade book already carries
  per-trade price and quantity — the bridge synthesises a delta instead of using it.
- **Fees are hardcoded to intraday and cannot express delivery.** `fees.py:111-119,149-153` both
  call `equity_intraday(...)` unconditionally. `equity_delivery()` exists with **zero production
  callers**. `Fill` carries no `product_type`, so the model *cannot* vary. Post-Oct-2024 delivery
  STT is 0.1% both sides; the code charges 0.025% sell-only — delivery cost understated ~5×. Missing
  entirely: DP charges, IPF/NSE research fund, clearing/exchange funds, BSE rate table, F&O schedule.
- **Fees are OFF by default, including live.** `config/schema.py:112` `fees_enabled: bool = False`
  [V]; `startup.py:237` builds `FeeCalculator()` only if set. In live: realised P&L is **gross**,
  the cash ledger never pays STT/GST/brokerage, and the cash gate over-states buying power by
  ~0.15% per side.
- **Position and order books diverge on an overfill.** `engine.py:831-834` books the full fill
  quantity to positions and cash, then `OrderManager.on_order_filled` **clamps** to the order's
  remaining (`order_manager.py:38-45`). A venue over-fill — real on liquid names, common on Dhan —
  leaves the position permanently overstated vs the OMS and the broker, and that drift then trips
  the kill switch at the *next* startup.
- **No corporate-action handling in live at all.** `on_corporate_action` has exactly one caller:
  the backtest. A 2:1 split leaves the position at 100@₹1,000 while the market is ₹500: a phantom
  −50% drawdown spike, a permanently wrong cost basis, and a −50% realised loss on exit that never
  happened. Dividends are never credited in live.
- **Realised P&L is WAC with no lot ledger** — defensible as a display convention, but the figure
  an Indian trader files must be FIFO-derived from the contract note. Two numbers will never agree
  and nothing reconciles them, even though `get_ledger()`/`get_trade_history()` exist.
- **No corporate-action/fee-authoritative event-log reconciliation.** Recovery folds the log well
  (latest `CashAccountInitialized` anchor, fee taken from the event not a rate table, and it raises
  rather than reporting zero when there is no anchor) — this is materially better than most. But it
  requires `cfg.persistence.path`, which is `None` by default: without it the position book is
  pure in-memory and a restart loses all P&L **silently**.

### 3.5 Order lifecycle

The state machine is **sound**: 9 states, `FILLED`/`REJECTED` sinks, `CANCELLED` non-terminal
(correctly permitting the cancel/fill race), `UNKNOWN` deliberately non-terminal
(`domain/execution.py:25-65`).

The enforcement is **not centralised**: `_LEGAL_TRANSITIONS` is consulted only by
`Order.transition_to`, and statuses are written directly at **6+ sites** —
`order_manager.py:54` (the hot fill path, via `replace(order, status=…)`), `paper/adapter.py:742-767`,
`fill_sources.py:150,228,445` (constructs `Order(status=FILLED)` from scratch),
`sqlite_store.py:277-318`, `recovery.py:111-172`. The module *named* "Order lifecycle FSM" is the
one that bypasses it. An audit reasoning from the transition table is reasoning from fiction.

**Ambiguous submissions are correctly modelled and never resolved.** `engine.py:613-645` files
`UNKNOWN` and trips the kill switch — but nothing ever transitions it out, `is_terminal` returns
`True` for it, the kill switch is cleared only by a raw unaudited setter (`engine.py:1310-1316`),
and reconciliation runs **only at startup**. A lost fill, a manual portal order, or a broker
auto-square-off is invisible for the rest of the session.

**Startup cash reconcile compares two structurally different quantities and will halt live
trading.** `startup.py:800-830` compares local settled cash against `Account.balance` (available
*margin*). `compare_funds` raises HIGH on any difference >₹0.01 and HIGH trips the kill switch,
refusing `session.start()`. The two numbers can never agree. The test mocks `Account.balance` as
settled cash, so it passes against a semantic the real adapters never provide.

### 3.6 Backtesting validity

**This is the least trustworthy area of the platform and it is the user-facing decision surface.**

| # | Divergence | Severity |
|---|---|---|
| 1 | Final-bar `flush_pending(candle)` fills a last-bar signal at **that bar's own open** [V] | **CRITICAL** |
| 2 | Recording-only bridge fills at **the current bar's close** (`backtest.py:471-487,503-515`) — textbook look-ahead, and it is a coin flip that silently determines whether a backtest looks good | **CRITICAL** |
| 3 | Cash debit in a bus subscriber *after* booking; overdraft swallowed by bus error isolation → flattering equity, no error (`backtest.py:326`) | **CRITICAL** |
| 4 | Engine's `require_debit` never runs (engine built with no `cash=`) | **CRITICAL** |
| 5 | User-facing route passes **no risk manager, no slippage, no corporate actions** (`interfaces/routes/chart.py:785-793`) | **CRITICAL** |
| 6 | No idempotency guard, all three fail-closed gates default `False` (`backtest.py:271-277` vs `startup.py:397-406`) | HIGH |
| 7 | Protective exit fills at **exactly** the stop price — no gap-through model, on a venue whose resting leg gaps | HIGH |
| 8 | Survivorship: `delisted` filter exists, correctly designed, **never populated**; default universe is *today's* Nifty 500. `research/pit.py` ships `assert_historical_universe` and **nothing calls it** | HIGH |
| 9 | Walk-forward carries **no warmup** between windows — explicit `ponytail:` deferral at `walk_forward.py:139-140,235-236` [V]. Every OOS window starts with an empty close history, so a 20-period SMA emits nothing for its first 20 OOS bars and those trades are silently absent | HIGH |
| 10 | Walk-forward `_aggregate` annualises OOS Sharpe as **daily regardless of bar frequency** (`:360`), and the fallback path *averages* per-step Sharpes | HIGH |
| 11 | Grid search takes the **max** over the full grid with no trial count, no Deflated Sharpe, no Reality Check, no parameter-stability check. `_evaluate` swallows every exception, so `best` is chosen from a survivor-biased subset | HIGH |
| 12 | `BacktestEngine` reuses a shared `RiskManager` and calls `reset_rate_window()` **unconditionally** — an in-process backtest would evaluate `max_position_value` against the *live* book and wipe the live rate window | MEDIUM |
| 13 | `num_trades` counts **fills**, not round trips, and the UI labels it "Trades" | MEDIUM |

### 3.7 Missing trading concepts

Pre-open auction; square-off/auto-close at 15:20; MIS/CNC/NRML product handling in accounting; MTF;
basket and split orders; GTT; OCO and trailing stops; per-symbol circuit-limit and halt handling;
index rebalancing; dividends and rights in live; delisting; margin shortfall; multi-account;
`TimeInForce` has exactly one member (`DAY` — no IOC, no GTT, no TTL).

### 3.8 Multi-broker

**One broker per process, one process per account, and the write lock is per *file*, not per
account.** There is no arbitration. Worse, **the OMS has no broker/account dimension**:
`TradingCache` keys orders by `order_id` alone (`trading_cache.py:45`) and positions by
`instrument_id` alone; `SQLiteEventStore._infer_stream` hardcodes `"orders"`/`"system"`
(`sqlite_event_store.py:72-73`). Two brokers in one process whose venues mint colliding order ids
(both use numeric strings) **silently overwrite each other** with no detection — and reconciliation
reports "no drift" because both books agree with the merged one. This is a data-integrity blocker,
not an architectural inconvenience.

---

## 4. Code Smell Report

### 4.1 Complexity — no gate exists at all

`ruff` selects `["E","F","W","I","UP"]`. No `C901`, no `PLR0911/0912/0915`, no radon, no xenon.

| Function | file:line | lines | branches |
|---|---|---:|---:|
| `ws_stream()` | `interfaces/routes/stream.py:49` | **1,467** | **310** |
| `create_chart_router()` | `interfaces/routes/chart.py:245` | **749** | **103** |
| `_boot_tail()` | `runtime/startup.py:356` | 421 | 42 |
| `BacktestEngine.run()` | `replay/backtest.py:226` | 403 | 61 |
| `ExecutionEngine._run_pipeline()` | `execution/engine.py:428` | 305 | 51 |
| `create_app()` | `interfaces/fastapi_app.py:123` | 297 | 21 |
| `_replay_start()` | `interfaces/routes/stream.py:1077` | 211 | 50 |
| `RiskManager.check()` | `execution/risk.py:354` | 134 | 37 |

48 of 2,494 functions exceed 80 lines; 7 exceed 40 branch points. A 1,467-line function with 310
branches passes CI green. `startup.py` is a legitimate composition root (`boot()`, 158 LOC) that
has grown a 421-line `_boot_tail()` carrying a whole startup-reconciliation engine — that belongs
in `persistence`, which is a 19-LOC stub.

### 4.2 Dead code

| Module | LOC | Evidence | Verdict |
|---|---:|---|---|
| `execution/src/tradex_execution/envelopes.py` | 433 | Sole importer is a test, and that test imports the *shim* [V] | **DELETE** |
| `frontend/src/{shellbar,chart-lifecycle,chart-state,chart-types,ladder,trade-bar}.ts` | **1,112 (16.5% of all TS)** | Zero importers; `mountTradeBar`'s body is literally empty | **DELETE** |
| `operations/src/tradex_operations/` | 41 | 0 production importers | **DELETE package** |
| `persistence/src/tradex_persistence/` | 19 | 0 production importers | **DELETE package** |
| `brokers/common/http_response.py` | 59 | Only its own test | DELETE |
| `market_data/universe.py` | 27 | Shim of a shim, 0 importers | DELETE |
| `duckdb-analytics/.../screener.py` | 63 | No console script entry | DELETE |
| `interfaces/routes/__init__.py:24 ALL_ROUTES` | 9 | Docstring claims `create_app` iterates it; `create_app` hardcodes its own list | DELETE |
| 15 remaining zero-importer shims | ~180 | | DELETE |
| `startup.py:958-984 _attach_event_store` | 27 | Superseded by `engine._append`; only tests call it | DELETE |

Clean, for the record: no unused config keys (all 29 fields have ≥2 readers), no unused CLI flags
(all 30 are consumed), no `yaml.load`, no `shell=True`, no `os.system`, no bare `except:`.

### 4.3 Error handling

189 `except Exception:` (56 brokers, 52 interfaces, 29 runtime, 22 execution) and 35 swallowed with
`pass`. Most are defensible at network boundaries. **10 `assert` statements in `*/src/` used as
runtime validation** — including `brokers/dhan/tick_parser.py:60,74,77` asserting invariants over
*untrusted third-party wire data*, and `runtime/bar_aggregator.py:229,267` in bar aggregation.
`assert` is stripped under `python -O`. These must be `raise`.

Five `raise ... from` omissions in exception handlers.

### 4.4 Duplication

**1,078 lines duplicated between `dhan` and `upstox`** (difflib-matched): `ws_streams.py` 273,
`client.py` 203, `adapter.py` 179, `_orders.py` 66, `_portfolio.py` 65, `master.py` 64,
`_facade.py` 48. The seam is half-built — `common/ws_shared.py` (82) and `ws_reconnect.py` (279)
already exist. Estimated cost of a third broker: **1,400-1,600 lines, of which ~500 is copy-paste
of the two existing brokers and ~0 is new correctness.** A third broker would arrive with the same
validation gap, the same dead `submit_super_order` gap, and the same no-heartbeat gap, because none
of those live somewhere a new broker is forced to touch.

**Test fixtures: 1,652 test files, 3 `conftest.py` in the whole repo.** `_make_request` redefined in
12 files, `_make_order` 10, `_make_fill` 9, `_make_engine` 8; 12 byte-identical `test_import_package`
bodies. A `tests/support/` package is already half-referenced and simply unused.

**Frontend:** 5 copies of `el()`, 4 of `injectStyle()`, 2 of `createPanel` — and `screener.ts:257`
reimplements the dock's panel *divergently* (missing tab registration, inverted error/empty
precedence), which is why the screener has no tab.

**Indicator math:** `_sma` re-implemented privately in `multi_symbol_sma_cross.py:117` and
`sma_cross.py`, `_atr` in `bracket_breakout.py`, while `analytics/indicators.py:116-119` holds the
canonical versions. Three live copies.

### 4.5 Primitive obsession

85 `-> Any` and 332 `dict[str, Any]` in `*/src/`; 33 `Any` in domain+execution signatures including
`def _dispatch(self, send: Any, request: OrderRequest) -> Any` in the order-dispatch path.
`Decimal | float | str` unions at `cash_ledger.py:33` (a value that may be three types is not a
money type, and `str` bypasses the `__post_init__` coercion `RiskConfig` performs). `BrokerConfig.name`
and `.environment` are `str` rather than `BrokerId`.

Timestamps, by contrast, are genuinely disciplined — IST tz-naive is enforced at the write
chokepoint and 0 raw-timestamp smells were found. Keep that.

### 4.6 What the code does *not* do wrong

Worth stating, because it is a real inventory: `domain` has zero internal dependencies; money
arithmetic is `Decimal` throughout the money path; all SQL is parameterised; the read-only DuckDB
surface is properly guardrailed; event objects are `frozen=True, slots=True`; the idempotency
guard's `IntegrityError`-based reserve is correct and race-tested; the Dhan/Upstox claim-priority
resolution is deterministic; the E2E harness seeds a real datalake so the browser suite cannot pass
vacuously; the golden-fixture provenance gate is genuinely excellent.

---

## 5. Testing Gap Analysis

### 5.1 Measured

| Metric | Value |
|---|---|
| Tests collected | **4,341** (2 skipped, 10 xfailed, 108s) |
| Test LOC / src LOC | 73,162 / 58,539 = **1.25 : 1** |
| Coverage | 88.2% aggregate (CI), floor 85% |
| **Branch coverage** | **DISABLED** (`has_arcs = 0`) |
| `hypothesis` | **13 `@given` in 4,341 tests**, 3 files |
| pytest markers registered | **0** |
| Python flake detection | **none** |
| Load / perf tests | **0** |
| Frontend unit tests | **0** (no vitest/jest) |
| E2E | 52 tests / 11 specs, `workers: 1` |
| `conftest.py` files | 3, root one has **zero fixtures** |
| Ruff ratchet status | **FAILING NOW** — 13 `I001` vs a baseline of `0` |

### 5.2 The structural defect

**Test ownership is fiction where it matters most.** `trading/tests` = 2,828 tests (65%),
`brokers/tests` = 866. Then: `replay` 1, `strategy` 1, `persistence` 1, `analytics` 1, `reactive` 1,
`application` 1, `market_data` 1 — and each of those single files is a **137-171 byte import smoke
test** (`assert pkg is not None`). A 774-line `backtest.py` whose main loop is 26.9% covered is
gated by a test that only checks the package imports. `interface` tests live in
`trading/tests/interface/` (36 files) while `interfaces/tests/` has 3 files / 6 tests.

**Branch coverage is off.** For a money platform this is the single most consequential
configuration choice. `if`/`else` and `try/except` arms on order status, fill direction and fee
tier *are* the arithmetic; line coverage counts `if x:` as covered whether the other side is taken.

**The aggregate floor cannot see the spread:** `position_accountant.py` at **60.6%**
(`on_corporate_action` 33.3%, `remark` 22.2% — the live position book, worst-covered money module
in the repo) and `resilience.py` at **48.9%** (`CircuitBreaker.request` and `RetryableHttpClient.send`
largely unexercised) sit invisibly beside five packages at 100%. `backtest.py run()` is missing 45
of 167 statements. `walk_forward.py` is 15-24% covered. `dhan/_facade.py` and `upstox/_facade.py`
are **0.0%**. `paper/adapter.py` — the reference implementation every parity test compares against —
is **63.7%**.

**Assertion-free money-path tests.** `trading/tests/execution/test_risk_tz.py` — 6 tests, **0
asserts**, uses `pytest.raises` only. `test_boot_safety.py` — 18 tests, 11 asserts, on *boot safety*.
These raise the test count while testing nothing.

**Chaos: present but mis-named and thin.** `test_chaos_parity.py` injects **no faults** — it is a
load soak with a deterministic price walk. What *is* excellent: `tests/regression/test_broker_timeout_unknown.py`
exercises a real post-send timeout through four production links and asserts `UNKNOWN` not `REJECTED`,
kill switch tripped, and `issubclass(TimeoutError, OSError)`. **Do not regress that file.**

Absent entirely: socket half-open / `ConnectionResetError` (**0 occurrences repo-wide**), DNS
failure, clock skew (**55 `datetime.now()` reads in tests, no injected clock**, 10 of them in the
rate-limit-window test file), token expiry *mid-flight*, **out-of-order event delivery**, corrupt
event log, corrupt parquet, disk full, and the *other* double-fill shape (venue accepted, HTTP 500
on the response — only the timeout shape is tested, and 500-on-POST is explicitly non-retryable).

Zero tests submit concurrent orders to one engine from multiple threads (0 `ThreadPoolExecutor`
tests in the suite). That is precisely where a duplicate live order happens.

### 5.3 The 15 missing tests that matter

1. `domain/tests/test_order_state_machine.py` — parametrised over **every** (from_status, event)
   pair. *Requires extracting an `ALLOWED_TRANSITIONS` table first; that extraction is the
   highest-leverage single change in this section, because today the FSM is untestable.*
2. `execution/tests/test_pipeline_branch_matrix.py` — all 8 `_run_pipeline` exit paths.
3. `execution/tests/test_position_accountant_matrix.py` — `on_fill`/`on_fee`/`remark`/
   `on_corporate_action` across long/short/flip/zero/stale-mark.
4. `domain/tests/test_fees_properties.py` — hypothesis: fee ≥ 0; monotone in qty; brokerage ≤ cap;
   q2 associativity ±1 paisa; buy-sell ≡ sell-buy fees. **Zero properties exist for 289 LOC of fee engine.**
5. `execution/tests/test_idempotency_properties.py` — any permutation of a fill stream delivered
   twice yields an identical book. The double-fill class.
6. `execution/tests/test_event_log_migration.py` — a v(N-1) row must still replay after a bump.
   `SCHEMA_VERSION` has exactly one version and one decoder, tested neither way.
7. `tests/regression/test_venue_accept_then_500.py` — the sibling of the best test in the repo.
8. `brokers/tests/common/test_token_expiry_inflight.py` — valid at submit, 401 on response.
9. `execution/tests/test_concurrent_order_submit.py` — real `Barrier` + N threads.
10. `replay/tests/test_backtest_run_matrix.py` — empty series, all-BUY ladder, split on/off the bar.
11. `replay/tests/test_walk_forward_properties.py` — OOS segments disjoint in time; IS and OOS never
    share a bar.
12. `brokers/tests/common/test_circuit_breaker_states.py` — full CLOSED→OPEN→HALF_OPEN→CLOSED table.
13. `trading/tests/interface/test_chart_endpoints_seeded.py` — replaces 4 `pytest.skip`s that let a
    7.7s scanner test and a 5.5s protective-level test **skip themselves and report green**.
14. CI: add `--cov-branch` + a per-package floor; set `.ruff_baseline` to ~20, not `0`.
15. `frontend/e2e/a11y.spec.ts` + vitest for `validateOrderIntent`, `pairRoundTrips`,
    `workspace.ts` boot deciders, `replay.ts` frame validators.

### 5.4 CI gate

Gates a merge: ruff ratchet, mypy ×12 (`brokers` is `continue-on-error`), pytest `--cov-fail-under=85`,
a `parity.yml` that re-runs ~10% of the suite as a redundant second job, and a well-built
`frontend-gate.yml`.

**It would catch:** fee arithmetic regressions, the broker-timeout→wrong-status class, capability
regressions, golden indicator drift, stale bundles.
**It would not catch:** a branch-level regression in `_run_pipeline`; a `position_accountant` MTM or
corporate-action bug; an event-log schema incompatibility; **any latency regression**; a
permanently-flaky E2E test (`retries: 1` with no flake *detection*).

**One collection error takes down the entire gate** — no `--continue-on-collection-errors`, and a
transient typo in a `trading/scripts/*.py` import was observed during this review failing the whole
job at collection.

---

## 6. Reliability Assessment

### 6.1 Failure-mode matrix

| Failure mode | Detected | Alerted | Survivable | Runbook |
|---|---|---|---|---|
| Broker REST 5xx burst | Yes (retry + breaker, idempotent methods only) | **No** | Yes | `broker-disconnect.md` |
| Broker WS drop | Yes (10 exp backoff + jitter → `DEGRADED`) | **No** | Yes | `broker-disconnect.md` |
| Broker outage > 5 min | Yes (stale 30s → 503 readiness) | **No** | **Yes, fail-closed** | `stale-feed.md` |
| Internet/VPN drop | Same path | **No** | Yes | both |
| Host reboot mid-session | Partially (log rebuild + startup reconcile) | **No** | Yes, but kill-switch armed | `oms-recovery.md` |
| OOM kill | **No** — no RSS metric, no limit | **No** | **Unknown** | **none** |
| Disk full | **No** — zero `statvfs`/`ENOSPC` handling | **No** | **No** | **none** |
| Token expiry mid-order | Partially; **`cancel_order`/`modify_order` get no 401 retry** → a bracket cancel 401s and the **protective legs stay live with no stop on the position** | **No** | **Degrades dangerously** | **none** |
| Partial-fill chain | Yes | **No** | Yes | — |
| Strategy throwing 1000×/s | Yes (bus isolates) | **No** | Yes, but burns the loop | **none** |
| Event log unwritable | Partially — `engine._append` logs but **does not increment a metric**; the *order mirror* does count failures, the *authority* does not | **No** | **No** | **none** |
| Clock drift | Partially (integrity tracker flags >60s) | **No** | Degrades | **none** |
| Stale parquet → wrong backtest | No (offline tools only) | **No** | **Yes, silently wrong** | partial |

**Seven of these thirteen have no runbook.**

### 6.2 Single points of failure

The single process (one Python process carries REST + WS + broker feed + bus + strategy engine +
execution engine; `workers>1` is **refused in live mode** so HA is architecturally off the table);
the single SQLite event log (one connection, one lock, `PRAGMA synchronous` never set);
the single parquet lake (intraday history beyond 90/30 days is **permanently unrecoverable**);
the single in-memory `TradingCache`; the single asyncio loop shared by REST+WS+replay — where
`routes/orders.py:376-403` is `async def` and calls `engine.submit()` **synchronously**, so a broker
round-trip **blocks the entire event loop**; the single bus, where `bus.py:224-235` `break`-drops the
entire pending backlog on overflow and the bus then looks healthy.

### 6.3 The concurrency findings that are the most damaging in the repo

1. **The global bus lock is held across broker HTTP.** `ThreadSafeReactiveBus.publish` takes a
   process-wide `RLock` (`reactive/thread_safe_bus.py:55-58`) and `ReactiveBus.publish` is a
   synchronous inline drain (`bus.py:222-242`). The execution pipeline runs as a subscriber and
   `engine.py:574` does a real venue POST inside it. **Every other publisher blocks for the full
   broker round-trip**, and the same thread is the market-data thread.
2. **The venue order submit runs on the WebSocket receive thread.** RxPY `Subject` dispatch is
   synchronous, so a strategy signalling on a quote executes `self._fill.submit(request)` — 115 µs of
   `flock`, a possible `time.sleep` in the rate limiter, and a 30-250 ms venue round-trip — **on the
   thread decoding every other symbol's tick**. One 250 ms order at the 09:15 open on 500 symbols is
   5,000-15,000 ticks of added queueing delay, and the 1 Hz forming-frame throttle means the *bars*
   look fine while the tape is stale.
3. **`CashLedger` has no lock at all** [V] (`cash_ledger.py:36`, a bare `Decimal`), and
   `engine.py:826-834` is `require_debit` (check) → `on_fill` → `_apply_cash_fill` (debit) →
   `_apply_fee` (debit) as **three separate unlocked critical sections**. Two concurrent `submit()`
   calls both pass the check and both debit. Every other shared structure in the engine is locked;
   the one that decides solvency is the one that was missed.
4. **Undocumented three-way lock order.** `Risk._lock → _positions_lock` and
   `position_lock(k) → _positions_lock` and `snapshot(): _orders → _positions → _quotes`. It is
   currently safe by ordering accident, not by construction. The per-instrument lock map is dynamic
   and grows one `RLock` per instrument ever seen, never reclaimed.
5. **Kill switch trips inside a publish** (`engine.py:639-643`), and tripping performs N broker
   cancel round-trips under the bus lock.
6. **Unbounded thread creation on broker reconnect**; unbounded dynamic lock maps in
   `TradingCache` and `ParquetStorage._partition_locks` (25,868 entries by end of 2026).

### 6.4 The feed never reaches READY on a clean cold boot

`FeedState` has 6 states and 8 mutators, **none validated** (`feed_supervisor.py:11-17,67-111`) —
`connected()` will happily move `HALTED → CONNECTING`. `recovery_succeeded()` is the **only** writer
of `READY`, and its only caller is `FeedRecoveryCoordinator.recover()`, which **`boot()` never
calls**. `MarketFeed.start()` sets `CONNECTING` then `RESYNCHRONIZING` and stops. So
`supervisor.ready` is `False` and `_run_pipeline` (`engine.py:462-468`) rejects **every** order with
`feed_not_ready` — until the first socket reconnect happens to trigger recovery. A `DEGRADED` feed
never self-heals.

This is the most consequential *unverified* claim in the report: if the panel is right it means live
trading cannot start at all; if there is a path the panel missed, it means READY is reached by
accident. **Verify this first** — it is a five-minute test and it gates the whole platform.

### 6.5 Health checks and observability

`/health` and `/health/live` both `return HealthResponse(status="ok")` **unconditionally** — pure
liveness pings masquerading as health. A process whose feed has been dead for four hours returns
200 forever. `/health/ready` is genuinely good (requires `READY` **and** `feed_supervisor.ready`),
except that a **missing** supervisor returns ready-by-absence.

**There is no alerting.** `docs/operations/metrics-catalog.md:22-49` has a "Suggested alert" column
with real thresholds. It is a *document*. `grep` for `alertmanager|prometheus.yml|alert:` across all
yml/yaml/toml returns nothing. **~50 lines of Prometheus rules converts a documentation exercise into
a working alarm** — the highest-leverage reliability action available.

`setup_structured_logging` and `set_correlation_id` are **called only by their own test** — every
production log line is unstructured text with no correlation id. There is **no end-to-end trace id**
from WS tick → signal → order → venue, and the one identifier that exists is the client-chosen
`Idempotency-Key`. Histograms render as `summary` with **no buckets**, so the catalog's own
`p95 > 60s` alerts are not computable. `metrics.py:23-24,36-37` **accepts labels and does
`del labels`** — the API lies by omission, and `/metrics` cannot answer "which symbol rejected".

The **runbooks are the strongest documentation in the repo** (7 files, Scope/Severity/Detection with
concrete `curl`/Halt/Diagnose/Recover, referencing real metric names). Seven failure modes have none.

---

## 7. Security Assessment

**No secret is committed to git.** Verified: `.env.local` is gitignored, has zero commits in any
ref, and `git log -p --all -S` across 40 revisions found no credential, no `sk_live`/`ghp_`/`AKIA`/
`xox`/`BEGIN PRIVATE KEY`. `/runtime/*` is ignored except `pyproject.toml`/`src/`/`tests/` with an
explicit comment that widening it "would commit credentials". That is genuinely good hygiene.

| # | SEV | Finding | Consequence |
|---|---|---|---|
| S1 | **CRITICAL** | `GET /` is unauthenticated and injects the live API key into the HTML (`fastapi_app.py:388-395`); the SPA scrapes it at module load (`apikey.ts:15-27`) [V] | `curl http://127.0.0.1:8000/` yields the money-moving credential in cleartext, bypassing the session cookie, CSRF and Origin controls it was meant to protect. Any local process, any SSRF, any user on the box gets order-placement rights. Any XSS in the gitignored chart library exfiltrates them. |
| S2 | **CRITICAL** | No key configured ⇒ no auth, silently: `auth/deps.py:126-127` `if expected is None: return`. Only a **startup** check fires, and only when `mode == "live"` | A caller invoking `create_app()` / `uvicorn ...:serve_app` directly bypasses it entirely → wide-open order API. Make `require_auth` fail closed on request, keyed to a live `BrokerId`, not to `mode`. |
| S3 | **CRITICAL** | `/docs`, `/redoc`, `/openapi.json` are live in production (`fastapi_app.py:134-138`) | Publishes the complete order/bracket/WS-command surface. With S1, the key is one `curl /` away. |
| S4 | **CRITICAL** | `PUT`/`DELETE /workspace/{id}` have **no** `require_auth` (`chart.py:971,988`) while the two sibling GETs do (`:958,963`) | Unauthenticated overwrite (with `force: true`, defeating the revision check) or deletion of every user's chart workspace. |
| S5 | **HIGH** | WS credential travels in the **query string** (`?api_key=`) and uvicorn's access log is on | The live credential lands in stderr, journald, log shippers and browser history. Use the session cookie on the upgrade (already supported at `fastapi_app.py:347-353`). |
| S6 | **HIGH** | `.env.local` is `-rw-r--r--` [V] and `runtime/dhan/token_state.json` is `-rw-r--r--` [V] (`upstox`'s is correctly `0600`) | One `read` yields a **complete unauthenticated account takeover on both brokers** — client id + access token + PIN + TOTP secret in one world-readable file, and TOTP lets an attacker mint fresh tokens indefinitely. `chmod 600` both and **assert 0600 at boot, failing closed in live**. |
| S7 | **HIGH** | `pickle.loads(base64.b64decode(payload))` on the OMS event log and the idempotency result column (`sqlite_event_store.py:108`, `sqlite_store.py:445`) | RCE by construction against a local unencrypted SQLite file — as the trading process, holding live tokens. Also `ErrorOccurred.error: Exception` pickles `__traceback__` and open sockets into the log, and one undecodable row **aborts the entire replay** → unbootable OMS. |
| S8 | **HIGH** | The OMS audit trail is not tamper-evident — plain `INSERT`, no hash chain, no remote/WORM sink | For a regulated real-money system the order log is a compliance artifact. Any process with filesystem access can rewrite a fill to conceal activity. |
| S9 | **HIGH** | No request signing, no nonce, no timestamp window, no per-client identity | A captured order request is **replayable verbatim** against any instance sharing the key, forever. |
| S10 | **HIGH** | `Idempotency-Key` is client-supplied, used verbatim as the `CorrelationId`, with no namespace or per-principal scoping; `idempotency.py:97` deliberately does not release | **Key squatting**: guessing a client's next key pre-reserves it → the legitimate order gets 409 and is **never placed**, permanently. Plus a free oracle over another operator's order history, and an unbounded `_completed` dict. |
| S11 | **HIGH** | `GET /metrics` unauthenticated; WS auth bypassed entirely when no key is configured (`fastapi_app.py:334-376`) | An unauthenticated client can open `/ws/stream` and drive `subscribe*` **and `replay_start`** — a CPU/event-loop DoS against the trading process. |
| S12 | MEDIUM | `parquet_storage.py:320-334` interpolates the caller-supplied symbol into a filesystem path; `Equity.of` validates nothing | `NSE:../../../../x` walks out of the lake. Bounded to `year=*/month=*/data.parquet` and `pq.ParquetFile`, so it is a **parquet-shaped arbitrary read**. Reject symbols not matching `^[A-Z0-9&.-]{1,32}$` at the route edge. |
| S13 | MEDIUM | `TRADEX_DATALAKE_ROOT` / `TRADEX_WORKSPACE_DB` / `TRADEX_RUNTIME_DIR` accepted with no validation | A typo or hostile profile silently repoints the lake, the workspace DB, or the **token state directory**. Validate and log the resolved value at WARNING on every boot. |
| S14 | MEDIUM | `secure=False` on the session cookie with a `ponytail:` deferral comment; no `__Host-` prefix | Behind any TLS terminator the cookie is cleartext client→proxy. Plumb the promised flag. |
| S15 | MEDIUM | `except Exception as exc: detail = str(exc)` returned to the client on 400/404/500/502 across ~10 sites | Leaks broker error strings, endpoint names, possibly upstream fragments. |
| S16 | **HIGH** | Every GitHub Action pinned to a **floating tag**; `pip install uv` unpinned; **zero** dependency scanning (no `pip-audit`, `npm audit`, `osv-scanner`, `trivy`, CodeQL, `dependabot.yml`, SBOM) | 89 locked Python packages plus the whole npm tree ship unreviewed. For a system with `pickle.loads` and a query-string credential, an unpatched transitive CVE is directly exploitable. |
| S17 | MEDIUM | `openalgo-charts` is a **gitignored external clone**; only the commit SHA is recorded, and the library's `dist/` is the ground truth the Python golden fixtures were captured from | A force-push or compromised upstream changes what you build with **no diff in your own repo**. The pin is CI-verified (good) but never hashed or SBOM'd. |

**Positives to keep:** constant-time comparison everywhere (`secrets.compare_digest` at
`deps.py:88,121,209`, `auth/routes.py:128`, `rate_limit.py:117`, `csrf.py:28,34`); wildcard origin
explicitly refused (`auth/origin.py:77-83`); CORS is a single explicit origin, so a malicious page
**cannot** POST to `localhost:8000`; no `eval`/`exec` beyond the base-broker pass-through wall
(itself replaceable with a descriptor factory); zero `yaml.load`, zero `shell=True`; all SQLite
parameterised; the DuckDB denylist is real.

**Authorization: there is none.** Single-tenant, all-or-nothing. `SessionRecord.subject` is the
literal `"operator"`, no roles/scopes, `WorkspaceStore` has no owner column. Any authenticated caller
can place orders, read all balances and positions, run arbitrary backtests and scanners, and
read/write every user's workspace. That may be an acceptable v1 for a single-operator box — but it
must be *stated*, because "auth" currently means "some process knows a shared string."

---

## 8. Performance Assessment

All numbers below were **measured on this checkout** (CPython 3.13, this host) with the real
production modules and a real Dhan type-8 full packet.

### 8.1 The tick path

| Stage | Cost |
|---|---:|
| `parse_tick_frame` (real 162-byte frame) | 7.9 µs |
| `parse → row_to_quote` (20 depth levels) | 32.4 µs |
| `FeedIntegrityTracker.on_quote` | 2.6 µs |
| `bus.publish` (1 subscriber) | 11.7 µs |
| **TAPE TOTAL** | **44.1 µs/tick = 22,661 ticks/s** |
| `BarAggregator.on_quote` | 2.3 µs |
| `ensure_token` (`flock` + 2 disk reads) | **115 µs, ×2 per HTTP request** |
| Datalake tail read, 1 symbol × 1 month | **16.8 ms — on the asyncio event loop** |

**The ceiling: ~22,700 ticks/s total across all sockets and all consumers.** Real NSE cash-equity
full-depth for 500 liquid names runs 5,000-20,000 prints/s at the open. **The design is at or past
its limit at the open, before any growth.** Threads buy nothing — the work is 100% pure-Python
bytecode, so a second thread is net slower under the GIL.

The 24 µs of `build_depth` is 40 `Price`/`Quantity` wrappers over 20 `Decimal(str(...))`.
Carrying `(float, float)` tuples and converting only at the API boundary is a ~3× win on its own
and is the single largest line item in the budget.

### 8.2 Storage

25,868 `data.parquet` files, 4.4 GB, 500 symbols. **`read()` has zero predicate pushdown** — it
loads every row group and column, then filters in pandas (`parquet_storage.py:282-308`). 1 symbol ×
1 month = 16.8 ms; 100 symbols × 1 month = 783 ms and 187 MB; **`BulkPrefetchMarketProvider` for a
1-year 500-symbol scan = ~12.3 GB resident and ~11 minutes of blocking I/O.** Fix is
`pq.read_table(path, filters=[...], columns=[...])` plus multiple row groups per file.

Writes are read-modify-write of the whole partition, **1 row group per file** (so no intra-file
pruning is possible), `use_dictionary=False`, **no compression** (22.5 B/row with `symbol`/`exchange`/
`kind`/`timeframe` stored as repeated strings inside every row despite being encoded in the Hive
path — ~30 B/row of pure waste), and `_write_atomic` has **no `fsync` on the temp file or the
directory**. A corrupt file propagates `ArrowInvalid` to every reader with **zero** handling repo-wide
(`grep ArrowInvalid` → 0 hits), so one truncated file makes a symbol permanently unreadable through
chart, scanner, backtest, feed recovery and replay.

### 8.3 Memory

The bus's `deque(maxlen=10_000)` log of `Quote` is **4,810 B/quote → 48.1 MB**, pinning complete
20-level order-book snapshots for a 1-second window at 10k ticks/s, and it exists only for a
`replay()` nothing on the live path calls. 500 symbols × 1 year of M1 bars as domain objects is
**45.4 GB** (985 B/candle, measured); as a DataFrame, 12.3 GB. There is **no eviction, no
compaction, and no tiering anywhere**.

### 8.4 WebSocket scaling — a hard stop, not a tuning problem

There is **exactly one market socket and one depth socket per broker**, and there is no sharding
anywhere. The cap is `max_stream_instruments`: Dhan 1000, Upstox **500**, enforced by
`_check_cap` → `ValueError`. **Nifty-500 is 100% of the Upstox cap on a single connection with zero
headroom** for a second universe, an index future, or a resubscribe race. The 501st symbol raises.
"10× more symbols" is an architectural stop.

There is also **no heartbeat or ping/pong anywhere** in either broker — `websockets.sync.connect` is
called with no `ping_interval`, and every receive loop is an unconditional blocking `ws.recv()`. A
TCP half-open socket blocks forever: no exception, no reconnect, no log. The market feed is
partially compensated by `FeedStaleMonitor`; **the order/portfolio stream has no such monitor**, and
neither backend resyncs (`_resubscribe` is an explicit no-op and nothing re-reads
`get_orderbook()`/`get_positions()` on reconnect, so every update during an outage is permanently
lost). Reconnect is capped at 10 attempts and then **gives up permanently** for the rest of the
session.

### 8.5 Bar generation — two correctness bugs

- **H1 bars are 30 minutes off the session grid.** `bucket_start` is a pure epoch floor
  (`bar_aggregator.py:78-80`): IST offset 19,800 s, and `19800 % 3600 == 1800`, so H1 buckets land
  on the half hour. [V] A 09:15 session opens an H1 bar labelled **08:30** holding 15 minutes of
  data, and the last bucket (`15:30..16:29`) additionally absorbs Dhan's phantom 16:00-20:00 bars.
  Any H1 strategy sees a shifted, contaminated series. Anchor buckets to the session open.
- **The 15:30 flat-close documented at `bar_aggregator.py:7-10` does not exist.** `grep` for
  `MARKET_CLOSE` in that file, `market_feed.py` and `stream.py` → **zero hits**. The 15:29 M1 bar
  is emitted as *forming* and **never closed**, for 18.5 hours. Every consumer gated on
  `frame.closed` never sees it; a 1-day chart is permanently one bar short.
- **A late tick is silently dropped and the bar is never revised.** `bar_aggregator.py:209-211`
  returns on `start < self._bucket.start`. A 14:59 tick arriving at 15:00:01 vanishes with no
  counter, and the already-emitted 14:59 bar keeps a permanently wrong high/low/close.
- **No sequence number exists**, so **a dropped frame is structurally indistinguishable from an
  illiquid minute.** `FeedIntegrityTracker` can prove a duplicate and a >60 s clock jump, never a
  loss. And `check_stale()` — the liveness signal that *would* detect it — **has no caller**:
  `grep -rn "check_stale()"` finds only the definition and a comment.

### 8.6 The benchmarks are measuring the wrong thing

`benchmarks/bench_tick_parse.py:23` uses `sample_frame = b"\x00" * 100  # Placeholder`. `data[0] == 0`
is not in `_MSG_TYPES` `{2,4,5,6,8,50}`, so the parser returns at the first length check. **It
reports 0.21 µs against a real 7.9 µs — a 38× understatement, published as a benchmark.**
`bench_bus_throughput.py` publishes `OrderPlaced(order=None)` (1.96 µs vs 11.7 µs for a real
quote). `bench_order_path.py` uses `PaperBroker` (28.9 µs, no network, no `ensure_token`).
`bench_parallel_fetcher.py` is the only honest one and it says so.

**There is no latency regression gate, no end-to-end tick→order→venue benchmark, and no load test
in the repository.** `benchmarks/` appears in exactly one place in all config — as a mypy
*exclusion*. Every performance defect above is invisible to CI, which is exactly the failure mode the
repo's own ethos ("a measurement written into prose is wrong the moment the next backfill lands")
was written to prevent.

### 8.7 The indicator push is a self-inflicted DoS

`stream_indicators.ts:206-220` recomputes the **full 400-bar window per push, per indicator, per
connection** and discards all but the last 2 points — and every closed bar invalidates the tail
cache and re-reads 30 days of parquet (16.8 ms) on the event loop. At 500 symbols × 1 Hz × 10
clients that is ~5 s of event-loop time per second, saturating `/ws/stream` and triggering the
fail-closed `WS_CLOSE_CONTROL_OVERFLOW` close. **The chart UI's own indicator feature degrades the
OMS feed.**

### 8.8 Bar/Hourly grid — what 10× actually requires

1. **Get the blocking venue call off the tape thread.** Non-negotiable, architectural. Until then no
   amount of decoder work helps, because the tape thread is the thing being blocked.
2. **Drop `Decimal` off the hot path** — carry floats, convert at the API boundary. ~3×.
3. **Native decode** — `struct.unpack_from` over a `memoryview`, timestamp as an `int` epoch, no
   dict building. ~2 µs.
4. **A real bounded queue between parse and compute** — bounded and known beats unbounded and
   unknown.
5. **Process split** — parse in N-1 processes (one GIL each) over a shared-memory ring; all domain
   construction in one consumer. The only way past ~1 core of CPython, and it also gives per-socket
   crash isolation.
6. **Only then** numpy for batch indicators — which means breaking the bit-exact-TS-golden
   constraint or keeping a dual path. **Do not do this first**; it is the smallest win and the most
   expensive to validate.

---

## 9. Frontend Review

**This is not a React app.** 6,747 lines of vanilla-DOM TypeScript across 20 files wrapping an
imperative canvas widget. No VDOM, no `useEffect` arrays, no reconciliation to memoize. **A state
machine library would be actively wrong here** — `xstate` for a 6.7k-line app with one refcounted
socket is ~15 kB and a new conceptual tax to fix four missing guard clauses. The honest direction
is **deletion, not abstraction**: −1,112 lines of dead code, −1 duplicate panel, −4 duplicate DOM
helpers, +1 exported `el()`.

**What is genuinely good** (better than most retail platforms, and it should be said plainly):
order entry is **disarmed by default**; the arm box is **disabled** until every gate passes;
`validateOrderIntent` is a **pure function** with a named-message table; indicator panes are blocked
(`paneIndex !== 0`); the confirmation string is **bound to the exact side, type, quantity, instrument
and price** and the E2E asserts the literal string; the arm drops on success and the E2E asserts the
box unchecks; there is a real live/paper indicator. The **single-store persistence claim is verified
in source** — `persist` appears only in doc comments, `localStorage` only in a test that *proves it is
ignored* — and restore-before-`createWidget` is genuinely race-free. The host imports only the
library's public entry points and never reaches into `dist/`. `strict` + `noUncheckedIndexedAccess`
are on, and live code has **zero `any`**.

| # | SEV | Finding | Symptom |
|---|---|---|---|
| F1 | **CRITICAL** | The live order path is unreachable: `main.ts:61` `getApiKey() === '' ? 'paper' : 'unknown'` [V] vs `order-safety.ts:39` accepting only `paper\|live\|replay` [V]. `controlsReady` is therefore false forever on a keyed server, and no endpoint reports the session mode | **A live trader cannot place, cancel, modify or exit from this UI.** The E2E asserts this fail-closed behaviour as *intended*, so CI will never catch it. |
| F2 | **CRITICAL** | Boot-timeout workspace overwrite: a >5 s workspace fetch yields `revisions: {}` → `revision: null` → the server skips its conflict check → **your saved layout is silently replaced by a blank chart** | `main.ts:36-56`, `workspace.ts:352,463-465`, `workspace_store.py:51-53` |
| F3 | **CRITICAL** | No double-submit protection: `armed=false` happens *after* the response, and each POST mints a **fresh** `crypto.randomUUID()`, defeating the backend's idempotency by construction | Two chart right-clicks inside a second = **two real orders** |
| F4 | HIGH | `ws.onclose = () => {}` **discards the CloseEvent**; 1008 (bad key), 1011 (no session) and 1013 (server's deliberate resync close) are all handled as a network drop | After a 10-attempt storm against a server that will never accept, the user sees "reconnect gave up". A rotated key and a dropped VPN are indistinguishable |
| F5 | HIGH | The stop-side check runs against the **last bar's close**, not the live quote, which the user is shown | On a 1m chart that is up to 60 s stale; a BUY STOP above the market is accepted |
| F6 | HIGH | `workspace.put()` has no `else` — 401/403/500 is **silently swallowed** and it retries every 2 s forever; no in-flight serialization, so two overlapping PUTs can write a stale revision back and manufacture a 409 that then **disables persistence for the session** | Workspace saves fail invisibly on a keyed server (the PUT overwrites `headers` rather than merging, so it is unauthenticated) |
| F7 | HIGH | No max-notional anywhere; `#qty` is a free-text integer with `min="1"` and no cap; the confirmation shows quantity but never notional | One right-click + `qty=100000` at ₹3,000 is a ₹3 crore order |
| F8 | HIGH | `exitPosition` in the account panel has **no confirmation and no notional**, unlike the chart path | A single click market-exits an entire position |
| F9 | HIGH | 1,112 LOC of dead TS (16.5%) — `shellbar.ts`, `chart-lifecycle.ts`, `chart-state.ts` (~40 `any`s, 4 `Proxy` traps), `chart-types.ts`, `ladder.ts`, `trade-bar.ts` (empty body) | Vite tree-shakes them so nothing ships, but `tsconfig` includes `src`, so a CI gate type-checks them — and their `any` count is a pure liability next to live code's zero |
| F10 | HIGH | Volume series rebuilt **twice** per data event, O(3,000 bars) each; `order-safety.render()` writes 9 DOM fields unconditionally on every depth frame | ~500 invalidating writes/second into an `aria-live` region — screen-reader flood |
| F11 | HIGH | `trades-panel.ts:250` says "the list is capped". It is not | A 5,000-fill run builds ~45,000 DOM nodes |
| F12 | MEDIUM | No global error boundary, and `main.ts` is a top-level-`await` module | Anything after line 54 throws → **blank page, console-only error** |
| F13 | MEDIUM | "No data" is indistinguishable from "failed": `NotFoundError` is never thrown, and a "no datalake data" backtest renders as "No backtest run yet — add a Backtest Equity pane above" | The user *did* add the pane and *did* run it |
| F14 | MEDIUM | Hand-written backend contract types with **no generation from OpenAPI** | Rename a Pydantic field and the frontend keeps compiling and then shows `—`. This is the highest-leverage MEDIUM in the frontend. |
| F15 | MEDIUM | No linter, no formatter, and `noUnusedLocals`/`noUnusedParameters` are off | F9 went unnoticed |
| F16 | MEDIUM | `CLAUDE.md:266` references `frontend/src/backend-indicators.ts` — **that file does not exist**; the real one is `tier2.ts`. And the third indicator hand-rolls its descriptor, so it gets **no `signal` (no cancellation) and no `refetchOn`** | Every agent is pointed at a phantom; CLAUDE.md's "new indicators appear automatically" is true for two of three |

**Testing:** 0 unit tests (devDependencies are `typescript` and `vite`; Playwright resolves from the
root). 0 a11y assertions. 0 visual regression. Chromium only. And the prior panel's claim is
**confirmed**: `e2e/order-safety.spec.ts:8-33` reaches the chart's order menu via
`page.mouse.click` at **computed pixel fractions** with hand-tuned weights — the pane-discrimination
assertion depends entirely on that arithmetic staying true. Change the dock height and it passes for
the wrong reason.

The five pure functions that most need unit tests are `validateOrderIntent` (the order-safety gate,
today reachable only by driving a browser to a canvas pixel), `pairRoundTrips`, the three
`workspace.ts` boot deciders, and `replay.ts`'s frame validators.

---

## 10. Repository Organization

### 10.1 Proposed target

The strangler migration should be **finished, not reversed** — production is already 100% migrated
and only 5 real files remain. But 18 packages is too many for one single-process system.

```
v4/
├── pyproject.toml              # one config: workspace, ruff, mypy, pytest
├── CLAUDE.md
├── docs/
│   ├── README.md               # MISSING — add an index
│   ├── design/YYYY-MM-DD-*.md  # existing convention, keep
│   ├── operations/runbooks/    # existing, keep
│   └── audits/YYYY-MM/         # the 3 root .md reviews + ARCHITECTURAL-AUDIT move here
├── src/
│   ├── tradex_domain/          # pure types, ZERO internal deps. KEEP EXACTLY AS IS.
│   ├── tradex_core/            # execution + application + reactive + observability
│   ├── tradex_ingest/          # brokers + market_data  (kills market_data→brokers)
│   └── tradex_app/             # runtime + interfaces + strategy + replay
├── services/duckdb-analytics/  # keep separate — different audience
├── scripts/                    # all lint+type gated
├── benchmarks/                 # KEEP, but add to ruff (perf code has bugs too)
├── frontend/
├── data/ runtime/ logs/        # gitignored
└── apps/ poc/                  # DELETE — out of a trading engine repo
```

**Justifications:** 18→4 packages, because the system's real seams are *pure types / money path /
infrastructure*; `market_data→brokers` becomes intra-package; `interfaces`' 11-package fan-in
becomes one import; `operations` and `persistence` are 41 and 19 LOC with zero production importers.
Delete `trading/` entirely once the 53,800 test lines move to `<pkg>/tests/`.

### 10.2 Repo hygiene

- **The three root `trading-*.md` reviews (80 KB) should not be at the root.** They are a
  three-generation cascade where the newest **supersedes** the other two, both pinned to `df377a0`
  while HEAD is 9 commits later, and their LOC figures are stale. They contain the highest-severity
  finding in this entire audit (26 of 42 mutating HTTP operations unauthenticated — since partly
  fixed by `e1c23e6`) and that is recorded nowhere else. Move to `docs/audits/2026-09/` with a
  5-line index stating which findings are open.
- **30 of 66 `docs/` files (45%) are `docs/superpowers/`** — agent-planning artifacts that read as
  project documentation.
- `CLAUDE.md` is excellent and unusually honest, but it is now **wrong in at least two places**:
  it names a frontend file that does not exist, and it says the backtest does not simulate a
  protective exit when it does. Documentation drift on a safety guide is worse than no guide.
- `.kilo/worktrees/sour-ragdoll/` is a **6.0 MB, 403-file duplicate of the whole tree**, gitignored
  and excluded from ruff — a live hazard for any future naive grep. Delete it.

---

## 11. Production Readiness Scorecard

| Area | Score | One-line justification |
|---|:-:|---|
| **Architecture** | **6/10** | Correct inward dependency direction and an excellent boundary test; but 18 packages over-decomposed, a half-finished 133-file shim tree, a real `runtime↔trading` ring the guard cannot see, no read model, no broker/account dimension in the OMS. |
| **Quant Design** | **3/10** | No position-sizing model (it is `abs(strength)` = 1 share), no leverage/margin model, no per-strategy budgets or attribution, no corporate actions in live, fees off by default, every risk limit defaulting to `None`. |
| **Code Quality** | **5/10** | Excellent type discipline and money-type hygiene in `domain`/`execution`; offset by a 1,467-line/310-branch function, a 94% shim package, 10 `assert`s guarding wire data, and 189 broad excepts. |
| **Testing** | **5/10** | 4,341 tests and a genuinely sophisticated culture for the problems it *noticed*; **zero branch coverage**, 13 property tests, 0 load tests, and package-level test ownership that is fiction for 7 packages. |
| **Reliability** | **4/10** | Genuinely good fail-closed design and one excellent SPOF mitigation (`SingleWriterLock`); no alerting, dead structured logging, health checks that only prove liveness, no periodic reconciliation, a feed that never reaches READY on a clean boot. |
| **Scalability** | **3/10** | 500-symbol single-socket hard stop; 22.7k ticks/s measured ceiling; the venue call on the tape thread; 45 GB for one year of history; no predicate pushdown. Multi-broker is blocked by a data-integrity bug, not an inconvenience. |
| **Security** | **3/10** | The money credential is served unauthenticated on `/`; `/docs` is live; the live order UI path is dead; the token file is world-readable; `pickle.loads` on the audit log; **zero** dependency scanning. |
| **Performance** | **3/10** | Fully measured, and the measurements are the problem. The one benchmark that covers the hot path measures a placeholder and is wrong by 38×. No latency gate exists. |
| **Maintainability** | **5/10** | Honest comments and a good design-doc convention; but 516 test import sites through a shim façade, 1,652 test files and 3 `conftest.py`, 80 KB of self-superseding reviews, and CLAUDE.md now wrong in two places. |
| **Operational Readiness** | **3/10** | No alerting, no runbooks for 7 of 13 failure modes, no deploy artifacts, no CD path, no on-call, a lint gate that is **red right now**, and a service that is stateful with no migration story. |

### Overall

| Deployment mode | Score | Verdict |
|---|:-:|---|
| **Paper trading** | **7/10** | Usable now. Fix the workspace-overwrite bug (F2) and the double-submit (F3) before anyone trades on it meaningfully. |
| **Live money, one strategy, one broker** | **3/10** | Not deployable. The risk layer does not constrain anything, sizing is one share, corporate actions are unhandled, and the UI cannot place a live order. |
| **Live money, multiple strategies or brokers** | **2/10** | Architecturally blocked: no per-strategy kill switch, no per-strategy budgets, no capital allocation, no P&L attribution, and an OMS with no broker/account dimension that would silently merge two books. |

**Production Readiness Score: 3.5 / 10 for live capital.** The score is gated by its floor, not its
mean — and the floor is the risk and security layers, which are written and tested but not wired to
constrain anything.

---

## 12. Top 20 Risks

| # | Risk | Sev | Cost if it happens |
|---|---|---|---|
| 1 | **Sizing is `abs(strength)` = 1 share, unbounded, unclamped, with all risk limits `None`** | CRITICAL | Every strategy trades one share today; a `strength = 3σ` signal triples position silently. No per-order, per-position, per-day or rate limit exists. |
| 2 | **No way to stop one strategy** — `unregister()` doesn't dispose, `pause()` is a no-op | CRITICAL | A runaway strategy cannot be halted without killing the process and every other strategy. |
| 3 | **Every backtest number is invalid** (2 look-aheads, a swallowed cash debit, no risk/slippage/corporate actions, meaningless Sharpe at 1-share sizing) | CRITICAL | Strategies are selected on numbers that are wrong in the flattering direction. This is how real money gets lost before any code runs. |
| 4 | **Partial fills booked at cumulative average price** (`live_fill_bridge.py:205-212`) | CRITICAL | Cost basis and every subsequent realised P&L on the position are wrong. |
| 5 | **Fees off by default in live; fee model hardcoded to intraday** | CRITICAL | Realised P&L is gross; buying power overstated ~0.15%/side; delivery costs understated ~5×. |
| 6 | **No corporate-action handling in live** | CRITICAL | A 2:1 split produces a phantom −50% drawdown spike, a permanently wrong basis, and a −50% realised loss that never happened. |
| 7 | **Venue order submit runs on the WebSocket receive thread** | CRITICAL | One 250 ms order at the open on 500 symbols = 5,000-15,000 ticks of added queueing delay, and bars look fine while the tape is stale. |
| 8 | **`CashLedger` unlocked; `require_debit` and the debits are three separate critical sections** | CRITICAL | Concurrent submissions both pass the affordability check and both debit. The one structure that decides solvency is the one that was missed. |
| 9 | **The global bus lock is held across broker HTTP** | CRITICAL | One slow order submission stalls market-data publication, hence all mark-to-market, all fresh-mark risk checks and all UI ticks. |
| 10 | **Live order UI path is unreachable; API key served unauthenticated on `/`** | CRITICAL | Cannot trade from the UI; and the money credential is one `curl` away, bypassing every auth control. |
| 11 | **No reconciliation after startup** — a lost fill, portal order or auto-square-off is invisible for the session | HIGH | The strategy sizes against a book that no longer matches the venue. |
| 12 | **The feed never reaches READY on a clean cold boot** (needs verification) | HIGH | Every order is rejected with `feed_not_ready` until an unrelated reconnect triggers recovery. |
| 13 | **H1 bars are 30 minutes off the session grid; the 15:29 bar never closes** | HIGH | Any H1 strategy trades a shifted, contaminated series; a 1-day chart is permanently one bar short. |
| 14 | **No heartbeat anywhere; the order/portfolio stream never resyncs; reconnect gives up permanently** | HIGH | A half-open socket is undetected forever, and every order update during an outage is lost with no recovery. |
| 15 | **Idempotency keys are client-supplied with no namespace and are never released on failure** | HIGH | A guessed key pre-reserves it and the legitimate order is **never placed**, permanently. |
| 16 | **No alerting exists; structured logging is dead code; `/health` is liveness-only** | HIGH | A feed can sit `DEGRADED` for hours and nobody is told. The instrument that would tell them exists and is correct. |
| 17 | **500-symbol single-socket cap; 22.7k ticks/s ceiling; 45 GB for 1 year of history** | HIGH | The 10× growth target is a hard architectural stop, not a tuning problem. |
| 18 | **`pickle.loads` on the audit log; the log is not tamper-evident** | HIGH | RCE plus the ability to conceal activity, against a compliance artifact, as the process holding live tokens. |
| 19 | **Position and order books diverge on an overfill; `UNKNOWN` orders are never resolved** | MEDIUM | The position is permanently overstated vs the OMS and the broker, and the next startup trips the kill switch. |
| 20 | **Zero dependency scanning; all GitHub Actions pinned to floating tags** | MEDIUM | An unpatched transitive CVE is directly exploitable given #18 and the query-string credential. |

---

## 13. Top 20 Improvements

| # | Improvement | Sev | Size |
|---|---|---|---|
| 1 | Add `Signal.quantity` + a `PositionSizer`; clamp lot size, tick size, max notional in `RiskManager` | CRITICAL | M |
| 2 | Make `unregister()` dispose; gate strategy delivery on the lifecycle registry | CRITICAL | S |
| 3 | Fix the two backtest look-aheads and the swallowed cash debit; give the chart route a risk manager, slippage and corporate actions | CRITICAL | M |
| 4 | Book one `Fill` per venue trade from the trade book, not a synthetic delta at cumulative average | CRITICAL | S |
| 5 | `fees_enabled=True` by default in live/paper; add `product_type` to `Fill`; make the rate table product- and exchange-keyed | CRITICAL | M |
| 6 | Add live corporate-action handling (or a scheduled split/dividend check before the open) | CRITICAL | L |
| 7 | Move `self._fill.submit()` off the WS receive thread onto a dedicated single-threaded executor; split bus `publish` (enqueue) from `dispatch` | CRITICAL | M |
| 8 | Lock `CashLedger` and make check-and-debit one atomic operation; route the engine through the existing `fill_with_remark` | CRITICAL | S |
| 9 | Delete the API-key injection at `/`; require a real login; disable `/docs` in production; add `require_auth` to the two workspace mutations | CRITICAL | S |
| 10 | Expose `mode` from `/api/charts/account` and drive `tradingMode` from it, with a `submitting` flag and one idempotency key per arming | CRITICAL | S |
| 11 | Add a `ReconciliationMonitor` on the `FeedStaleMonitor` pattern: periodic order/position/funds diff with drift metrics | HIGH | M |
| 12 | Commit ~50 lines of Prometheus rules implementing the thresholds `metrics-catalog.md` already specifies; call `setup_structured_logging` in `cmd_serve` | HIGH | S |
| 13 | Fix the `RiskConfig` defaults to fail closed — refuse to boot live if `max_order_value`/`max_position_value` is `None` | HIGH | S |
| 14 | Anchor `bucket_start` to the session open; implement the 15:30 flat close; add a late-tick re-open window | HIGH | S |
| 15 | Add `ping_interval`/`ping_timeout` + a frame-age watchdog; resync `get_orderbook()`+`get_positions()` on order-stream reconnect; slow retry after exhaustion | HIGH | M |
| 16 | Namespace idempotency keys by principal, add a lease/TTL so a crashed reservation can be stolen, and cap `_completed` | HIGH | S |
| 17 | Enable `--cov-branch` + a per-package coverage floor; extract `ALLOWED_TRANSITIONS` and write the exhaustive FSM test; add hypothesis properties for fees, idempotency and PnL conservation | HIGH | M |
| 18 | `require_success` inside `ProviderHttpClient.request` so every broker response is validated once — removes Upstox's silent `Price(0)` and Dhan's empty-positions-on-error, for all future brokers | HIGH | M |
| 19 | Fix the workspace boot-timeout overwrite (a `degraded` flag that suppresses the first write) and serialise `put()` | HIGH | S |
| 20 | Add `["C90","PLR09"]` to ruff, a per-package mypy pass, `bandit`+`gitleaks`+`pip-audit`, and branch-coverage + latency gates | MEDIUM | S |

---

## 14. Quick Wins (1-2 days)

| # | Action | File |
|---|---|---|
| Q1 | `chmod 600 .env.local runtime/dhan/token_state.json`; assert 0600 at boot, fail closed in live | `runtime/src/tradex_runtime/live.py` |
| Q2 | Add `max_order_value` / `max_position_value` to the live config with real values; refuse to boot live without them | `config/src/tradex_config/schema.py:35-41` |
| Q3 | Flip `fees_enabled` to `True` for live/paper | `config/src/tradex_config/schema.py:112` |
| Q4 | Make `StrategyEngine.unregister()` dispose the strategy's subscriptions | `strategy/src/tradex_strategy/core/engine.py:367-373` |
| Q5 | Add `threading.Lock` to `CashLedger`; collapse `require_debit` + debit into one `apply_fill` | `execution/src/tradex_execution/cash_ledger.py` |
| Q6 | Remove the `GET /` API-key injection and the `apikey.ts` scrape; disable `/docs`/`/redoc`/`/openapi.json` in production | `fastapi_app.py:134-138,388-395`, `apikey.ts:15-27` |
| Q7 | Add `require_auth` to `PUT`/`DELETE /workspace/{id}` | `interfaces/routes/chart.py:971,988` |
| Q8 | Gate `/metrics` behind auth; alert on `master_lifecycle` refresh errors and `engine._append` failures | `fastapi_app.py:412`, `startup.py:963-980` |
| Q9 | Anchor `bucket_start` to 09:15 (fixes H1/30m/15m); add the 15:30 flat-close flush | `runtime/bar_aggregator.py:78-80,191-242` |
| Q10 | `validate` every response in `UpstoxBroker.ltp`/`DhanBroker.get_positions`/`get_account` via `require_success` | `upstox/_marketdata.py:78-88`, `dhan/_portfolio.py:112-121,29-47` |
| Q11 | Fix `benchmarks/bench_tick_parse.py` to use a real frame, with a negative-control assertion | `benchmarks/bench_tick_parse.py:23` |
| Q12 | Add ruff `["C90","PLR09"]`, set `.ruff_baseline` to ~20, fix the 13 `I001`, and add `--cov-branch` | `pyproject.toml`, `.ruff_baseline` |
| Q13 | Call `setup_structured_logging("tradex", json_format=True)` in `cmd_serve` | `interfaces/cli.py:440` |
| Q14 | Delete `envelopes.py` (433), the 6 dead frontend files (1,112), `operations/`, `persistence/`, `http_response.py`, `.kilo/` | multiple |
| Q15 | Add `submitting` flag + one idempotency key per arming + `AbortSignal.timeout` to `placeOrder` | `frontend/src/main.ts:112-179` |
| Q16 | `ping_interval`/`ping_timeout` on both `websockets.sync.connect` calls | `dhan/ws_streams.py:56`, `upstox/ws_streams.py:49` |
| Q17 | Add `mode` to `GET /api/charts/account`; drive `tradingMode` from it | `chart.py:617`, `main.ts:61` |
| Q18 | Pin all GitHub Actions to 40-char SHAs; add `gitleaks` to pre-commit | `.github/workflows/*` |

---

## 15. Medium-Term (1-4 weeks)

| # | Action | Outcome |
|---|---|---|
| M1 | **Split bus publish/dispatch**; one dedicated pipeline thread; `engine.submit()` publishes a command and awaits a future | Removes the global lock, the event-loop block, and the dual-door race in one change |
| M2 | **Broker account dimension**: `stream_id = f"{broker_id}:{account_id}:orders"`, key the cache by `(account_id, order_id)`, partition the fill-dedup LRU | Unblocks multi-broker from a data-integrity standpoint |
| M3 | **Strategy dimension**: `strategy_id` on `Fill`/`Position`, per-strategy `RiskBudget` wired through config (fix the `tag` lookup), P&L attribution | Unblocks multi-strategy |
| M4 | **BrokerRegistry**: collapse the ~30 literal `"dhan"`/`"upstox"` branches in `live.py`, `startup.py`, `parallel_fetcher.py`, `cli.py` | A third broker becomes additive |
| M5 | **`ReconciliationMonitor`**: 60 s order/position/funds diff with drift metrics and a HIGH-severity trip; make position-qty drift HIGH (it is currently LOW) | Closes the "invisible for the session" hole |
| M6 | **WebSocket sharding** (`ShardedStreamPool`, N sockets, stable hash assignment) + a frame-age watchdog + order-stream resync | Lifts the 500-symbol hard stop |
| M7 | **Parquet predicate pushdown** + multiple row groups per file + `filters=` on read + compression + dictionary encoding + a corrupt-file quarantine | 1 symbol-month 16.8 ms → ~2 ms; ~40% smaller lake |
| M8 | **Indicator push**: process-level tail cache keyed `(instrument, timeframe)`, append instead of re-read, incremental warm-start, only compute the new bar | Removes the self-inflicted event-loop DoS |
| M9 | **Extract `ALLOWED_TRANSITIONS`** into `domain`; route every status write through `OrderManager`; make `Order.status` non-public | Makes the FSM testable and enforced |
| M10 | **Snapshot + retention for the event store** (fold-and-persist every N events; prune with an archived tail) + a migration test + a registered legacy decoder | Boot time stops growing without bound; deploys stop bricking history |
| M11 | **Move order submission off the fill-source call path** into a dedicated executor with a bounded queue; add an overall per-operation deadline | Decouples the tape from the venue |
| M12 | **PIT-correct backtest**: populate `delisted`, call `assert_historical_universe`/`assert_no_future_candle` from the engine, carry warmup between WFO windows, thread `frequency` into `_aggregate`, add an embargo | Makes OOS numbers mean something |
| M13 | **Finish or kill the shim tree**: move `trading/tests/**` into `<pkg>/tests/`, repoint 516 import sites, make the boundary test parse `import_module` strings | The dominant structural cost, removed |
| M14 | **Position sizing + margin model**: `MarginModel` with per-`(product, segment)` leverage; gate on `required_margin(order) <= buying_power`; anchor the ledger to settled cash; `max_gross_short_value` | The missing risk layer |
| M15 | **Alerts, runbooks, deploy artifacts**: Prometheus rules, Alertmanager routing, a `deploy/` with a unit file + probe block, and the 7 missing runbooks | Operational readiness |
| M16 | **Frontend**: vitest for the four pure modules, `axe` + keyboard-only order path, generate `api-types.ts` from OpenAPI, add ESLint, window the trades table | |

---

## 16. Long-Term Strategic (1-6 months)

| # | Action | Rationale |
|---|---|---|
| L1 | **Snapshot the whole tape.** A `FeedEventStore` (or a per-socket append-only frame log) enabling point-in-time replay of live sessions | Today the durable log is **order events only** — the `SQLEventLog` exists but nothing passes it. This is the single highest-leverage structural investment: it makes post-incident forensics, strategy research on live conditions, and true backtest/live parity all possible. It also closes the "I don't know why this happened" gap, which `envelopes.py` was designed for and never delivered. |
| L2 | **Multi-process architecture** — parse per socket in separate processes over a shared-memory ring, compute in one consumer | The only way past ~1 core of CPython, and it also gives per-socket crash isolation. Do this *after* L1's store exists, because the frame log is the hand-off. |
| L3 | **Separate read and write models.** A projector that folds the event log into denormalised views for the UI, scanners and analytics | Every UI read currently hits the in-memory write model. Required before multi-strategy/multi-account. |
| L4 | **Consolidate 18 packages to 4** (`domain`, `core`, `ingest`, `app`) after M13 | Reduce 17 `pyproject.toml` files, 17 mypy blocks and a 795-line boundary test to something proportionate to three real decisions. |
| L5 | **Formal risk engine**: pre-trade (price band, lot, duplicate window, self-trade, per-order/position/sector caps), intraday (drawdown, auto-square-off at 15:20, product-type buckets), post-trade. Plus a pre-open checklist gate. | The platform has a `RiskManager` with good primitives and no policy. This is where a real fund's risk actually lives. |
| L6 | **Broker framework, not adapters.** Move `require_success` into the shared HTTP client, make `submit_mutation` mandatory for all mutations, extract a `BaseVendorAdapter` and a `WsStreamBase`, and split the 40-method `BrokerAdapter` Protocol into `HistoryPort`/`QuotePort`/`OrderPort`/`PortfolioPort`/`StreamPort` | ~500 lines of copy-paste per new broker, and ~0 new correctness — the invariants must live where a new broker is *forced* to touch them. |
| L7 | **Tamper-evident audit**: HMAC hash chain over the event log, a daily signed digest shipped off-host, and a verified incident-replay bundle procedure | Regulatory requirement for real capital, and it makes "I lost money" answerable. |
| L8 | **Full quant research stack**: purge/embargo, combinatorial purged CV, Deflated Sharpe / Probability of Backtest Overfitting, White's Reality Check, parameter-stability surfaces, and a minimum-sample-size assertion on every reported metric | The optimiser currently takes the argmax of a grid with no multiple-testing control, and reports it as an out-of-sample result. |
| L9 | **Latency SLOs and a capacity model** as a gate: tick→bar, bar→signal, signal→venue p50/p99 with a committed baseline and a >10% CI failure | The single benchmark covering the hot path is wrong by 38×, and nothing measures the end-to-end path at all. |
| L10 | **Fill the missing trading concepts**: pre-open auction, square-off/auto-close, MTF, basket/split orders, GTT, OCO/trailing, circuit-limit and halt handling, dividend/split/rights in live, multi-account and sub-account routing | Without these the platform is a single-strategy intraday cash system, whatever the architecture says. |

---

## 17. The One-Paragraph Verdict

This codebase is what a very good engineer builds when they care more about being right than
shipping. The layering, the type discipline, the money arithmetic, the idempotency design, the
three-state UNKNOWN policy, the golden-fixture provenance gate and the stale-bundle E2E guard are
all better than most production trading systems. The problem is not quality — it is that the
*constraints* live in the wrong place. Sizing is an accident of a signal-confidence field. Risk
limits are configuration nobody set. The strategy kill switch does not exist. Backtest fills are
optimistic in two independent ways and the UI cannot place a live order at all. The risk layer is
written, tested, and wired so that it constrains nothing.

The fastest path to production is not more engineering. It is: pick one broker, one strategy, one
account; make sizing explicit and clamped; make every risk limit non-`None` and fail the boot if it
is; fix the two backtest look-aheads; and get the venue call off the market-data thread. That is
roughly two weeks of work and it converts this from a research platform into something that can be
trusted with one strategy's capital. Everything in the long-term section can wait until then.
