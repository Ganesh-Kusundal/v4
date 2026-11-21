# Principal Review — Delta Addendum (2026-11-21)

**Branch:** `refactor/phase1-safety` · HEAD `a3828c2c`
**Baseline:** `docs/audit/2026-11-06-principal-architecture-review.md` (579 lines, commit `638b24e`) — not repeated here.
**Method:** re-verified with `git log`, `code_search`, and `pytest tests/test_guardrails.py` (17 passed, 2026-11-21).

## 0. Verdict delta

The 2026-11-06 review's three headline defects are **materially remediated** since:

| 2026-11-06 finding | 2026-11-21 state [V] |
|---|---|
| Guardrail red: gateway redefined `_tf_seconds` | **Fixed.** `code_search _tf_seconds` → only guard test + docs. `tests/test_guardrails.py` **17 passed**. |
| Paper/live `context.analytics=None` → hand-rolled `_closes` fallback | **Fixed.** `03d90a3b fix(strategy): wire analytics lazily so paper/live match backtest (A3)`; parity test `trading/tests/parity/test_paper_live_analytics_wiring.py` green-path exists. |
| Live HTTP orders with only advisory gate | **Fixed server-side.** `interfaces/.../routes/orders.py:210` refuses `mode==live` unless `_live_orders_enabled()`; contracts carry `live_orders_enabled`. |
| No frontend | **Still true.** `ls frontend` → absent. Any frontend work remains green-field, contract-first. |
| `RevisedBar`/`mark_filled` dead, 15:30 flush | **Flush and revisions fixed; the freeze is not.** `SessionCloseFlush` (`737c48f3`) is the 15:30 caller, and a revision now reaches consumers as `BarRevised` (`3e4959f1`), pinned by `trading/tests/parity/test_eod_flush.py` and `test_sba_late_tick_parity.py`. `mark_filled` still has no production caller: the fill path is drift/latency-based and carries no `BarFrame`, so freezing needs the engine→aggregator link, not a call site — still open, recorded in `docs/DIVERGENCE.md`. |
| Incremental analytics (O(n²) recompute) | **Largely fixed.** `d2d89b1c` incremental EMA/SMA/RSI with batch parity, `5009f54f` incremental view, `d6bade8b` SMA-seeded EMA + Bollinger/MACD. |
| Sizer/property tests absent | **Fixed.** `73924737` property-based sizer+bracket invariants. |
| Versionless HTTP contract | **Fixed (A5).** `2ca252c6 feat(interfaces): version the HTTP response contract`. |
| New since: MCP agent surface | `50cfd190` MCP stdio + pluggable LLM; `a3828c2c` per-instrument strategy from definition. Vibe-Trading is a reference clone only (`vibe-trading/`, gitignored, ruff-excluded) — see integration spec 2026-11-21. |

**Real-money verdict (unchanged in kind, narrower in scope):** core is defensible for paper/research; live remains gated on (a) the traded-bar freeze (`mark_filled` unwired — a bar a fill was booked against can still be revised under it), (b) sizer/fees/pending-expiry parity documented in `docs/DIVERGENCE.md` with owners+expiry, (c) bracket-leg parity, (d) corporate-action adoption in live, (e) a typed frontend contract before any UI ships.

## 1. Audit — remaining violations

- **SOLID/SoC:** `interfaces/` remains the god-leaf (transport + presentation shaping + workspace + replay guarding). Acceptable as composition leaf *only if* presentation shaping (`stream_indicators.py`, depth reshape, order-status tier) is deleted once `BarFrame` is the single wire shape.
- **SSOT:** `domain/timeframe.bucket_seconds` is now the single home [V]. Remaining duplicate: `domain/timezones.py` vs `market_calendar.py: IST = timezone(timedelta(5,30))` — one-line fix, still open.
- **Dead code to delete now:** `RevisedBar` if no consumer within one release; `DurableFillDedup` if still unwired (verify callers before deleting); `_backpressure_triggered` stub. Prefer deletion per brief.
- **Frontend leakage:** none — there is no frontend. Backend leakage persists in `interfaces/` (see above).

## 2. Domain model (target — keep, tighten)

```
InstrumentId, Timeframe, Side          — frozen identity, hashed
Candle (UTC-aware ts, OHLCV, closed)   — one type for tick/1m/5m derivation
Signal                                 — side, strength, ref_ts, strategy_id (domain, not strategy pkg)
OrderRequest → Order(FSM) → Fill → Position → PortfolioSnapshot
Money = Decimal INR · RiskParams (validated at boot) · TradingSession (calendar-anchored)
MarketEvent = Quote | Depth | Candle | FeedStatus   — single bus payload union, wire_versioned
```

Rules: no UI/broker fields in domain; one `domain/serialization.py` for all types; `FromDict` rejects unknown keys without `wire_version`; all domain timestamps tz-aware UTC (IST-naive only at datalake boundary).

## 3. Architecture (hexagonal — as 2026-11-06 §3, with delta)

```
Presentation (absent) ──typed DTOs/WS──▶ Interface (fastapi + gateway, one envelope)
        ──commands/queries──▶ Application (one owner per use-case, incl. MCP agent tools)
        ──ports──▶ Domain/Strategy/Execution (pure) ◀──ports── Projections read-models
        ──ports──▶ Infrastructure (brokers, parquet, sqlite, duckdb, feeds)
```

Gateway + `duck_analytics` remain second products: one documented launch path each, or spin out. MCP agent (`interfaces/.../agent/`) must be read-only by default; any order tool requires the same server-side gate as HTTP.

## 4. Pipeline (deterministic, one engine)

```
Feed → Normalize → Clock(pinned) → BarAggregator → BarClosed
  → IndicatorEngine.push (O(1) amortized, incremental, golden-pinned)
  → Strategy.on_event → Signal → RiskManager → OrderRequest (deterministic correlation_id)
  → OMS.submit → FillSource(sim|paper|broker) → Fill/OrderEvent
  → PositionAccountant + CashLedger + EventStore.append (atomic) → Snapshot/PnL → Projections/WS
```

Backtest/replay/live/paper = one runner + `Feed` + `FillSource` + `Clock` config. Replay controls time only. No per-candle recompute.

## 5–7. Frontend / Backend / API

- **Frontend (future):** pure consumer. Owns rendering, input, replay *transport*. Forbidden: PnL/risk/indicator math, session decisions, venue-code mapping, secrets, money caching.
- **Backend:** broker-agnostic OMS via `FillSource` port; adapters for Dhan/Upstox/Paper behind capability tables; deterministic sim engine retained (`TapeRecorder` + tape-parity tests).
- **API:** Pydantic DTOs for every route (no `dict` bodies — lint it), one error envelope, `/api/v1` versioning, `BarDto` shared by history + WS, `X-Request-Id` end-to-end, server-enforced `live_orders_enabled`, WS Origin check.

## 8. Test strategy (north star: parity)

Unit (indicators/strategies) · property-based (fee monotonicity, position conservation, idempotency, sizer bounds) · replay-vs-live tape parity (strategies × sessions × clocks) · broker-adapter contract tests (capability-loud errors) · E2E simulation (order→fill→position) · regression guards (`no_dict_bodies`, `no_naive_now_in_domain`, `no_wallclock_outside_infra`). Every intentional divergence → `docs/DIVERGENCE.md` with owner + expiry.

## 9. Roadmap

| Phase | Change / Delete / Don't touch | Validation |
|---|---|---|
| 1 Safety | Seal guard green (done); boot refuses insecure secret perms; tape capture in CI. Delete: nothing. | Guard + parity green at HEAD [V: 17 passed] |
| 2 Domain | UTC-aware ts; `Signal` DTO; decide `RevisedBar` (delete preferred). Delete: `_backpressure_triggered`. | Serialization round-trip; goldens unchanged |
| 3 Architecture | Incremental `IndicatorEngine` everywhere (largely done); unified envelope; server gate (done). Delete: recompute sites, duplicate perm fn. | Parity grid green; replay≡live frame conformance |
| 4 Separation | Thin UI host *after* typed contract; generated TS client. | Playwright paper E2E; zero client trading logic |
| 5 Perf/Obs | Real-payload benchmarks; tracing on order path; seal-duration metrics. | p99 ≥ baseline; zero parity drift |

## 10. Non-negotiables

1. One engine, one pipeline, one `FillSource` port — no mode forks.
2. No `datetime.now` outside infra; no naive timestamps in domain; no `dict` in routes.
3. Fees on by default in every mode; no unprotected live brackets without parity tests.
4. Replay never reaches a venue — pinned by test.
5. Prefer deletion; every divergence gets an owner and an expiry date.
