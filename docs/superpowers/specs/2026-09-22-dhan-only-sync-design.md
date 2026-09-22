# Dhan-only datalake sync — design

Date: 2026-09-22.

Strip multi-broker failover and clipped-tail merge from the sync path.
One broker (Dhan). One gap plan. Honest results. Upstox fills Dhan’s
~15:14 ceiling in a **separate** follow-up script — not in sync.

## Goals

- Sync is fast, predictable, and free of multi-broker failure modes
  (split routing, blacklist, clip-merge, primary-wins collisions).
- Gap policy from the caller is the fetch plan (no silent re-detect).
- Operators can see skips; success ≠ “session grid complete.”

## Non-goals

- Implementing Upstox `repair_dhan_tail` in this change (follow-up).
- Changing ParquetStorage shape, GapDetector classification math, or
  broker auth.
- Re-introducing SyncOrchestrator / dual sync paths.

## Contract

```
caller: GapDetector.scan(**policy) → ranges
  → simple_sync(broker, store, instruments, tf, start, end, ranges=…)
  → ParallelHistoryFetcher({ "dhan": broker })   # single key only
  → series_to_frame → store.upsert
  → SyncResult
```

### `simple_sync`

| Param | Rule |
|---|---|
| `broker` | The only serving broker (Dhan in production scripts/CLI). |
| `ranges` | Optional. `dict[instrument_id, list[(start,end)]]` from the caller’s `GapDetector.scan`. When set, only those windows are fetched. When omitted, fetch the full `[start, end]` for every instrument. |
| `gaps` | **Removed.** Callers scan once and pass `ranges=`; sync never re-detects. |
| `failover_brokers` | **Removed.** |

### `SyncResult`

```
requested: int
fetched: int
written: int
failed: list[str]    # transient / hard errors (429, network)
skipped: list[str]   # empty / no series (IPO, unlisted, contested empty)
```

| Outcome | Field | Process exit |
|---|---|---|
| Bars upserted | `fetched` / `written` | 0 |
| Empty series | `skipped` | 0 (warn if any) |
| 429 / network | `failed` | 1 |
| Dhan stops ~15:14 | not an error | 0 — Upstox repair later |

Success means the job ran. Completeness remains `GapDetector.scan`.

## ParallelHistoryFetcher (slim)

**Keep:** auto-chunk (Dhan 90d intraday), thread pool, per-broker rate
limiter, empty→raise, `fetch(instruments, tf, start, end, ranges=)`.

**Delete:** failover loops, `_clipped_tail`, `_complete_shortfall`,
`_merge_candles`, multi-broker `_split` assignment, `_BrokerHealth`.

Always construct with a **one-entry** brokers dict. Unknown multi-broker
call sites must not be reintroduced by scripts.

## Callers

`fill_gaps`, `repair_gaps`, `sync_today`, `topup_gaps`, `tradex sync`,
and any backfill that routes through `simple_sync`:

1. Connect **Dhan only** (drop Upstox failover dict). No Dhan → exit 1.
2. `scan(**policy)` once → keep only instruments with non-empty gap lists → pass those + `ranges=` into `simple_sync`. For full-window backfill with no gap skip, omit `ranges` and pass the full universe.
3. Report `skipped` in logs/CLI; exit 1 only on `failed`.

`tradex sync --skip-existing` (or equivalent): CLI runs `GapDetector.scan` itself and passes `ranges=`; it does not hand a detector into sync.

## Follow-up (not this change)

`trading/scripts/repair_dhan_tail.py` (name flexible):

- Upstox only, sequential or low workers.
- Window: session close-side holes Dhan cannot serve (~15:15–15:29),
  driven by GapDetector ranges — not mid-sync merge.
- Never called from `simple_sync`.

## Testing

1. Happy path: single broker → upsert.
2. `ranges=` respected; symbols with empty gap lists are not fetched.
3. Empty → `skipped`, not `failed`.
4. Transient error → `failed`.
5. 90d auto-chunk still works.
6. Drop / rewrite tests for failover, clip-merge, multi-broker split, and `_plan_gaps` / `gaps=` detector path.

## Out of scope leftovers

- `NSE_HOLIDAYS_2026` year hardcoding (unchanged).
- Host TZ on scripts using naive `datetime.now()` — fix only if a
  touched script is already edited; not a sync redesign item.
- Contested-symbol resolution (loader concern; empty stays `skipped`).
