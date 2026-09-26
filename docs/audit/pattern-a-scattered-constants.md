# Pattern A Audit — Scattered Constants / Magic Values

Repo: `/Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4`
Scope: `*/src`, `scripts/`, `trading/scripts/`, `frontend/src`, excluding `__pycache__` and `.venv`.

All findings below were opened and read; each literal was confirmed to encode the *same concept*,
not a coincidental numeric match. Ordered by blast radius, then by whether an existing central
constant is being bypassed.

---

[A-1] Datalake root path `"data/"` re-hardcoded past its own fix module
Concept: the datalake root directory. `market_data/src/tradex_market_data/paths.py` exists **solely**
to kill the bare relative `"data/"` literal (its docstring names the 2026-09-02 incident: launching
`tradex serve` from `trading/` made the chart API serve `source: "none"` with zero bars). It exports
`DATALAKE_ROOT` / `datalake_root()` — and four call-site defaults never adopted it.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/market_data/src/tradex_market_data/market_provider.py:36
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/market_data/src/tradex_market_data/market_provider.py:118
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/market_data/src/tradex_market_data/backtest_loader.py:56
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/market_data/src/tradex_market_data/catalog.py:28 (`"data/lake"` — a *third*, different spelling)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/market_data/src/tradex_market_data/parquet_storage.py:376 (docstring only)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/market_data/src/tradex_market_data/paths.py:4 (the fix module documenting the smell)
Blast Radius: 4 files (5 divergent spellings incl. `data/lake`)
Confidence: verified
**RE-HARDCODE OF AN EXISTING CENTRAL CONSTANT.** `catalog.py` using `"data/lake"` while everything else
uses `"data/"` means the two can never agree on a root even once anchoring is applied.

---

[A-2] `"Asia/Kolkata"` / `+05:30` IST identity hardcoded across interfaces, analytics, brokers
Concept: the Indian market timezone. `domain/market_calendar.py:21` defines `IST` as a fixed
`timezone(timedelta(hours=5, minutes=30))` and calls it "the datalake storage contract". Nothing
outside `domain` imports it. Every consumer re-derives it, and there are two *different
representations* (IANA string vs. fixed offset), one of which is used inconsistently in the same file.
Files (IANA-string form):
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/interfaces/src/tradex_interfaces/routes/stream.py:710, 900, 923, 925, 968, 1119
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/interfaces/src/tradex_interfaces/routes/chart.py:42
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/interfaces/src/tradex_interfaces/routes/stream_indicators.py:23
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/interfaces/src/tradex_interfaces/replay_run.py:13
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/tick_parser.py:46
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/profiles.py:15
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/seasonality.py:17
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/feed.ts:665
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/openalgo-charts/src/profile/market-profile.ts:69
Files (numeric-offset form):
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/interfaces/src/tradex_interfaces/routes/stream_indicators.py:72 — `IST = timezone(timedelta(hours=5, minutes=30))`, byte-for-byte the body of `domain.market_calendar.IST`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/profiles.py:16 — `IST_OFFSET_SECONDS = 5 * 3600 + 30 * 60`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/studies/studies_complex.py:193 — `IST_OFFSET = 5 * 3600 + 30 * 60`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/openalgo-charts/src/feed/time.ts:10 — `IST_OFFSET_SECONDS = 5 * 3600 + 30 * 60`
Blast Radius: 15+ files
Confidence: verified
Note: `stream_indicators.py` is the sharpest instance — it defines `_IST = "Asia/Kolkata"` at line 23
for `_IST_ZONE()`, then at line 72 re-creates the *same zone* as a raw fixed offset inline. Two
representations, one file.

---

[A-3] Timeframe → seconds map re-declared 3× in `runtime/`, bypassing `domain/timeframe.py`
Concept: seconds-per-bar for each `Timeframe`. `domain/timeframe.py` is documented as the "REF-02
single-source timeframe registry" and exposes `bucket_seconds()`. Three runtime modules re-declare
the identical map instead of calling it, and each has drifted on the 86400/86_400 spelling.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/runtime/src/tradex_runtime/bar_aggregator.py:71-76 (`_tf_seconds` — misses `W1` entirely, so `W1` raises)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/runtime/src/tradex_runtime/feed_recovery.py:39-44
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/runtime/src/tradex_runtime/feed_integrity.py:43-48
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/runtime/src/tradex_runtime/bar_aggregator.py:61 (`if seconds == 86_400` — day-detection by magic number)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/runtime/src/tradex_runtime/feed_recovery.py:143 (`lookback_seconds: float = 86_400.0`)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/domain/src/tradex_domain/timeframe.py:45-48 (the canonical map, unreferenced by the three above)
Blast Radius: 4 files
Confidence: verified
**RE-HARDCODE OF AN EXISTING CENTRAL CONSTANT.** Concrete divergence: `domain` maps `Timeframe.W1 -> 604800`,
`bar_aggregator._tf_seconds` has no `W1` key, so a weekly bar-aggregation request raises
`ValueError` where the domain registry would answer.

---

[A-4] Market session window `09:15` / `15:30` re-hardcoded past `domain/market_calendar.py`
Concept: NSE/BSE cash session bounds. `domain/market_calendar.py` states in its own docstring that it
"owns the only copy" of `09:15`/`15:30`. Two call sites re-inline it; `runtime/calendar.py` and
`market_data/parquet_storage.py` correctly import it, which proves the refactor pattern already works.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/runtime/src/tradex_runtime/feed_integrity.py:120 — `session_window: tuple[time, time] = (time(9, 15), time(15, 30))`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/trading/scripts/seed_e2e_datalake.py:73 — `SESSION_START = time(9, 15)`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/trading/scripts/datalake_stats.py:107, 109, 111 — SQL `TIME '09:16:00'`, `TIME '09:15:00'`, `TIME '15:29:00'`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/trading/scripts/datalake_stats.py:284, 286 — display labels `"09:15"`, `"15:30"`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/openalgo-charts/src/profile/market-profile.ts:69 — `startMinute: 9 * 60 + 15, endMinute: 15 * 60 + 30` (TS cannot import the Python constant; needs a generated/shared JSON contract)
Blast Radius: 4 files
Confidence: verified
Note: the SQL uses `09:16`/`15:29` as the *interior* band, which is derived-from but not equal-to the
session bounds — a fourth, non-obvious set of time literals that nothing constrains to stay in sync
with `MARKET_OPEN`/`MARKET_CLOSE`.

---

[A-5] Equity fallback universe `("RELIANCE", "TCS", "INFY", "HDFCBANK")` duplicated across brokers
Concept: the synthetic equity universe each broker adapter falls back to when no instrument master is
loaded. Dhan and Upstox declare *identical* tuples under different private names, and the frontend /
scripts hardcode the same symbols again.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/adapter.py:42 — `_DHAN_EQUITY_UNIVERSE`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/upstox/adapter.py:40 — `_UPSTOX_EQUITY_UNIVERSE`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/main.ts:21 — `symbol: 'RELIANCE'`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/tier2.ts:59, 69
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/trading/scripts/seed_e2e_datalake.py:76 — `DEFAULT_SYMBOL = "RELIANCE"`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/strategy/src/tradex_strategy/extensions/strategies/{sma_cross,macd_cross,rsi_reversal,bollinger_breakout,ema_ribbon_pullback,supertrend_flip}.py (7 strategy extensions all bind `RELIANCE`)
Blast Radius: 2 broker files + 2 frontend + 1 script + 7 strategies
Confidence: verified

---

[A-6] Frontend exchange allow-list triplicated, and `NSE` default repeated 4×
Concept: which exchanges the UI offers. `domain/enums.py:ExchangeId` is the canonical list of 9.
The UI declares a 5-element subset in two files and repeats the `'NSE'` default in four more places.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/main.ts:23 — `const EXCHANGES = ['NSE', 'NFO', 'BSE', 'BFO', 'MCX']`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/shellbar.ts:168 — `for (const ex of ['NSE', 'NFO', 'BSE', 'BFO', 'MCX'])` (inline duplicate of the constant 145 lines above its own use site in the same package)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/main.ts:21 — `exchange: 'NSE'`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/tier2.ts:58 — `default: 'NSE'`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/tier2.ts:68 — `?? 'NSE'`
Blast Radius: 3 files
Confidence: verified

---

[A-7] Order type vocabulary diverges across the language boundary: `SL`/`SL-M`/`STOP_LOSS`/`STOP`
Concept: order type. Four distinct spellings of the same three concepts (stop, stop-limit, market).
`domain/enums.py:OrderType` defines `MARKET/LIMIT/STOP/STOP_LIMIT`. The frontend invents a third
vocabulary and the two brokers a fourth and fifth.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/order-safety.ts:2 — `export type OrderType = 'MARKET' | 'LIMIT' | 'SL'`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/chart-lifecycle.ts:204 — `const apiType = type === 'SL' ? 'STOP' : type === 'SL-M' ? 'STOP_LIMIT' : type` (`'SL-M'` is not in the declared union — the select UI can emit a value the type system rejects)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/main.ts:144 — `order_type: order.type === 'SL' ? 'STOP' : order.type`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/client.py:220-223 — `"STOP_LOSS"` / `"STOP_LOSS_MARKET"`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/upstox/client.py:161-165 — `"SL"` / `"SL-M"`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/interfaces/src/tradex_interfaces/routes/orders.py:146, 207, 444 — `OrderType(body.get("order_type", "MARKET"))` / `default="LIMIT"` (two different defaults in one file)
Blast Radius: 5 files
Confidence: verified

---

[A-8] Product type vocabulary: frontend sends `MIS/CNC/NRML`, domain knows `INTRADAY/DELIVERY/MARGIN/MTF`
Concept: order product. `domain/enums.py:ProductType` is canonical. The UI select offers
`MIS/CNC/NRML` and the POST body at `chart-lifecycle.ts:189` sends `product: 'MIS'` — but
`interfaces/routes/orders.py:_build_request` never reads a `product` field at all (grep for
`product` across `interfaces/src` returns only two unrelated comment hits). The value is silently
dropped: the UI's product selector has no effect on the order.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/shellbar.ts:323 — `for (const prod of ['MIS', 'CNC', 'NRML'])`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/shellbar.ts:392 — `return productSelect?.value ?? 'MIS'`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/chart-lifecycle.ts:189 — sends `product:`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/chart-state.ts:35, 106 — `product?: string` (untyped passthrough)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/openalgo-charts/src/trade/order-engine.ts:38, 110 — `product?: 'CNC' | 'NRML' | 'MIS'`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/openalgo-charts/src/feed/openalgo-trade.ts:63, 124 — `defaultProduct = 'MIS'`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/domain/src/tradex_domain/enums.py:40-45 — the canonical `ProductType` (unused by the wire path)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/client.py:210-215 / upstox/client.py:170-178 — the only real product translators
Blast Radius: 5 files (+1 silent-drop)
Confidence: verified
This is a **functional** finding, not just cosmetic: the field crosses the wire and is discarded.

---

[A-9] HTTP client timeout `30.0` re-declared 9× across `brokers/` and `runtime/`
Concept: the default per-request HTTP timeout. One number, two packages, no constant.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/common/transport.py:34 (Protocol), :56 (`HttpTransport.__init__`)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/common/transport.py:120 — `timeout: float = kwargs.get("timeout", self._timeout)`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/runtime/src/tradex_runtime/live.py:82, 141, 228, 418, 497, 572
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/common/resilience.py:568 — `recovery_timeout: float = 30.0`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/common/resilience.py:887 — `timeout: float = kwargs.get("timeout", 30.0)`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/trading/scripts/e2e_smoke.py:51, 77 — `urlopen(req, timeout=30)`
Blast Radius: 4 files (11 sites)
Confidence: verified
`runtime/live.py:resolve_fetch(30.0)` is the *composition root* for the live brokers, so changing
`HttpTransport`'s default would not change the live path — they are two independent knobs that
happen to agree today.

---

[A-10] `DAY_SECONDS = 86400` / `HOUR_SECONDS = 3600` redeclared 4× in `analytics/`
Concept: seconds in a day / hour. Each file defines its own private copy; `domain/timeframe.py`
already owns the canonical values.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/profiles.py:17 — `DAY_SECONDS = 86400`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/ma_vol.py:41-42 — `_HOUR_SECONDS = 3600` / `_DAY_SECONDS = 86400`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/studies/studies_complex.py:194-195 — `DAY_SECONDS = 86400` / `HOUR_SECONDS = 3600`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/runtime/src/tradex_runtime/bar_aggregator.py:61 — `if seconds == 86_400`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/upstox/auth_flow.py:152 — `expires_at=clock() + 86400.0` (different meaning: token TTL in hours, coincidentally the same number — **not** reported as a matching concept)
Blast Radius: 4 files (real matches: 3)
Confidence: verified

---

[A-11] Session-break heuristic `max(4 * gap, 4h)` / `36h` cadence rule triplicated across Python and TS
Concept: how a "new trading session" is detected from bar timestamps. `ma_vol.py:44-50` explicitly
says it is "a Port of openalgo-charts `sessionStartIndices`" and `studies_complex.py:260` says
"`feed/time.ts` `sessionStartIndices`". The algorithm — median gap, 4-bar/4-hour threshold, 36-hour
cadence ceiling — is written out three times with independently-named constants.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/ma_vol.py:68, 79 — `max(4 * gap, 4 * _HOUR_SECONDS)`, `spans[...] <= 36 * _HOUR_SECONDS`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/studies/studies_complex.py:264, 272 — `max(4 * gap, 4 * HOUR_SECONDS)`, `> 36 * HOUR_SECONDS`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/openalgo-charts/src/feed/time.ts:490 — `Math.max(4 * gap, 4 * HOUR_SECONDS)`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/studies/studies_complex.py:36 (docstring naming the TS source)
Blast Radius: 3 files
Confidence: verified
Two of the three are near-identical Python; the two *differ* in the fallback: `ma_vol.py` falls back
to "the IST calendar day" when no break is readable, `studies_complex.py` returns `None`. A tuning
change to the 4h/36h constants must land in three places to stay consistent.

---

[A-12] `tick_size` default diverges inside one file: `0.05` (signature) vs `0.1` (spec table)
Concept: instrument tick size — the finest price increment used to bucket profile rows. The two
representations are for the *same* computation and disagree.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/profiles.py:330 — `compute_market_profile(..., tick_size: float = 0.05, ...)`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/profiles.py:666 — `PROFILE_SPECS["volume-profile"]["params"]["tick_size"] = 0.1`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/profiles.py:672 — `"tick_size": 0.1` (tpo)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/profiles.py:679 — `"tick_size": 0.1` (market-profile)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/openalgo-charts/src/profile/market-profile.ts:118 — `tickSize: 0.05` (the TS default the spec table is *meant* to match)
Blast Radius: 2 files
Confidence: verified
`PROFILE_SPECS` always supplies `tick_size` explicitly, so the `0.05` default is unreachable through
the normal dispatch path — but it is what a direct `compute_market_profile(bars)` call gets, and it
is 2× the value the TS primitive and the spec table use. NSE index tick is 0.05, equity 0.01, so
neither number is "wrong" — they encode two different instruments, neither named.

---

[A-13] `value_area_percent = 0.7` / TPO distribution thresholds `0.35`, `0.3` duplicated Python↔TS
Concept: the standard 70% value-area split and the two-peak / drive-distribution detection bands.
The same three ratios, with the same names, exist in the Python profile engine and the TS primitive.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/profiles.py:126, 173, 334, 666, 672, 683 — `value_area_percent: float = 0.7`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/profiles.py:295, 314 — `peak * 0.7`, `peak * 0.35`, `rng * 0.35`, `pos > 0.7 or pos < 0.3`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/openalgo-charts/src/profile/market-profile.ts:122 — `valueAreaPercent: 0.7`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/openalgo-charts/src/profile/market-profile.ts:420, 441 — `peak * 0.7`, `peak * 0.35`, `range * 0.35`, `pos > 0.7 || pos < 0.3`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/openalgo-charts/src/profile/volume-profile.ts:13, 20 — `valueAreaPercent = 0.7`
Blast Radius: 3 files
Confidence: verified
The two implementations are line-for-line equivalents; only the constant names differ.

---

[A-14] NSE trading-day count `252` and session minutes `375` hardcoded 21× in `analytics/`
Concept: annualisation basis. `reports.py:19` states the derivation ("NSE 375 min/day") but writes
`375` as a bare literal; `375` is separately defined in the seed script; `252` appears 16× in
`reports.py` and 5× in `engine.py` with no name.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/reports.py:22-41 — `252` × 16, `375` × 2 (lines 38, 39), `6.25`/`12.5`/`25`/`75` (derived, lines 28-37)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/engine.py:165, 300, 306, 307, 312 — `252` × 5
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/volatility/volatility.py:9 — `periods_per_year: int = 252`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/trading/scripts/seed_e2e_datalake.py:74 — `SESSION_MINUTES = 375`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/analytics/src/tradex_analytics/reports.py:19 (docstring naming the derivation)
Blast Radius: 4 files (21 sites)
Confidence: verified
`reports.py` computes `int(252 * 6.25)` for hourly = 1575 periods/yr, which is 252×6.25 = exactly
375/60 — so the mapping is internally consistent, but changing the session length or trading-week
count requires editing 21 literals in 4 packages with no single edit point.

---

[A-15] Risk mark-freshness `5.0` seconds duplicated in config and execution
Concept: how old a position mark may be before the fail-closed gate rejects new exposure. The
config default and both `RiskManager` constructor defaults are separate literals.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/config/src/tradex_config/schema.py:31 — `max_mark_age_seconds: float = 5.0`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/execution/src/tradex_execution/risk.py:70 — `max_mark_age_seconds: float = 5.0`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/execution/src/tradex_execution/risk.py:273 — `max_mark_age_seconds: float = 5.0`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/runtime/src/tradex_runtime/startup.py:391 — `max_mark_age_seconds=cfg.risk.max_mark_age_seconds` (the one correct wiring)
Blast Radius: 2 files
Confidence: verified
Low blast radius but high safety weight: the value gates *live order rejection*. The config is the
user-facing knob; the two `risk.py` defaults are silent fallbacks for any direct construction.

---

[A-16] Feed staleness `30.0` duplicated in `market_feed.py` and `feed_monitor.py`
Concept: seconds without a tick before an instrument is reported `StaleFeed`. `feed_monitor.py:36-37`
re-reads the *same* value as a literal fallback and derives a poll interval from it.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/runtime/src/tradex_runtime/market_feed.py:95 — `stale_after: float = 30.0`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/runtime/src/tradex_runtime/feed_monitor.py:36 — `float(getattr(feed, "stale_after", 30.0) or 30.0)`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/runtime/src/tradex_runtime/feed_monitor.py:37 — `return max(0.5, min(stale_after / 2.0, 30.0))` (the `30.0` cap reuses the number for an unrelated purpose)
Blast Radius: 2 files
Confidence: verified
Note the monitor is defensive by design (`getattr` with a default), but the fallback literal means a
`market_feed` constructed with a *custom* `stale_after` and an attribute-less stand-in would silently
revert to 30.

---

[A-17] `X-API-Key` header name + comparison logic triplicated in `auth/deps.py`
Concept: the legacy API-key authentication header and its constant-time check. Three functions each
re-implement the same `getattr(state) → compare_digest → 403` sequence.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/interfaces/src/tradex_interfaces/auth/deps.py:83 — `request.headers.get("X-API-Key")` in `resolve_identity`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/interfaces/src/tradex_interfaces/auth/deps.py:116 — same header, in `require_auth`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/interfaces/src/tradex_interfaces/auth/deps.py:203 — same header, in `verify_api_key`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/apikey.ts:41 — `{ 'X-API-Key': key }`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/feed.ts:497 — `?api_key=` query param (a *fourth* transport for the same credential)
Blast Radius: 2 files (5 sites)
Confidence: verified
Three backends + two frontend transports for one credential. A header rename is a 5-site edit with
no compiler help on the Python↔TS seam.

---

[A-18] `TRADEX_UI_ORIGIN` default `http://localhost:5173` in two files with different fallbacks
Concept: the allowed browser origin for CORS and CSRF. Both sites read the same env var, but
`fastapi_app.py` reads it once at app construction while `deps.py` re-reads it per request and
consults `app.state.ui_origin` first.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/interfaces/src/tradex_interfaces/fastapi_app.py:107 — `os.environ.get("TRADEX_UI_ORIGIN") or "http://localhost:5173"`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/interfaces/src/tradex_interfaces/auth/deps.py:153-156 — `getattr(app.state, "ui_origin", None) or os.environ.get("TRADEX_UI_ORIGIN") or "http://localhost:5173"`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/frontend/src/feed.ts:16 — `location.port !== '5173' ? '' : 'http://127.0.0.1:8000'` (client-side origin knowledge, different host spelling: `localhost` vs `127.0.0.1`)
Blast Radius: 3 files
Confidence: verified
`deps.py` is the safer of the two (it honours `app.state` first, so a programmatic override wins);
`fastapi_app.py` ignores `app.state` entirely. If a caller sets `app.state.ui_origin` without the env
var, CORS and CSRF disagree about the allowed origin.

---

[A-19] Runtime state directory: `".tradex_v4"` vs `<repo>/runtime` — two incompatible defaults
Concept: where broker token state, TOTP cooldowns, instrument caches and `workspace.sqlite` live.
`brokers/common/paths.py` documents the exact bug this class of default causes ("forked the *same*
token/totp/instrument state into a different directory for every launch cwd") and anchors to
`<repo>/runtime`. The config package instead defaults to the bare relative `".tradex_v4"`.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/config/src/tradex_config/env.py:47 — `os.environ.get("TRADEX_RUNTIME_DIR", ".tradex_v4")`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/config/src/tradex_config/schema.py:155 — `runtime_dir: str = ".tradex_v4"`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/config/src/tradex_config/schema.py:203 — `runtime_dir=str(data.get("runtime_dir", ".tradex_v4"))`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/common/paths.py:32-37 — reads `TRADEX_RUNTIME_DIR`, else `<repo>/runtime` (a **different** default for the same variable)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/interfaces/src/tradex_interfaces/fastapi_app.py:119-125 — correctly delegates to `default_runtime_dir()`
Blast Radius: 3 files
Confidence: verified
With `TRADEX_RUNTIME_DIR` unset, the config layer and the broker layer resolve the same variable to
**two different directories**. This is the documented shadow-state bug, reintroduced through a second
default.

---

[A-20] Dhan exchange-segment code maps re-declared per broker, plus a numeric reverse map
Concept: domain `ExchangeId` → Dhan `ExchangeSegment` string, and the binary-feed segment integer.
Four separate tables encode the same exchange taxonomy; the integer table has no canonical home.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/client.py:54-64 — `_DHAN_EXCHANGE_SEGMENT` (9 entries, keyed by domain `ExchangeId`)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/tick_parser.py:33-42 — `SEGMENT_EXCHANGE: dict[int, str]` (the *inverse*, keyed by wire int; `0..8`, with `6` absent)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/instruments.py:22-34 — `SEGMENT_CANONICAL: dict[tuple[str, str], str]` (13 entries, keyed by `(SEM_EXM_EXCH_ID, SEM_SEGMENT)`)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/client.py:75-85 — `_OPTION_LEG_EXCHANGE` (9 entries, a *different* exchange→exchange projection)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/upstox/instruments.py:13-22 — the Upstox `NSE_EQ`/`BSE_EQ`/`NSE_CURRENCY` family
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/adapter.py:180 — `if underlying.exchange.value in ("MCX", "NSE_COMM")`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/runtime/src/tradex_runtime/feed_integrity.py:38 — `_CASH_EXCHANGES = frozenset({"NSE", "BSE", "IDX"})` (raw strings where `ExchangeId` exists)
Blast Radius: 5 files (7 tables)
Confidence: verified
`_DHAN_EXCHANGE_SEGMENT` and `SEGMENT_EXCHANGE` are a forward/reverse pair over the *same* Dhan
segment taxonomy with no shared declaration — adding a segment means editing both plus the two
master-load tables.

---

[A-21] Dhan/Upstox instrument-kind codes `FUTIDX`/`OPTSTK`/`FUTCUR`/… re-declared 3×
Concept: Dhan's `SEM_INSTRUMENT_NAME` vocabulary. The same four future codes and four option codes
appear as a module-level set, an inline set, and a per-exchange mapping.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/instruments.py:36-37 — `_FUTURES = {"FUTIDX", "FUTSTK", "FUTCOM", "FUTCUR"}` / `_OPTIONS = {...}`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/client.py:294-295 — the same eight literals, inline
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/client.py:301, 303 — membership tests against re-typed sets
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/client.py:306-323 — the per-exchange `FUTIDX`/`FUTCOM`/`FUTCUR` and `OPTIDX`/`OPTFUT`/`OPTCUR` projections
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/instruments.py:38 — `_RIGHT_MAP = {"CE": "CE", "PE": "PE", "CA": "CE", "PA": "PE"}` (option right codes; `CE`/`PE` also appear bare in `domain/options.py` consumers)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/upstox/master.py:118-120 — `"FUTIDX", "FUTSTK", "FUTCOM"` again, in the *Upstox* package
Blast Radius: 3 files
Confidence: verified

---

[A-22] `OrderSide.BUY` compared as the raw string `"BUY"` instead of the enum member
Concept: buy-vs-sell sign. `domain/enums.py:OrderSide.BUY` exists, but three financial-math modules
branch on `fill.side.value == "BUY"` — string-typed where the enum is in scope.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/domain/src/tradex_domain/position_math.py:104
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/domain/src/tradex_domain/position_math.py:119
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/execution/src/tradex_execution/fees.py:253
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/execution/src/tradex_execution/cash_ledger.py:69
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/execution/src/tradex_execution/cash_ledger.py:71 — `self._debit(notional, "BUY fill")` (audit-log string)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/client.py:386 / upstox/client.py:270 — `OrderSide(str(row.get("transactionType", "BUY")).upper())` (defensible: parsing untrusted wire data)
Blast Radius: 4 files (5 branch sites)
Confidence: verified
`position_math.py` is *in* `domain` and still bypasses its own enum. Low blast radius, but it sits
on the signed-quantity path, so a typo would silently flip position direction rather than raise.

---

[A-23] Money rounding: `q2` is canonical, but `fees.py` re-derives it without `ROUND_HALF_UP`
Concept: paisa quantization for money. `domain/utils.py:19` states it is "the single source of truth
for money rounding across the entire TradeX v4 codebase" with `ROUND_HALF_UP`. `fees.py` calls
`quantize(Decimal("0.01"))` with no rounding mode, which uses the context default
(`ROUND_HALF_EVEN`) — a silent convention divergence on the fee and cash path.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/execution/src/tradex_execution/fees.py:119 — `breakdown.total.quantize(Decimal("0.01"))`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/execution/src/tradex_execution/fees.py:174 — same
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/execution/src/tradex_execution/fees.py:254, 255 — same, in `total_cost`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/domain/src/tradex_domain/utils.py:19 — the canonical `q2` (`ROUND_HALF_UP`)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/execution/src/tradex_execution/fees.py:170 — `Money(amount=q2(total))` (the same function uses `q2` on the *capped* branch and raw `quantize` on the uncapped branch, two lines apart)
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/execution/src/tradex_execution/reconciliation.py:111, 132, 176 — `Decimal("0.01")` tolerances (different concept, but the same re-derived literal)
Blast Radius: 2 files (4 divergent sites)
Confidence: verified
`fees.py:170` uses `q2` and `fees.py:174` uses raw `quantize` in adjacent branches of the same
`if/else` — the two branches round differently on exact half-paisa values. The divergence is
one-paisa per fill, but it is exactly the kind that breaks backtest/live parity, and the repo has
a `trading/tests/parity/` suite guarding that.

---

[A-24] Datetime format strings `"%Y-%m-%d"` / `"%Y-%m-%d %H:%M:%S"` re-declared across 4 packages
Concept: the wire and storage datetime formats. No shared format constants module exists; the
strings are inline at each parse/format site.
Files:
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/dhan/_marketdata.py:230, 240, 241 — `strftime("%Y-%m-%d %H:%M:%S")`, `strftime("%Y-%m-%d")`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/domain/src/tradex_domain/wire.py:86 — `iid.expiry.strftime("%Y%m%d")`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/domain/src/tradex_domain/value_objects.py:123, 154 — `strptime(parts[2], "%Y%m%d")` / `strftime("%Y%m%d")`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/interfaces/src/tradex_interfaces/cli.py:437, 473 — `strptime(args.end, "%Y-%m-%d")`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/trading/scripts/backfill_parquet.py:66, 67, 174 — parse and re-format `"%Y-%m-%d"`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/runtime/src/tradex_runtime/logging_config.py:58 — `datefmt="%Y-%m-%dT%H:%M:%S"`
- /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4/brokers/src/tradex_brokers/common/provider_common.py:184-190 — a 7-entry `strptime` format ladder (a de-facto canonical list, private to one module)
Blast Radius: 6 files
Confidence: verified
Two distinct format families are in play: the dashed `%Y-%m-%d` (CLI/datalake/Dhan REST) and the
compact `%Y%m%d` (instrument-key expiry). The instrument-key format in particular is a *wire
contract* — `domain/wire.py` and `domain/value_objects.py` each spell it out rather than sharing a
constant, and any third-party key parser has to match it.

---

## ALREADY-CENTRALIZED

These are working well and should be *reused* by the refactor rather than reinvented:

1. **`domain/src/tradex_domain/market_calendar.py`** — the model to copy. It owns `MARKET_OPEN`,
   `MARKET_CLOSE`, `IST`, the `_STR` variants, the Dhan per-exchange session dicts, `NSE_HOLIDAYS_2026`
   and `to_ist_naive`, in a stdlib-only module with a docstring that names its own original
   duplication. `runtime/calendar.py` and `market_data/parquet_storage.py` both import it correctly.
   Findings A-2 and A-4 are the stragglers that never adopted it.

2. **`domain/src/tradex_domain/timeframe.py`** — one registry for timeframe→broker interval mapping,
   bucket seconds, and `MAX_POLL_DAYS` per provider. `dhan_interval()` / `upstox_unit_interval()` /
   `bucket_seconds()` have clean accessors. A-3 is three runtime modules that re-derived a subset and
   dropped `W1` in the process.

3. **`execution/src/tradex_execution/fees.py`** — STT/brokerage/exchange/GST/SEBI/stamp-duty rates as
   named `_`-prefixed module constants, with a docstring that records the historical GST-base bug and
   the statutory-vs-default SEBI ambiguity. Every re-used fee number outside it (A-23 aside) is a
   test assertion, which is correct. This is exactly the shape A-1/A-3/A-4 need.

4. **Per-broker rate tables** — `brokers/src/tradex_brokers/{dhan,upstox,paper}/rate_table.py` each own
   one `*_RATE_LIMITS` dict, with the *reasoning* for every non-obvious number recorded inline
   ("capacity 80 -> 50 to match the documented per-second burst (80 exceeded it)"). Same for
   `common/totp_cooldown.py`'s `DHAN_COOLDOWN_SECONDS` / `UPSTOX_COOLDOWN_SECONDS`. A per-broker
   module beats a shared global table when the values are genuinely provider-specific — do not
   "centralize" these into one file.

5. **Path anchoring modules** — `market_data/paths.py` and `brokers/common/paths.py` both encode the
   "never resolve against process cwd" rule as a documented, testable function
   (`datalake_root()`, `default_runtime_dir()`), and each docstring records the specific production
   incident that motivated it. A-1 and A-19 are failures to *use* these, not failures of the
   pattern — the fix is one import per call site.

6. **`domain/src/tradex_domain/enums.py` + `domain/utils.py`** — `ExchangeId`, `BrokerId`,
   `Timeframe`, `ProductType`, `OrderType`, `OrderSide`, `TimeInForce` as `StrEnum`s, and `q2` as the
   one money-rounding function. The enums are correctly used at every *boundary* (CLI choices, REST
   parse, broker payload build). The gaps are internal branches (A-22) and the browser boundary
   (A-7, A-8), which no amount of Python discipline can fix — those need a generated TS/Python
   contract file.
