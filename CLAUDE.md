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

# Smart routing: < 30 days → both brokers, >= 30 days → Dhan only
fetcher = ParallelHistoryFetcher({'dhan': dhan_broker, 'upstox': upstox_broker})
results = fetcher.fetch(instruments, Timeframe.M1, start, end)
# Returns dict[str, HistoricalSeries] keyed by instrument_id
```

### Chart UI (openalgo-charts frontend)

```bash
# Build the frontend (output: frontend/dist/, served by FastAPI at /ui/)
cd frontend && npm install && npm run build

# Serve everything (REST + WS + UI) — then open http://127.0.0.1:8000/ui/
python -m tradex_trading.interface.cli serve --broker paper --port 8000
```

The chart is render-only: every computation lives in `tradex_trading`. Bars come from `/api/charts/history` (datalake-first, broker fallback), indicators from the backend registry surfaced through the chart's Tier-2 contract (`frontend/src/backend-indicators.ts`), backtests/scanners run `BacktestEngine`/`ScannerEngine` server-side (`POST /api/charts/backtest`, `POST /api/charts/scanner/run`), and forming bars/replay stream over `/ws/stream` (`subscribe_bars`, `replay_start/pause/resume/speed/stop`). Replay drives the SAME BarAggregator path as live quotes via `SyntheticTickGenerator`. Chart timestamps are UTC seconds of IST wall clock; the chart renders in Asia/Kolkata.

---

## Datalake Facts

- **Location:** `data/ohlcv/` (Hive-partitioned: `symbol=X/year=Y/month=M/data.parquet`)
- **Coverage:** 500 Nifty symbols, ~63+ trading days, 1-minute bars
- **Timestamps:** IST (tz-naive), market hours only (9:15-15:30)
- **Size:** ~261 MB
- **Source:** Dhan `/charts/intraday` API via nTrade backfill

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
