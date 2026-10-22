# TradeX v4 — Agent Guide

---

## Standard Interfaces — USE THESE, DON'T REINVENT

### Broker Operations (Auth, History, Quotes, Orders)

**NEVER** investigate broker token generation, TOTP logic, or auth internals unless explicitly asked. Always use the standard interface:

```python
# 1. Load credentials
from tradex_config.env import load_env_file
load_env_file('.env.local')

# 2. Build broker (auto-handles TOTP auth, token refresh, instrument registry)
from tradex_runtime.live import build_broker_from_env
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

> **Path note (verified 2026-09-28).** These are the real package paths. There is
> no `tradex_trading` package anymore — `b941da2` retired it and
> `import tradex_trading` raises `ModuleNotFoundError`. The shim tree that used
> to re-export these names is gone, so the old paths do not merely warn, they do
> not import. Evidence: `git grep -n "def load_env_file" -- '*.py'` →
> `config/src/tradex_config/env.py:138`; `git grep -n "def build_broker_from_env"`
> → `runtime/src/tradex_runtime/live.py:565`; and
> `.venv/bin/python -c "import tradex_trading"` →
> `ModuleNotFoundError: No module named 'tradex_trading'`.

**Alternative: Full session boot** (includes strategy engine, bus, etc.)
```python
from tradex_config.schema import AppConfig
from tradex_runtime.startup import boot
from tradex_domain import BrokerId

cfg = AppConfig(broker_id=BrokerId.DHAN, mode='live', live_enabled=True)
session = boot(cfg)
# session.broker.history(...), session.broker.ltp(...), etc.
session.stop()
```

**`session.market` does not exist.** The accessor is `session.broker`
(`runtime/src/tradex_runtime/session_impl.py:373`), which returns the
`BrokerAdapter`; `history`/`ltp` live on it. There is no `market` property — the
closest name is `market_feed` (`:524`), and that is the live tick feed, not a
history surface. The `boot()` signature above is the real one
(`runtime/src/tradex_runtime/startup.py:158`): it takes `config`, `bus`,
`broker`, `wire_strategies`, `feed_instruments`, `validation_report_store`.

**Key points:**
- `build_broker_from_env()` handles ALL auth: TOTP generation, token minting, token persistence, refresh
- Credentials come from `.env.local` (DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, DHAN_PIN, DHAN_TOTP_SECRET, etc.)
- Timestamps from broker are UTC tz-aware — convert to IST before storing: `ts.astimezone(IST).replace(tzinfo=None)`
- Dhan `/charts/intraday` serves up to 90 days per request; `ParallelHistoryFetcher` **auto-chunks** long intraday ranges into ≤90-day windows rather than failing — `_DHAN_INTRADAY_MAX_DAYS = 90` and `_chunk_cap_for()` at `market_data/src/tradex_market_data/parallel_fetcher.py:35` / `:56`. (It raises `ValueError`, not `SDKError`, and only for the wrong broker count: "requires exactly one broker".)
- Dhan returns phantom post-market bars (16:00-20:00) — filter to 9:15-15:30 IST

### Datalake Operations (Parquet Storage, Backfill, Sync)

```python
# Read from datalake
from tradex_market_data.parquet_storage import ParquetStorage
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
  gapped, which is why the `min_gap_stamps` floor exists. `tradex sync` asks
  for the 09:15 open by default (`--no-open-stamps` turns it off); it is a real
  missing bar and Dhan serves it when the request starts a minute early.
- **Dhan's ceiling is part of the detector.** Every minute after 15:14 is
  classified with the 15:30 close, never reported as a hole and never requested.
  The one source is `BROKER_LIMITS.dhan_last_1m`.
- **The floor hides holes; it no longer hides that it hid them.** The 2026-08-31
  cohort was short 14 tail stamps against a floor of 15: a real hole that every
  run reported as "lake is clean". Counted now, and `--min-gap-stamps` is a flag.
- **`--tail-days N` is opt-in.** `tradex sync` scans the whole `--start`..`--end`
  window by default. `--tail-days 7` limits each symbol to its last 7 stored days
  (a daily top-up), and a hole older than that is then never detected.

**Syncing and repairing a backlog.** `tradex sync --start 2026-07-23 --budget 480`
is the one path (Dhan, 1-minute only). It scans, fetches only the holes, and
re-scans until the lake is clean or the budget is spent. Holes go worst first:
partial-day holes, then whole missing sessions, then bare 09:15 opens. Exit 0
means every servable minute in the window is present; a spent budget exits 1
and a rerun continues. Only one sync runs per lake (`.sync.lock`), because the
rate limiter and the partition locks are per process. `--only-symbols` and
`--skip-symbols` narrow the run; a symbol that comes back empty is usually
resolving to the wrong security. The command refuses a window in a year
`NSE_HOLIDAY_YEARS` does not cover. `fill_gaps.py` and `repair_gaps.py` were
removed; they re-implemented this loop.

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
them the ones requested.

> **Correction (verified 2026-09-28).** An earlier version of this paragraph said
> `ParallelHistoryFetcher` validates responses against the requested window via
> `_clipped_tail` and cross-broker tail-merges via `_merge_candles`. **Neither
> name exists** — `git grep -n "_clipped_tail\|_merge_candles" -- '*.py'` returns
> nothing, and the module docstring says the opposite:
> `market_data/src/tradex_market_data/parallel_fetcher.py:4-7` — *"No failover, no
> clipped-tail merge — those lived here when multi-broker sync was the path; sync
> is Dhan-only now, and Upstox tail repair is a separate script."* The fetcher's
> real post-fetch work is `_stitch_windows` (`:306`), which concatenates
> auto-chunked windows and dedupes by timestamp; it has no coverage check and no
> cross-broker path. Gap repair now lives in `tradex sync`
> (`SyncService.converge`). The 15-minute Dhan hole is real and is
> still not fixable by re-fetching; only the *mechanism* that was claimed to
> handle it was wrong.

### Universe Loading

```python
from tradex_domain.universe import load_universe, available_universes

# Load Nifty constituents as Equity instruments
instruments = load_universe('nifty500')  # returns list[Equity]
# [Equity('NSE:360ONE'), Equity('NSE:3MINDIA'), ...]

# Available universes: nifty50, nifty100, nifty200, nifty500
available_universes()  # ['nifty100', 'nifty200', 'nifty50', 'nifty500']
```

CSV files are in `Dependencies/nifty{50,100,200,500}_list.csv`.

### Parallel History Fetcher (single broker, parallel symbols)

```python
from tradex_market_data.parallel_fetcher import ParallelHistoryFetcher

# Long intraday ranges are auto-chunked (Dhan caps one poll at 90d intraday,
# Upstox at 30d minute); symbols are fetched concurrently.
fetcher = ParallelHistoryFetcher({'dhan': dhan_broker})
results = fetcher.fetch(instruments, Timeframe.M1, start, end)
# Returns dict[str, HistoricalSeries] keyed by instrument_id
```

Despite the plural-sounding name, the constructor requires **exactly one**
broker — `"requires exactly one broker"` is raised otherwise
(`market_data/src/tradex_market_data/parallel_fetcher.py`). It is parallel across
symbols, not across brokers. The old "split across ALL configured brokers" claim
was wrong even before the shim removal.

### Indicator parity goldens (chart library, UI removed)

The chart UI (`frontend/` + the `openalgo-charts` clone) was removed on
2026-09-29; the REST + WS surface remains (`/api/charts/*`, `/ws/stream`) and
is what tests and tooling target. What remains of the chart-library
relationship is the **golden-parity gate**: `frontend/` used to depend on
`openalgo-charts: file:../openalgo-charts`, a **gitignored** clone of an
independent upstream repo, and the golden fixtures under
`trading/tests/analytics/goldens/` are a snapshot of that library's TypeScript
indicator math at the commit pinned in `openalgo-charts.pin`. To re-derive or
regenerate fixtures, clone the library at the pin (`OPENALGO_CHARTS_ROOT`
overrides the expected `<repo>/openalgo-charts` location).

```bash
# The chart library, at the pinned upstream commit (public repo).
# The pin lives in openalgo-charts.pin (one file): the golden generators and
# the pytest provenance gate all read it.
git clone https://github.com/marketcalls/openalgo-charts.git openalgo-charts
cd openalgo-charts && git checkout "$(sed -e 's/#.*//' -e '/^\s*$/d' ../openalgo-charts.pin | head -1 | awk '{print $1}')"
npm install && npm run build
```

**`TRADEX_DATALAKE_ROOT`** points the datalake somewhere other than `<repo>/data`
(`datalake/paths.py`, default unchanged). Use it to run a server against a
fixture: seed a scratch root, serve with the override, and the real lake is never
touched.

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

Everything computational lives in the backend packages (`tradex_runtime`,
`tradex_analytics`, `tradex_market_data`): bars from
`/api/charts/history/{exchange}:{symbol}` (datalake-first, broker fallback),
indicators from the backend registry, backtests/scanners via `BacktestEngine`/
`ScannerEngine` (`POST /api/charts/backtest`, `POST /api/charts/scanner/run`),
and forming bars/replay over `/ws/stream` (`subscribe_bars`,
`replay_start/pause/resume/speed/stop`). Replay drives the SAME BarAggregator
path as live quotes via `SyntheticTickGenerator`. Chart timestamps are UTC
seconds of IST wall clock (`interfaces/src/tradex_interfaces/routes/chart.py`).
Workspace state ({v, series, state}) still persists server-side in the workspace
blob (`interfaces/src/tradex_interfaces/workspace_store.py`).

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

The backtest **does** simulate the protective exit, and deliberately not at the
declared level. A triggered stop is a stop-*market*: it fills at the worse of the
modelled gap past the trigger (`DEFAULT_STOP_GAP_BPS` in
`replay/src/tradex_replay/backtest.py`, 25 bps) and the worst price the triggering
bar actually printed — so a bar that gapped through the stop reports what the
market did, and a bar that merely traded through it cannot manufacture a loss. A
take-profit is a limit order and gets none of that: it fills at its level whenever
the bar reaches it, which is the one optimism left in the model and a stated
simplification rather than a claim. Stop wins when both legs pierce one bar. A
split also re-bases an open bracket's levels **and** its size, and is applied only
to a lot that already existed at the ex-date — so a pre-split stop is never
compared against a post-split market, and a lot opened after the ex-date is not
re-based at all. A drawn bracket is therefore evidence of what the model did, not
only of what the strategy declared; the reference producer is
`extensions/strategies/bracket_breakout.py`.

---

### The backtest run window, and the per-trade table

**A run covers the bars its pane was handed** (`ctx.bars[0].time … [last].time`),
never `ctx.from`/`ctx.to`. Tier-2 passes those back as the *request's* window, and
the engine pages an extension (`load(c, state.to, c.to, true)`) whenever the
visible slice grows past what it holds — which every live bar append does. Using
them re-ran the strategy over the appended sliver: observed on a live 1h chart, a
51-round-trip run (Sharpe 0.32) became "Trades 0" after seven bars arrived.

The per-trade table was rendered by the removed chart UI (`frontend/src/backtest.ts`
/ `trades-panel.ts`, one published run indexed by `pairRoundTrips(fills).trips`);
the backend contract it consumed is unchanged. Two traps, both already paid for:
`metrics.num_trades` counts **fills**, not round trips (55 vs 27 on the same run),
and the analysis surface must not re-derive round trips differently from
`analytics`' `pairRoundTrips`. See `docs/design/2026-09-13-trades-table.md`.

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
`GapDetector` / `tradex sync`.

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
trading/         → Tests + legacy scripts (the `tradex_trading` package was REMOVED; nothing imports it)
Dependencies/    → Universe CSVs (nifty50/100/200/500) + `nse_delistings.csv` (438 rows)
data/            → Parquet datalake (ohlcv/) + `quarantine/` for unreadable files
runtime/         → Token state, broker runtime files
config/          → `tradex_config` (env loading, schema)
market_data/     → `tradex_market_data` (parquet storage, parallel fetcher)
analytics/       → `tradex_analytics` (indicators)
execution/       → `tradex_execution` (engine, sqlite event store)
interfaces/      → `tradex_interfaces` (FastAPI routes, WS, the `tradex` CLI)
```

**Dependency direction:** `domain` ← `brokers` ← the extracted packages (`config`,
`market_data`, `analytics`, `execution`, `strategy`, `runtime`, `interfaces` …).
`trading/` is a downstream consumer — tests and legacy scripts — and no longer owns
source for any of these names.

---

## Things NOT To Do

1. **DON'T** manually construct DhanBroker/UpstoxBroker — use `build_broker_from_env()`
2. **DON'T** investigate TOTP/auth internals unless explicitly debugging auth
3. **DON'T** store UTC timestamps in the datalake — always convert to IST
4. **DON'T** assume broker data is clean — Dhan returns phantom post-market bars
5. **DON'T** fetch intraday history for ranges > 90 days from Dhan in one call — `ParallelHistoryFetcher` auto-chunks long ranges into ≤90-day windows, so if you bypass it you must chunk yourself (it no longer raises `SDKError`; that was the pre-auto-chunk behavior)
6. **DON'T** reinvent parallel fetching — use `ParallelHistoryFetcher`
7. **DON'T** manually parse universe CSVs — use `load_universe()`
8. **DON'T** compute indicators/strategy logic outside the backend packages — the API surface renders nothing itself; every computation lives in `tradex_analytics`/`tradex_runtime`/`tradex_strategy`

---

## Standing lessons (learned the hard way, re-verified 2026-09-28)

These are repo-specific traps that have each produced at least one wrong result. Read them
before trusting a test run, a doc claim, or a merge.

### 1. The editable venv defeats worktrees — reproduce mutations *in place*

The shared `.venv` is an editable install. `.venv/lib/python3.13/site-packages/` holds 17
`_editable_impl_tradex_*.pth` files, one per workspace package, and each is a single absolute
path into the **main** checkout — `_editable_impl_tradex_domain.pth` contains
`.../v4/domain/src`. Python loads those at startup from `site`, regardless of which worktree
launched the interpreter:

```
$ cd /tmp/pristine-worktree && ../../v4/.venv/bin/python -c "import tradex_domain as m; print(m.__file__)"
/Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/domain/src/tradex_domain/__init__.py
```

So a source mutation made inside a worktree is **silently never exercised** by a test run from
that worktree. This produced a false PASS here (a control appeared to pass with its lock
removed) and a wrong "pre-existing failure" verdict that hid 38 failing tests behind a
deselection.

**The fix is not a second worktree.** It is `git checkout <commit> -- <src files>` *in place*,
run the test, then restore. That changes what the interpreter actually imports. Any
"I reproduced this from a clean worktree" claim in this repo is unreliable until re-done in
place.

### 2. Docs that name a Python path are load-bearing, and they rot

`CLAUDE.md` is read before the code, so a stale import path in it is worse than no
documentation — it sends the next agent to a module that does not exist. As of 2026-09-28 this
file pointed at a package retired by `b941da2`, a serve command that exits without starting,
two functions that never existed (`_clipped_tail`, `_merge_candles`), and an error path
(`SDKError`) that had been replaced by auto-chunking. Every one of those was plausible prose
and none of it was true.

**Rule:** before you trust a doc's claim about a module, function, or command, re-derive it —
`git grep -n "<name>"` and actually run the command. A doc is a claim about the tree at a
moment in time, not ground truth. If you change a module path or a CLI entry, grep the docs
and runbooks for the old name in the same change.

### 3. A status table is a snapshot, not a state machine

The lane tables in `docs/audits/2026-09/pending-work-inventory.md` were accurate when written
and stale within days, because work landed after them — five "NOT STARTED" rows were already
contradicted by the closure log on the same page. Counts drift the same way: a test count or a
file count is true for exactly one commit.

**Rule:** treat any "NOT STARTED" or "absent" claim in an audit as unverified until you grep
the tree for the named symbol. Absence claims are the worst class — you cannot grep for what
is not there, so the burden of proof is on re-deriving the check, not on re-reading the table.
