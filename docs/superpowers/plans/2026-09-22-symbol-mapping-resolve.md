# Symbol Mapping Resolve Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** ISIN-aware rename alias at connect, typed sync outcomes, HEG→HEGAM CSV + lake migrate.

**Architecture:** `symbol_resolve.py` aliases old trading-symbol InstrumentIds onto EQ/BE provider keys matched by ISIN. `simple_sync` classifies fetch errors explicitly. One-shot lake rename script.

**Tech Stack:** Python 3.13, existing InstrumentRegistry, pytest.

**Spec:** `docs/superpowers/specs/2026-09-22-symbol-mapping-resolve-design.md`

## Global Constraints

- InstrumentId stays trading-symbol keyed.
- No new dependencies.
- Pytest: `.venv/bin/python -m pytest … -p no:cacheprovider --import-mode=importlib -c pyproject.toml`
- Do not run live broker sync during unit verification.

---

### Task 1: Universe attaches ISIN

**Files:** `trading/src/tradex_trading/datalake/universe.py`, `trading/tests/datalake/test_universe.py` (create if missing)

- [ ] Load Equity with `meta=InstrumentMeta(isin=…, extra={"series": series})`
- [ ] Test nifty fixture or Dependencies row has isin set
- [ ] Commit

### Task 2: `symbol_resolve.py`

**Files:** create `trading/src/tradex_trading/datalake/symbol_resolve.py`, tests

- [ ] `ResolveResult(ok, renamed, quarantine)`
- [ ] `resolve_universe_symbols(broker, instruments) -> ResolveResult`
- [ ] Build isin index from `broker._loaded_instruments` + registry meta/keys (EQ/BE)
- [ ] Alias via `register_authoritative(old_iid, key, meta)` when ISIN matches different symbol
- [ ] Tests with mock registry/broker
- [ ] Commit

### Task 3: Typed sync classify

**Files:** `simple_sync.py`, `parallel_fetcher.py` (error text), tests

- [ ] `_classify_fetch_error(msg) -> Literal[...]`
- [ ] Wire into empty/missing series branch
- [ ] Tests for NO_PROVIDER_KEY / EMPTY / TRANSIENT
- [ ] Commit

### Task 4: CSV + lake rename + wire callers

**Files:** Dependencies CSVs, `rename_symbol.py`, fill_gaps/cli/repair/sync_today

- [ ] Replace HEG→HEGAM in nifty lists containing HEG
- [ ] Script rename lake partition
- [ ] Call `resolve_universe_symbols` after connect in sync entrypoints; skip quarantine
- [ ] Run rename on real lake for HEG→HEGAM
- [ ] Commit

### Task 5: Verify

- [ ] pytest datalake + interface modules
- [ ] Optional: `audit_symbol_resolution.py` offline check for HEGAM
