# Symbol mapping resolve (tier 2) — design

Date: 2026-09-22.

Universe CSV lag and contested empties make sync look healthy while
symbols fail (`no provider key`) or skip (`empty series` = wrong
security). Tier 2: ISIN-aware connect resolve + typed sync outcomes +
HEG→HEGAM lake/CSV continuity. Domain stays trading-symbol keyed.

## Goals

- Renames (HEG→HEGAM, same ISIN) do not break `provider_key` or sync.
- Contested non-EQ primaries are **failed**, not skipped IPOs.
- Operators see quarantine / typed outcomes; lake history stays continuous
  across a rename.

## Non-goals

- Keying `InstrumentId` by ISIN (tier 3).
- Upstox close-side 15:15–15:29 top-up (separate ops run).
- Hand-maintained rename config files.

## Contracts

### Universe

`load_universe` still returns `Equity` with trading-symbol `InstrumentId`,
but sets `meta.isin` and `meta.extra["series"]` from the CSV
(`ISIN Code`, `Series`). CSV update: `HEG` → `HEGAM` in all nifty lists
that contain it (same ISIN `INE545A01024`).

### Connect-time resolve

After broker `load_instruments`, `resolve_universe_symbols(broker, universe)`:

1. Build ISIN → (instrument_id, provider_key, series) from registry meta /
   loaded master rows (EQ/BE only).
2. For each universe equity:
   - If `provider_key(iid)` set and primary series is EQ/BE → OK.
   - Else if CSV ISIN maps to an EQ/BE master row under a **different**
     trading symbol → `register_authoritative(old_iid, that_key, meta)`
     (and alias), log `rename: HEG→HEGAM`.
   - Else → append to `quarantine` (unresolved / non-equity primary).
3. Return `{ok, renamed, quarantine}`. Sync scripts skip quarantine.

Lives in `trading/src/tradex_trading/datalake/symbol_resolve.py`;
called from `fill_gaps` / `tradex sync` / `repair_gaps` after connect
(and optionally from `build_broker_from_env` callers that sync).

### Sync outcomes

Replace `"empty" in e` substring checks with an explicit classifier on
fetcher error strings / raised types:

| Outcome | SyncResult |
|---|---|
| OK (candles) | `fetched` |
| `EMPTY_SERIES` | `skipped` |
| `NO_PROVIDER_KEY` | `failed` |
| `NON_EQUITY_PRIMARY` | `failed` |
| `TRANSIENT` (429/network) | `failed` |

Optional: before history, if registry meta series not in `{EQ,BE}` →
fail as `NON_EQUITY_PRIMARY` without calling the API.

### Lake continuity

`trading/scripts/rename_symbol.py --from HEG --to HEGAM`:

- Rename hive dirs `symbol=HEG` → `symbol=HEGAM`.
- Rewrite `symbol` column in those parquets.
- Idempotent if target already exists (merge or refuse — prefer refuse
  if both exist with overlapping stamps; operator decides).

## File map

| File | Role |
|---|---|
| `datalake/universe.py` | Attach isin/series on Equity |
| `datalake/symbol_resolve.py` | Connect-time ISIN alias + quarantine |
| `datalake/simple_sync.py` | Typed classify → failed/skipped |
| `datalake/parallel_fetcher.py` | Stable error markers for classify |
| `Dependencies/nifty*_list.csv` | HEG→HEGAM |
| `trading/scripts/rename_symbol.py` | Lake migrate |
| Callers (`fill_gaps`, `cli cmd_sync`, …) | Call resolve after connect |

## Testing

1. Universe row carries ISIN.
2. Master registers HEGAM only; universe still HEG + ISIN → resolve aliases;
   `provider_key(NSE:HEG)` non-None.
3. Unknown ISIN → quarantine.
4. Classify: no provider key → failed; empty series → skipped;
   non-EQ primary → failed.
5. Rename script fixture: HEG partition becomes HEGAM.

## Success

- `audit_symbol_resolution`: HEG not unresolved after CSV update.
- Sync can fetch renamed equity (CSV current or aliased lag).
- Contested empties no longer masquerade as skipped IPOs.
