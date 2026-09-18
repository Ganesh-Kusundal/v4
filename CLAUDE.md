# TradeX v4 — Agent Guide

---

## Standard Interfaces — USE THESE, DON'T REINVENT

### Broker Operations (Auth, History, Quotes, Orders)

**NEVER** investigate broker token generation, TOTP logic, or auth internals unless explicitly asked. Always use the standard interface:

```python
# 1. Load credentials
from tradex_trading.config.env import load_env_file
load_env_file('.env.local')

# 2. Build broker (auto-handles TOTP auth, token refresh, instrument registry)
from tradex_trading.runtime.live import build_broker_from_env
broker = build_broker_from_env('dhan')  # or 'upstox'
broker.connect()

# 3. Use broker
from tradex_domain import Equity, Timeframe
from datetime import datetime, timedelta, UTC
inst = Equity.of('NSE', 'RELIANCE')
series = broker.history(inst, Timeframe.M1, start, end)
quote = broker.ltp(inst)

# 4. Cleanup
broker.close()
```

**Alternative: Full session boot** (includes strategy engine, bus, etc.)
```python
from tradex_trading.config.schema import AppConfig
from tradex_trading.runtime.startup import boot
from tradex_domain import BrokerId

cfg = AppConfig(broker_id=BrokerId.DHAN, mode='live', live_enabled=True)
session = boot(cfg)
# session.market.history(...), session.market.ltp(...), etc.
session.stop()
```

**Key points:**
- `build_broker_from_env()` handles ALL auth: TOTP generation, token minting, token persistence, refresh
- Credentials come from `.env.local` (DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, DHAN_PIN, DHAN_TOTP_SECRET, etc.)
- Timestamps from broker are UTC tz-aware — convert to IST before storing: `ts.astimezone(IST).replace(tzinfo=None)`
- Dhan `/charts/intraday` serves up to 90 days per request; `ParallelHistoryFetcher` guards intraday timeframes (M1/M5/M15/H1) to ranges ≤ 90 days and raises `SDKError` beyond that (fail-loud, no silent truncation)
- Dhan returns phantom post-market bars (16:00-20:00) — filter to 9:15-15:30 IST

### Datalake Operations (Parquet Storage, Backfill, Sync)

```python
# Read from datalake
from tradex_trading.datalake.parquet_storage import ParquetStorage
store = ParquetStorage('data/')
df = store.read(symbols=['RELIANCE'], start=datetime(2026, 5, 1), end=datetime(2026, 8, 1))

# List available symbols
symbols = store.symbols()  # ['360ONE', '3MINDIA', ...]

# Check date range
store.date_range('RELIANCE')  # (min_timestamp, max_timestamp)
```

**Backfill/sync script** (handles auth, fetch, IST conversion, upsert):
```bash
# Sync missing days for Nifty 500
python trading/scripts/backfill_parquet.py \
  --universe nifty500 --timeframe 1m --months 1 \
  --broker dhan --skip-existing

# Dry run (no API calls)
python trading/scripts/backfill_parquet.py --dry-run --limit 5
```

**Clean phantom data** (strip post-market bars):
```bash
python trading/scripts/clean_datalake.py --data-root data/
```

**Gap detection: two things are not gaps, and one of them used to be.**
`GapDetector.scan()` classifies instead of discarding — `gaps` holds actionable
interior ranges, `edge_only` / `open_missing_symbols` / `close_missing_symbols`
count session-edge shortfalls, and `sub_threshold_stamps` counts what
`min_gap_stamps` dropped. `detect()` is `scan().gaps`.

- **Session-edge bars are classified.** The grid's first/last stamp per session
  (09:15 open, 15:30 close) cannot be fetched reliably as a side effect of a
  session request, so a symbol differing only by one of them is a fetch-shape
  finding, not a hole — folding them into `gaps` is what made every symbol look
  gapped, which is why the `min_gap_stamps` floor exists. `fill_gaps
  --include-open-stamps` asks for them explicitly when they are worth a repair
  (they are real missing bars, and both brokers can serve the open one).
- **The floor hides holes; it no longer hides that it hid them.** The 2026-08-31
  cohort was short 14 tail stamps against a floor of 15: a real hole that every
  run reported as "lake is clean". Counted now, and `--min-gap-stamps` is a flag.
- **`--tail-days 0` for repairs.** The default scan window is each symbol's last
  7 days (a daily top-up), so a hole three weeks back is never even detected.

**Repairing a backlog.** `python trading/scripts/repair_gaps.py --budget 480`
drives a priority-ordered repair over the 90-day window (interior holes worst
first, then whole missing sessions, then 09:15 opens), exiting cleanly between
chunks so it can be rerun until the scan reports nothing left. Use it rather
than a single long `fill_gaps.py` run: that one is resumable in principle but a
whole-job foreground run outlives the shell that started it, and it orders
clusters by size, which puts a whole missing session ahead of a partial hole.
It takes `--skip-symbols` for tickers no configured broker can serve.

**Contested trading symbols — one symbol, two securities.** A vendor master can
list materially different securities under a single trading symbol, and the
domain keys cash equities by *trading symbol*, so both rows collapse onto one
`InstrumentId`. `register_authoritative` is last-wins, so the provider key used
to belong to whichever row the master happened to print last. Both configured
brokers list the same four: `CHOLAFIN`, `MOTHERSON`, `ELECTCAST` (equity vs a
debenture/warrant of the same issuer) and `IMC1` (three bond series). Upstox
lists the `CHOLAFIN` debenture `NSE_EQ|INE121A08PJ0` (series `D1`) after the
equity `NSE_EQ|INE121A01024` (series `EQ`), so every history request for the
equity resolved to a debenture that does not trade and came back **empty** —
indistinguishable downstream from a broker with no data for the symbol, which
is exactly how it was misdiagnosed for weeks. `BaseBroker.load_instruments` now
registers rows weakest-claim-first, so the strongest claimant owns the key
whatever order the vendor used, and every contested symbol is logged with both
keys and the winner. Dhan is ranked on `SEM_SERIES` (now normalized as
`series`), Upstox on `instrument_type` (`_row_claim_priority`; cash-equity
series `EQ`/`BE` outrank other series in the same segment). `CHOLAFIN` was the
only one of the four resolving wrongly on Upstox; on Dhan and for the other two
symbols the right row won by luck of row order, which a daily master refresh can
flip. So: **a symbol returning an empty series is usually resolving to the wrong
security, not unservable** — check the resolution (and the loader's contested
symbol warning in the run log) before reaching for `--skip-symbols`.

**Venue ceilings that shape both of the above** (measured 2026-09-16, not read
from docs): Dhan `/charts/intraday` treats `fromDate` as exclusive (a request
starting exactly at 09:15 loses the 09:15 bar) and its NSE 1m series **stops at
15:14** — the last 15 minutes of every session are simply not on the endpoint,
whatever window is asked for. Upstox serves the full 09:15–15:29. Both adapters
widen an intraday request to the whole day, so a tail-only gap fetch reaches
Dhan as a full-session request and comes back with a day of candles, none of
them the ones requested. `ParallelHistoryFetcher` therefore checks a response
against the window it asked for (`_clipped_tail`) and fetches an uncovered tail
from another broker, merging it over the bars already in hand
(`_merge_candles`) — a non-empty response is not evidence of a complete one, and
that missing check is what let the same 15-minute hole survive every fill run.

### Universe Loading

```python
from tradex_trading.datalake.universe import load_universe, available_universes

# Load Nifty constituents as Equity instruments
instruments = load_universe('nifty500')  # returns list[Equity]
# [Equity('NSE:360ONE'), Equity('NSE:3MINDIA'), ...]

# Available universes: nifty50, nifty100, nifty200, nifty500
available_universes()  # ['nifty100', 'nifty200', 'nifty50', 'nifty500']
```

CSV files are in `Dependencies/nifty{50,100,200,500}_list.csv`.

### Parallel History Fetcher (Multi-Broker)

```python
from tradex_trading.datalake.parallel_fetcher import ParallelHistoryFetcher

# Instruments split across ALL configured brokers; long intraday ranges are
# auto-chunked (Dhan caps one poll at 90d intraday, Upstox at 30d minute)
fetcher = ParallelHistoryFetcher({'dhan': dhan_broker, 'upstox': upstox_broker})
results = fetcher.fetch(instruments, Timeframe.M1, start, end)
# Returns dict[str, HistoricalSeries] keyed by instrument_id
```

### Chart UI (openalgo-charts frontend)

`frontend/` depends on `openalgo-charts: file:../openalgo-charts`, and that clone is
**gitignored** — it is an independent upstream repo, not vendored source. So a fresh
checkout must create it first, at the pinned commit, or `npm install` resolves a
missing directory. The host imports the library's public exports only and never
patches it, so the pin is the single knob controlling the UI.

```bash
# 1. The chart library, at the pinned upstream commit (public repo).
#    dist/ is what the host consumes, and it is NOT committed upstream — build it.
#    The pin lives in openalgo-charts.pin (one file): this step, the frontend build
#    stamp, the golden generators and the pytest provenance gate all read it.
git clone https://github.com/marketcalls/openalgo-charts.git openalgo-charts
cd openalgo-charts && git checkout "$(sed -e 's/#.*//' -e '/^\s*$/d' ../openalgo-charts.pin | head -1 | awk '{print $1}')"
npm install && npm run build

# 2. The host (output: frontend/dist/, served by FastAPI at /ui/)
cd ../frontend && npm install && npm run typecheck && npm run build

# 3. Serve everything (REST + WS + UI) — then open http://127.0.0.1:8000/ui/
cd .. && python -m tradex_trading.interface.cli serve --broker paper --port 8000

# 4. E2E (Playwright, root devDependency)
cd frontend && npm run e2e     # builds first, then serves on :8123; E2E_PORT to move it
```

`npm run e2e` **builds before it tests**, and that is load-bearing rather than
convenience: the suite serves `frontend/dist/`, so without the build it asserts
against whatever bundle was last built — a negative control (deliberately broken
source, expecting a failure) passed here once for exactly that reason.

**The E2E run is self-contained.** `data/ohlcv/` is gitignored, so CI and a fresh
clone have no market data and the chart would serve zero bars — every browser test
would then pass against *"no bars for this range"* without exercising anything.
`webServer` therefore runs `trading/scripts/seed_e2e_datalake.py` before `serve`.
The seeder uses the production writer, is **idempotent**, and exits without
writing when the symbol already has bars, so a developer with the real lake is
unaffected. Overrides: `E2E_PORT`, `E2E_PYTHON`, `E2E_STATE_DIR`.

It never reuses a running server: a regression suite must not depend on another
process's state, and the workspace blob is the single source of chart state (so a
dev server's database would decide which layout the tests boot into). It runs on
its own port with its own workspace database in temp state, and leaves no files
in the checkout. CI (`frontend-gate.yml`) runs this suite; locally it is opt-in.

**`TRADEX_DATALAKE_ROOT`** points the datalake somewhere other than `<repo>/data`
(`datalake/paths.py`, default unchanged). Use it to run a server against a
fixture: seed a scratch root, serve with the override, and the real lake is never
touched.

**`dist/` goes stale silently — rebuild after every pull.** Upstream tracks zero
`dist/` files and gitignores the directory, but `package.json` resolves
`module`/`types`/`exports` there. So `git pull` inside `openalgo-charts` changes
`src/` while the served UI keeps running the *previous* build, with no warning
anywhere: the host imports a directory that still exists and still typechecks.
This is exactly how the checkout was showing 2.1.0 chrome against a 2.1.8 source
tree. `npm run build` in `openalgo-charts` and in `frontend` is part of pulling,
not an optional step.

**A build artifact is stale until it says otherwise.** `npm run build` writes
`frontend/dist/BUILD_STAMP.json` (a hash of the sources it was built from, the
pinned library and its build hash, and a per-file hash of what it emitted),
`npm run verify-artifact` reads it back, and the Playwright `globalSetup`
refuses to start the suite against a bundle that is not the current tree — a bare
`npx playwright test` used to be able to pass against a previous bundle, which is
exactly how a deliberately-broken source file once passed. `trading/scripts/
e2e_smoke.py` compares the bytes `/ui/` serves against the stamp, so "the UI is
mounted" is a claim about the built artifact rather than about a 200.

**Bumping the library pin.** `trading/tests/analytics/test_golden_parity*.py`
asserts the backend against **checked-in** fixtures captured from the library's
TypeScript indicator math, so those tests do not read `openalgo-charts/` at all:
they pass against a library whose math has drifted, and equally against fixtures
captured from a version nobody runs. That is what had happened — the fixtures were
a 2.1.7 snapshot while the repo pinned 2.1.8 — so the pin now lives in
`openalgo-charts.pin` and a bump is an act with consequences:

```bash
cd openalgo-charts && git checkout <new-sha> && npm install && npm run build
cd .. && scripts/regenerate-goldens.sh            # refuses an unpinned/stale checkout
git diff --stat trading/tests/analytics/goldens   # the diff IS the review
.venv/bin/python -m pytest trading/tests/analytics -q
```

The 2.1.8 bump was not plumbing: it changed indicator input defaults
(`sma.length` 20 → 9, `stochastic.kSmoothing` 3 → 1), dropped `supertrend`'s
`bodyMid` plot, and added fields to the footprint result (implemented in
`analytics/profiles.py`). A fixture captured under the old defaults is
self-consistent and tests nothing about the new ones, because the gate takes its
parameters from each golden's own recorded settings — so a green parity run is not
evidence that the fixtures reflect the pin.

The chart is render-only: every computation lives in `tradex_trading`. Bars come from `/api/charts/history/{exchange}:{symbol}` (datalake-first, broker fallback), indicators from the backend registry surfaced through the chart's Tier-2 contract (`frontend/src/backend-indicators.ts`), backtests/scanners run `BacktestEngine`/`ScannerEngine` server-side (`POST /api/charts/backtest`, `POST /api/charts/scanner/run`), and forming bars/replay stream over `/ws/stream` (`subscribe_bars`, `replay_start/pause/resume/speed/stop`). Replay drives the SAME BarAggregator path as live quotes via `SyntheticTickGenerator`. Chart timestamps are UTC seconds of IST wall clock; the chart renders in Asia/Kolkata.

Chart state lives in **one store**: the server workspace blob
(`frontend/src/workspace.ts`), written as `{v, series, state}` where `series` is
the last bar time the saved viewport was captured against. The widget's own
`localStorage` persistence is deliberately **off** (`persist` is not passed), so a
layout can never be restored from two sources that disagree — see
`docs/design/2026-09-13-single-store-persistence.md`.

Two rules to know before touching it:

- **Restore runs before `createWidget`.** The blob is fetched and validated, then
  handed to `restoreState`, and the widget is constructed with the *stored*
  instrument so `restoreState` sees no change and does not reload. Booting with
  the defaults first would fetch the wrong symbol and then fetch again.
- **A viewport is a range of bar *indices***, so it is only restored when the
  fingerprint matches the series on screen; otherwise the layout is kept and the
  view is dropped via the engine's `stripView`. The fingerprint is checked
  against a datalake probe — that probe is also the load-window clock anchor, so
  validating costs no extra request.

Because `localStorage` is no longer a store, **the active workspace is defined as
the most recently saved layout** (`updated_at`), not a stored pointer. Entry
point: `loadActiveWorkspace()`.

---

### Protective levels (stop/target) — the one declaration path

A strategy declares its stop and target on the signal it emits, and the engine
carries them onto the order. `Signal.metadata['stop_loss_price']` and
`['target_price']`, the same names `OrderRequest` uses:

```
Signal.metadata  →  protective_request()  →  BracketOrderRequest  →  Order legs
                 (strategy/core/brackets.py)                      →  fills['stop'|'target']
                                                                  →  chart SL/TP lines
```

Build every signal-driven order with `protective_request()` — the engine's
`next_open` flush, its `signal_close` submit, and the backtest's recording-only
bridge all do, and hand-rolling an `OrderRequest` at a fourth site would drop the
legs there silently. Three rules the module enforces: the levels are a **pair**
(the venue's composite endpoint is the only carrier, so a lone leg is dropped
loudly), the geometry is checked against the price the order **actually fills
at** (an invalid pair degrades to unprotected rather than raising mid-run), and
nothing is ever inferred from a missing level.

The backtest does **not** simulate a protective exit — a drawn bracket is true
when the strategy also exits at its declared levels (see
`extensions/strategies/bracket_breakout.py`, the reference producer). See
`docs/design/2026-09-13-backtest-results-surface.md`.

---

### The backtest run window, and the per-trade table

**A run covers the bars its pane was handed** (`ctx.bars[0].time … [last].time`),
never `ctx.from`/`ctx.to`. Tier-2 passes those back as the *request's* window, and
the engine pages an extension (`load(c, state.to, c.to, true)`) whenever the
visible slice grows past what it holds — which every live bar append does. Using
them re-ran the strategy over the appended sliver: observed on a live 1h chart, a
51-round-trip run (Sharpe 0.32) became "Trades 0" after seven bars arrived.

`frontend/src/backtest.ts` keeps **one** published run (`publishBacktestResult` /
`currentBacktestResult`) that five surfaces read, and `frontend/src/trades-panel.ts`
renders it as one row per round trip (gross P&L; the open lot as a row of dashes).
Rows and zones both index `pairRoundTrips(fills).trips`, and a row click sets a
`TripSelection` (`{kind:'trip', index}` or `{kind:'open'}`) that the zone painter
reads — so the pick, the row's `aria-selected` and the emphasised zone all come
from one state and cannot disagree. A published run clears the pick.

Two traps, both already paid for: `metrics.num_trades` counts **fills**, not round
trips (55 vs 27 on the same run), and a row that selects something needs DOM —
`ChartTable.hitTest` returns the table's own `externalId` and never names a row.
See `docs/design/2026-09-13-trades-table.md`.

---

## Datalake Facts

**Coverage and size are deliberately absent here.** This block used to carry a
hardcoded size and day count, and they went stale without anything failing — a
measurement written into prose is wrong the moment the next backfill lands, and
no gate notices. Ask the lake instead:

```bash
python trading/scripts/datalake_stats.py            # coverage + density anomalies
python trading/scripts/datalake_stats.py --json     # same, machine-readable
python trading/scripts/datalake_stats.py --root /mnt/lake --top 10
```

It walks the parquet files: file count, bytes, rows, symbols, first/last
timestamp, trading-day count, the timeframe/kind/exchange breakdown, per-symbol
day coverage, and where the lake falls short of its own density — measured
*within a day* (against that day's densest symbol) and over 09:16–15:29, with
the two session-edge stamps (the 09:15 open and the 15:30 close) reported as
their own counts so a source convention cannot be laundered into a gap figure.
It measures bars present, not calendar gaps — for real gap detection use
`GapDetector` / `fill_gaps.py`.

Those two measure whether bars are *there*. They cannot tell you whether they
are the *right bars*, which is a third question and the one that bit hardest:

```bash
python trading/scripts/audit_symbol_resolution.py                   # offline
python trading/scripts/audit_symbol_resolution.py --verify-source 5 # + cross-check
```

It checks, for every universe symbol, that `NSE:<symbol>` resolves to the
security the universe CSV names (against each master's own identity field — the
Upstox ISIN, or Dhan's equity series), and then that the *stored* history looks
like that security, judged on turnover, no-trade minutes and single-day jumps
beyond NSE's widest circuit band. `--verify-source` fetches the worst jumps from
every broker and says which value the lake holds — a bad print, a re-based
series, or a move both venues agree on. Resolution is checked by running the
production loader against the dated master cache, so a green result means the
resolution the live system performs is right, not that a re-implementation
agrees with itself.

Detection is only half of it. When a stored day is *wrong* rather than missing,
no other tool can replace it — they all fetch gaps, and a wrong bar is not a gap:

```bash
python trading/scripts/reconcile_bars.py --symbols IRB,ANANDRATHI          # dry run
python trading/scripts/reconcile_bars.py --symbols IRB,ANANDRATHI --apply
```

It compares each stored day's high close against the broker's and rewrites only
the days that disagree. Both real defects it was written for were of this kind.
`IRB` held one day at half the broker's price with double the volume — turnover
preserved, so a pure adjustment factor rather than a different security: the
neighbouring days were continuous and that day was not. `ANANDRATHI`'s whole
pre-split history sat at exactly twice the broker's, the vendor having re-based
its history after a 2:1 split the lake predated — a phantom −49% crash for any
backtest crossing that date.

**Read the volume ratio**, because it names the cause: a corporate action
re-bases price and volume by opposite factors (price ÷2 with volume ×2), while a
wrong security moves neither. Two guards refuse a rewrite that would make things
worse — a broker response too thin to cover the day it would replace, which
would leave half a session at the old level, and a day only the broker has at
all (phantom sessions the storage guard drops at the write chokepoint; `2026-02-01`
is a Sunday in both masters). `--apply` writes, then re-reads and reports the
residual, because a repair announced without confirmation is how those days sat
wrong for months.

What follows are the invariants, which do not drift:

- **Root:** `<repo>/data/ohlcv/`, anchored by `datalake/paths.py` — never
  cwd-relative (`TRADEX_DATALAKE_ROOT` overrides it). `trading/data/` is **not**
  a data root; nothing reads it.
- **Layout:** Hive-partitioned `symbol=X/year=Y/month=M/data.parquet`, pruned by
  symbol + year/month on read.
- **Shape:** 1-minute bars, `kind="equity"`, NSE-only — the store is equity-only
  today.
- **Timestamps:** IST (tz-naive), weekday sessions only (9:15-15:30).
- **Source:** Dhan `/charts/intraday` via backfill; Upstox tops up ranges Dhan
  cannot serve.
- **Query layer:** the store is Parquet, not a database — no `.duckdb` file
  exists. DuckDB reads the same files: `services/duckdb-analytics/` is the
  guardrailed read-only SQL surface (memory/row/statement caps, MCP server),
  and ad-hoc scripts call `read_parquet(...)` over the same glob.
- **Not in git:** `data/` is gitignored, so CI and fresh clones have no lake
  (`seed_e2e_datalake.py` gives the E2E suite a synthetic one).

---

## Architecture Quick Reference

```
domain/          → Pure domain types (Equity, Candle, Order, Signal, etc.)
brokers/         → Broker adapters (Dhan, Upstox, Paper)
trading/         → Trading engine, strategies, datalake, analytics
frontend/        → openalgo-charts UI (Vite build; dist served at /ui)
Dependencies/    → Universe CSVs (nifty50/100/200/500)
data/            → Parquet datalake (ohlcv/)
runtime/         → Token state, broker runtime files
```

**Dependency direction:** domain ← brokers ← trading (never reverse)

---

## Things NOT To Do

1. **DON'T** manually construct DhanBroker/UpstoxBroker — use `build_broker_from_env()`
2. **DON'T** investigate TOTP/auth internals unless explicitly debugging auth
3. **DON'T** store UTC timestamps in the datalake — always convert to IST
4. **DON'T** assume broker data is clean — Dhan returns phantom post-market bars
5. **DON'T** fetch intraday history for ranges > 90 days from Dhan (API limit — `ParallelHistoryFetcher` raises `SDKError`)
6. **DON'T** reinvent parallel fetching — use `ParallelHistoryFetcher`
7. **DON'T** manually parse universe CSVs — use `load_universe()`
8. **DON'T** compute indicators/strategy logic in the frontend — the chart renders; `tradex_trading` computes (new backend indicator registry entries appear in the UI automatically via the Tier-2 wiring)
