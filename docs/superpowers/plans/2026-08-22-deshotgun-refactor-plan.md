# Implementation Plan — De-Shotgun Refactor (REF-1..10)

Spec: `docs/superpowers/specs/2026-08-22-deshotgun-refactor-design.md` (commit `c4ea73c`)
Rules: one commit per slice · full suite green before commit · no behavior change.

Shared gate (run after every slice):
```bash
python -m compileall -q domain/src brokers/src trading/src && \
ruff check domain/src brokers/src trading/src && \
python -m pytest domain/tests brokers/tests trading/tests -q
```

---

## Wave 1 — Vocabulary

### Slice REF-1 — endpoint constants
1. Create `brokers/src/tradex_brokers/common/endpoints.py`: module constants exactly equal to today's literals (`DHAN_REST_BASE_URL = "https://api.dhan.co/v2"`, sandbox variants from `live.py:231`, `UPSTOX_REST_BASE_URL/_HFT/_V3` + sandbox variants) + helpers `dhan_base(environment)` / `upstox_bases(environment)` reproducing `live.py`'s env fallback chains verbatim.
2. Replace defaults in `upstox/client.py` (×2 signatures), `upstox/adapter.py`, `dhan/client.py` (incl. `BASE_URL` class attr → constant), `dhan/adapter.py`.
3. Rewire `trading/runtime/live.py:_dhan_base/_upstox_bases` to delegate to the helpers (keep the function names; callers unchanged).
4. Gate + grep proof: `grep -rn 'https://' brokers/src trading/src --include='*.py' | grep -v endpoints.py` returns only master-download URLs and docstrings.
Commit: `refactor(brokers): single-source broker REST base URLs [SMELL-01]`

### Slice REF-2 — IST/session vocabulary
1. Append to `domain/market_calendar.py`: `IST = timezone(timedelta(hours=5, minutes=30))` and `def to_ist_naive(dt)` (aware→IST-naive, naive passthrough); add both to `__all__`.
2. `backfill_parquet.py`: delete local `ist = ...` line, use `to_ist_naive`.
3. `clean_datalake.py`: replace hardcoded 9:15/15:30 comparisons with `MARKET_OPEN`/`MARKET_CLOSE` imports.
4. Add `domain/tests/test_market_calendar.py::test_to_ist_naive_roundtrip`.
Commit: `refactor(domain): canonical IST + naive conversion [SMELL-10]`

### Slice REF-3 — one normalize_symbol (behavior-preserving)
⚠ Decision: do NOT make `InstrumentId` strip `-EQ/-BE/-FUT` — that would change behavior.
1. Canonical fn lives in `value_objects.py` (avoids the `wire`→`value_objects` import cycle):
   `normalize_symbol(value, *, strip_provider_suffixes=False)` — today's strip+upper with the suffix loop gated by the flag.
2. `wire.py` re-exports it and passes `strip_provider_suffixes=True` at its current suffix-stripping call site.
3. Delete `_normalize_symbol`; `InstrumentId` calls `normalize_symbol(self.underlying)` (flag off → identical behavior).
4. Pinning tests: suffix stripped iff flag on; registry roundtrip tests stay green.
Commit: `refactor(domain): merge divergent normalize_symbol behind explicit flag [SMELL-06]`

### Slice REF-10a — standards sweep
1. `parallel_fetcher.py`: `ACQUIRE_TIMEOUT_S = 30.0`; replace 4 literals.
2. `dhan/ws_streams.py`, `upstox/ws_streams.py`: `logger` → `log` (+ call sites).
3. `domain/utils.py`: delete `_q2` alias; fix imports in `fees.py`, `position_math.py`.
Commit: `style: log naming, timeout const, drop _q2 alias [SMELL-12/13]`

---

## Wave 2 — Contracts

### Slice REF-4 — stream/master Protocols
1. `domain/protocols.py`: add `@runtime_checkable` Protocols `MarketStreamPort`, `OrderStreamPort`, `MasterRefreshProvider` (methods only, matching existing `BrokerClientPort` style); export from package `__init__`.
2. New `NullStreamBackend` in `common/streaming.py` implementing both stream ports as no-ops returning sentinel subscription ids; replaces marker dicts in `upstox/client.py:366,390`.
3. Annotate `base.py` backend attrs with the port types (`_ws_backend: MarketStreamPort | None`, etc.).
Gate + extend `contracts/test_adapter_protocol.py` with isinstance checks.
Commit: `feat(domain): typed stream/master ports; kill marker-dict backends [SMELL-05]`

### Slice REF-5 — no foreign private writes
1. `BaseBroker.set_token_manager(tm)`; adapters call it instead of `broker._token_manager = ...`.
2. `BaseBroker.bind_stream_backend(kind, backend)`, kind ∈ {"market","order","depth"}; `live.py:480,554` call it with `broker.market_stream_backend()`.
3. `StrategyEngine.pending_snapshot()` (read-only copy) + public `flush_pending(candle)` delegating to `_flush_pending`; `backtest.py` uses them.
4. `Bus.__init__(..., log=None)`; delete `thread_safe_bus.py:37` write-back.
5. New `tests/test_no_foreign_private_writes.py`: AST walk over all three srcs flagging `x._attr = ...` where `x` is not `self`. Fix any stragglers it finds (`session._market_feed` lands in Wave 3).
Commit: `refactor: public binding APIs replace cross-object private writes [SMELL-04]`
---

## Wave 3 — Composition

### Slice REF-6 — one boot path
1. Extend `startup.boot()` to accept injected `broker=`; extract shared wiring so BOTH `TradingSession.live()/paper()` and CLI paths funnel through it.
2. Delete session.py's five duplicated blocks; constructor receives fully-wired deps.
3. Parity test `tests/integration/test_boot_parity.py`: build via `boot(cfg)` vs factory; compare component types and recorded start-order list.
Commit: `refactor(runtime): single composition root for TradingSession [SMELL-03]`

### Slice REF-7 — table-driven bootstrap
1. Snapshot first: capture resolved URLs/env-names per (provider × LIVE/SANDBOX) into a test fixture.
2. `runtime/bootstrap.py`: `BrokerSpec(NamedTuple)` = env_prefixes, environment_key, token_manager_factory, master_loader_factory, adapter_from_fetch, base_url_resolver — two specs copied verbatim from today's `live.py`.
3. Generic `build_broker_from_env(provider, **kw)` looks up the spec; old public names remain as thin aliases.
4. Master parsing moves behind adapters (`instrument_master_loader(fetch, cache)` hook); `live.py` drops `tradex_brokers.{dhan,upstox}.master` imports.
5. Env-matrix test against the step-1 snapshot.
Commit: `refactor(runtime): table-driven broker bootstrap; unimport broker masters [SMELL-07/09]`

---

## Wave 4 — Deep Merges

### Slice REF-8 — WS skeleton consolidation
1. Snapshot behavior: run reconnect harness, save per-backend scenario results.
2. Create `common/ws_streams_base.py`: `_WsStreamBase(AutoReconnectMixin)` with final methods `_ensure_ws/_receive_loop/close/feed_raw/unsubscribe` and hooks `_authorize_url()`, `_decode_frame(raw)`, `_encode_subscribe(instruments)`, `_resubscribe(ws)`; plus `guard_reconnect()` absorbing the reset/schedule/thread-check triad.
3. Convert backends one at a time (Dhan order → Dhan market → Dhan depth → Upstox portfolio → Upstox market); harness green after each. Dhan throttle-disconnect quirk stays as a close-frame-handler override, original test untouched.
4. LOC target: ~1350 combined → <700.
Commits (one per backend): `refactor(brokers): <backend> onto _WsStreamBase [SMELL-02]`

### Slice REF-9 — HTTP dedupe (descope-aware)
1. Single `_build_url` impl in `HttpTransport`; `ProviderHttpClient._build_url` delegates.
2. Role docs for transport/provider_client/client_shared/http_response in `common/__init__.py`.
3. STOP unless budget remains: full Dhan migration deferred with documented exception.
Commit: `refactor(brokers): dedupe URL builder; document transport roles [SMELL-08]`

### Slice REF-10b — sprawl + CI gates
1. `git mv smoke_*.py trading/scripts/`; fix path assumptions; run each once.
2. `quality-gate.yml`: add grep gates (URL literals outside endpoints.py; `logger =`) — the private-write AST test already lives in the main suite.
Commit: `chore: consolidate smoke scripts; CI structural gates [SMELL-14]`

---

## Execution Notes

- Order strict across waves, loose within Wave 1.
- Any gate failure caused by code outside the slice's diff: stop, ratchet up classification, re-present (spec §5).
- Final step: re-run Phase-2 greps; confirm dispositions match spec §6 traceability table.

