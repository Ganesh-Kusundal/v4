# ADR / Design — TradeX v4 De-Shotgun Refactor (REF-1..10)

- **Date:** 2026-08-22
- **Status:** Approved (design gate passed 2026-08-22)
- **Origin:** Full architectural audit of `v4/` (Phase 1–5, findings SMELL-01..14)
- **Method:** Superpowers brainstorming → this spec → writing-plans → sliced implementation
- **Lens:** Ponytail (lazy-minimal). No behavior changes. No abstractions that existing duplication doesn't force.

---

## 1. Goal & Success Criteria

Eliminate every Phase-2 shotgun-surgery / coupling finding via four waves of
independently-verifiable, refactor-only slices.

**Done means:**

1. Zero `https://` broker literals outside `brokers/src/tradex_brokers/common/endpoints.py` (grep-enforced in CI).
2. Zero assignments to a foreign object's underscore attribute (new AST guard test in `tests/`).
3. One composition root: `runtime/startup.boot()`. `sdk/session.py` contains no
   "Mirrors ``runtime.startup.boot``" comments — the duplicated wiring they
   apologized for is deleted.
4. Five WS stream backends (Dhan order/market/depth, Upstox portfolio/market)
   share one skeleton in `brokers/common/ws_streams_base.py`; a reconnect-policy
   change touches exactly one file.
5. Full existing suite green after every slice:
   `pytest domain/tests brokers/tests trading/tests -q` plus
   `python -m compileall -q domain/src brokers/src trading/src` and
   `ruff check domain/src brokers/src trading/src`.

## 2. Non-Goals (ponytail cuts)

- No new abstraction beyond what duplication forces: no `SessionAssembler`
  class, no DI container, no config-file indirection for constants.
- No behavior change anywhere: fee math, reconnect timing, wire formats,
  env-var names and precedence are frozen.
- No rewrite of `common/resilience.py` or `common/token_lifecycle.py`.
- **Descope candidate:** REF-9's full Dhan→ProviderHttpClient migration. If
  Wave 4 runs long, ship only the `_build_url` dedupe + module-role docs and
  leave a documented exception in `common/__init__.py`.

## 3. Slice Plan — Four Waves

```
W1 vocabulary   REF-1, REF-2, REF-3, REF-10a        (independent, mechanical)
W2 contracts    REF-4 ──► REF-5                     (5 binds through 4's ports)
W3 composition  REF-6 ──► REF-7                     (7 needs 5's binding API + 1's URLs)
W4 deep merges  REF-8 ──► REF-9 ──► REF-10b         (8 needs 4)
```

One commit per slice; commit message references the SMELL-N finding.

### Wave 1 — Vocabulary

| Slice | Change | Files touched |
|---|---|---|
| REF-1 | New `common/endpoints.py`: frozen constants (`DHAN_REST_BASE_URL`, `DHAN_SANDBOX_REST_BASE_URL`, `UPSTOX_REST_BASE_URL`, `UPSTOX_HFT_BASE_URL`, `UPSTOX_V3_BASE_URL` + sandbox variants) and helpers `dhan_base(environment)`, `upstox_bases(environment)`. All six hardcoding sites import from it. | `common/endpoints.py` (new), `upstox/client.py`, `upstox/adapter.py`, `dhan/client.py`, `dhan/adapter.py`, `trading/runtime/live.py` |
| REF-2 | Extend existing `domain/market_calendar.py` with `IST` and `to_ist_naive(dt) -> datetime`. `clean_datalake.py` imports `MARKET_OPEN/CLOSE` instead of hardcoded `9:15/15:30`; `backfill_parquet.py:57` uses `IST`. | `domain/market_calendar.py`, `trading/scripts/clean_datalake.py`, `trading/scripts/backfill_parquet.py` |
| REF-3 | Delete `value_objects._normalize_symbol`; `InstrumentId.__post_init__` uses `wire.normalize_symbol`. Add pinning tests for `-EQ/-BE/-FUT` suffix stripping so the semantics become explicit and single-sourced. | `domain/value_objects.py`, `domain/wire.py`, `domain/tests/test_instrument_id*.py` |
| REF-10a | Module constant `ACQUIRE_TIMEOUT_S` replacing `timeout=30.0` ×4 in `parallel_fetcher.py`; `logger=`→`log=` ×2 (ws_streams files); delete `_q2` legacy alias, fix its two importers (`fees.py`, `position_math.py`) to import `q2`. | `datalake/parallel_fetcher.py`, `dhan/ws_streams.py`, `upstox/ws_streams.py`, `domain/utils.py`, `execution/fees.py`, `execution/position_math.py` |

**Wave-1 verification:** literal-equality grep diff (URLs, hours) before/after;
existing client/calendar/serialization suites green; new pinning tests added.

### Wave 2 — Contracts

| Slice | Change | Files touched |
|---|---|---|
| REF-4 | Extend `domain/protocols.py` (home of `BrokerClientPort`) with `@runtime_checkable` Protocols: `MarketStreamPort`, `OrderStreamPort`, `MasterRefreshProvider`. Replace marker dicts (`{"type": "upstox_market_data_stream", ...}`) with a `NullStreamBackend` null object. | `domain/protocols.py`, `common/base.py`, `dhan/ws_streams.py`, `upstox/ws_streams.py`, `upstox/client.py`, `sdk/session.py`, `runtime/market_feed.py` |
| REF-5 | Public surfaces replace every cross-object private write: `BaseBroker.bind_stream_backend(...)`, `BaseBroker.set_token_manager(...)`, `StrategyEngine.pending_snapshot()` + public `flush_pending(candle)`, logger injected via `Bus.__init__`. Add AST guard test `tests/test_no_foreign_private_writes.py`: no `x._attr = ...` where `x` is not `self`. | `runtime/live.py`, `dhan/adapter.py`, `upstox/adapter.py`, `common/base.py`, `strategy/core/engine.py`, `replay/backtest.py`, `reactive/bus.py`, `reactive/thread_safe_bus.py`, `tests/` (new guard test) |

**Wave-2 verification:** `isinstance` assertions at bind points in the
composition root; `contracts/test_adapter_protocol.py` extended; full
execution/replay/reactive suites green; guard test passes repo-wide.

### Wave 3 — Composition

| Slice | Change | Files touched |
|---|---|---|
| REF-6 | `TradingSession.live()/paper()` become thin wrappers synthesizing `AppConfig` and delegating to `startup.boot()`; the five duplicated wiring blocks in `session.py` are deleted. Parity test asserts both construction paths yield an identical component graph and start order. | `runtime/startup.py`, `sdk/session.py`, `tests/integration/`, `tests/sdk/` |
| REF-7 | New `runtime/bootstrap.py`: `BrokerSpec` NamedTuple per broker (env prefix list, token-manager factory, master-loader factory, adapter class, base-url resolver) + one generic `build_from_env(spec)`. `live.py` loses ~150 lines; `trading/` stops importing `tradex_brokers.{dhan,upstox}.master` (master loaders move behind each adapter's existing `instrument_loader` hook). | `runtime/bootstrap.py` (new), `runtime/live.py`, `dhan/adapter.py`, `upstox/adapter.py`, `brokers/{dhan,upstox}/master.py` exports, `tests/runtime/test_live_*.py` |

**Wave-3 verification:** parity test (boot vs factory paths); env-permutation
matrix test proving identical env-name resolution and precedence; live smoke
script run (`smoke_market_feed.py`) against paper.

### Wave 4 — Deep Merges

| Slice | Change | Files touched |
|---|---|---|
| REF-8 | New `common/ws_streams_base.py`: `_WsStreamBase(AutoReconnectMixin)` owns socket lifecycle, receive loop, subscribe bookkeeping, and one `guard_reconnect()` absorbing the triad copy-pasted ~15×. Each of the 5 backends becomes a subclass overriding 3–4 hooks (authorize URL/host, frame decode, wire-format encode, resubscribe policy). Dhan's throttle-specific server-disconnect handling (`ws_streams.py:449-463`) becomes a documented hook override with its harness test kept verbatim. Reconnect harness parametrized across all five configurations. | `common/ws_streams_base.py` (new), `dhan/ws_streams.py`, `upstox/ws_streams.py`, `common/ws_reconnect.py`, `common/streaming.py`, both clients' `*_stream_backend()` factories, `brokers/tests/test_stream_reconnect_harness.py` |
| REF-9 | Dedupe `_build_url` (single impl in `common/transport.py`; `provider_client.py` delegates); document roles of `transport.py` / `provider_client.py` / `client_shared.py` / `http_response.py` in `common/__init__.py`. Full Dhan→ProviderHttpClient migration only if budget remains (descope candidate). | `common/transport.py`, `common/provider_client.py`, `common/__init__.py`, optional `dhan/client.py` |
| REF-10b | Script sprawl: root `smoke_*.py` move to `trading/scripts/`; final grep gates (no URL literals outside endpoints.py; no `logger =`) wired into `.github/workflows/quality-gate.yml`. | root smoke files, `.github/workflows/quality-gate.yml` |

**Wave-4 verification:** golden frame-decode fixtures unchanged; reconnect
harness runs the same scenario suite against all five backend configs;
recorded-request fixtures prove identical URLs/headers for REF-9.

## 4. Uniform Verification Protocol (every slice)

1. `python -m compileall -q domain/src brokers/src trading/src`
2. `ruff check domain/src brokers/src trading/src`
3. `pytest domain/tests brokers/tests trading/tests -q`
4. Slice-specific golden/side-by-side diff (URLs, session graphs, reconnect
   scenarios, recorded requests)
5. Wave boundary: green CI `parity.yml` + `quality-gate.yml` before next wave

## 5. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| REF-6 breaks subtle live init order (feed start vs READY gate) | Parity test asserts component graph **and** start-order sequence; live smoke run |
| REF-8 regresses Dhan throttle-specific reconnect signature | Quirk becomes documented hook override; original harness test kept verbatim |
| REF-7 changes env-var precedence | Spec entries carry exact current env-name lists from `live.py`; matrix test over env permutations |
| Hidden coupling surfaces mid-slice | Brainstorming ratchet: stop, upgrade the slice's classification, re-present — never silently expand scope |
| REF-9 migration exceeds budget | Pre-declared descope: `_build_url` dedupe + docs only |

## 6. Traceability

| Audit finding | Slice(s) |
|---|---|
| SMELL-01 endpoint literals | REF-1 |
| SMELL-02 mirrored WS backends | REF-8 |
| SMELL-03 dual boot paths | REF-6 |
| SMELL-04 cross-module private writes | REF-5 |
| SMELL-05 naming-only contracts | REF-4 |
| SMELL-06 divergent normalize_symbol | REF-3 |
| SMELL-07 twin env builders | REF-7 |
| SMELL-08 inconsistent HTTP abstraction | REF-9 |
| SMELL-09 broker-internals imports from trading | REF-7 |
| SMELL-10 missing IST/session vocab | REF-2 |
| SMELL-11 fallback universe literals | (accepted — 2 files, low value to centralize; revisit if a third adapter appears) |
| SMELL-12 timeout literals | REF-10a |
| SMELL-13 standards drift | REF-10a |
| SMELL-14 script sprawl | REF-10b |
