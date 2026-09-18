# Workspace view restore: the view belongs to the series

**Status:** implemented, tested
**Scope:** `frontend/src/workspace.ts`, `frontend/src/feed.ts`, `trading/.../routes/chart.py`
**Tests:** `frontend/e2e/workspace-viewport.spec.ts`

## The defect

A saved chart viewport is a range of **logical bar indices** — `viewport.from` /
`viewport.to` are positions in the series the chart held when it was captured.
They mean nothing against a different series.

Restore only checked *identity*: the layout id (`NSE_RELIANCE_1m_default`) and,
inside the engine, `symbol`/`exchange`/`interval`. Neither can see a dataset that
changed **under a stable identity** — the datalake backfilling a symbol, or a
window that used to be empty.

The observed failure:

```
blob written while 1m had 0 bars   viewport {from: -112.47, to: 3}
next load, 867 bars present        restore re-applies -112.47..3
                                   -> chart framed at the far-left edge
                                   -> widget re-saves the bad view
                                   -> survives every reload
```

It was self-perpetuating: the host restored the bad view, the widget's `layout`
event re-saved it, so the corruption outlived any single session. The blob for
that layout had reached **revision 28** by the time it was found.

## Two fixes

### 1. Series fingerprint (`workspace.ts`)

The stored blob becomes an envelope that names the series it was captured on:

```ts
interface StoredBlob {
  v: number;              // BLOB_VERSION = 2
  series: number | null;  // last bar time, or null for an empty series
  state: unknown;         // the engine state
}
```

`lastBarTime(widget)` reads the last bar's time off the price series. It is the
right identity because it pins the actual dataset while surviving the one change
that is *not* a new dataset — older bars paged in at the front.

On restore, the view is kept only when the envelope version matches **and** the
fingerprint matches:

```ts
const keepView = envelope !== null && envelope.v === BLOB_VERSION && saved === current;
```

When it does not match, the layout is restored through the engine's own
`stripView` — the library's existing rule for *same layout, different data*:
panes, indicators and drawings survive; viewport, bar spacing and pinned price
ranges go. Falling back to `stripView` rather than a bespoke cleaner keeps the
meaning of "view" owned by the library.

`settledSeries(widget)` resolves the fingerprint only once bars are present (or
after a 5 s bound). The restore runs on its own round trip and can win the race
against the first history load; comparing against an empty series would strip the
view on *every* reload — safe, but needlessly lossy.

### 2. The first view of a layout is persisted (`workspace.ts`)

The fix above needs a fingerprint to exist. It did not, for a fresh layout: the
widget emits `layout` only for **structural** changes (`_followChart`,
`widget.ts:876` — pane add/resize/move/maximize/remove, indicator removal and
settings, price-axis moves, `objects:change`, draw edits). **Nothing fires on a
plain data load.** Measured directly against the running app, a cold load on a
never-saved layout produced an empty request log — zero PUTs.

So the startup restore now reports whether the stored view was trustworthy, and
persists the corrected one when it was not, or when nothing was stored:

| startup state | persisted? |
|---|---|
| no blob for this layout | yes — records the first view + fingerprint |
| blob untrusted (mismatch, legacy, unreadable) | yes — records the corrected view |
| blob trusted | no — nothing changed |

The write waits on `settledSeries` so `payload()` cannot record `series: null`
behind a first load that is still in flight.

Blob format is versioned, so a pre-fix blob is recognised as *unvalidatable*
rather than being read as if it carried a fingerprint — it keeps its layout and
is upgraded on the spot.

## Cache-hit contract

`GET /api/charts/history` returns `last_closed_time`, and the host's anchor
(`feed.ts`) is built on it. The **cache-hit branch omitted it**, so a cache hit
would have silently handed the host no anchor. Now returned on that path too,
derived the same way `_serialize_series` derives it.

## Verification

`frontend/e2e/workspace-viewport.spec.ts`, run against the real stack (FastAPI +
parquet datalake + built `dist`), via `npm run e2e`:

| test | proves |
|---|---|
| first view of a layout is persisted | the cold-load gap above |
| a viewport captured against another dataset is not restored | the guard fires on mismatch and rewrites the fingerprint |
| a viewport that matches the series is restored | the guard is a comparison, not an unconditional strip |
| a legacy blob keeps the layout and drops the view | backward compatibility, and the in-place upgrade to `v: 2` |

Two properties keep these tests from passing for the wrong reason:

- **Seeds come from the app, not from fixtures.** `captureRealState` lets the host
  write a genuine blob and then mutates it, so the seeded state is one the engine
  actually accepts. A synthetic state the engine silently rejects would satisfy a
  viewport assertion while testing nothing.
- **A control proves the strip path is distinguishable.** The match test first
  asserts the engine's own fit differs from the seeded window, so a restored
  `from: 300` cannot be confused with the engine having ignored the blob.

**Negative control.** With the guard disabled (`keepView = true`, pre-fix
behaviour), the two strip tests **fail** and the other two pass. That is the
intended discrimination — the suite demonstrably fails without the fix rather
than passing vacuously.

Locally: 4/4 E2E pass, frontend `typecheck` + `build` clean, and
`pytest trading/tests domain/tests` → 2257 passed.

## Observed after the fix

Cold load, blob absent:

```
NSE_RELIANCE_1m_default   revision 1   v 2   series 1789018860
state.chart.viewport      {from: -1.0, to: 870}
```

`1789018860` is the datalake's newest bar (`Thu 10 Sep '26 11:11` IST) — the
statusline reads `867 bars` over the same series. A reload reproduces the
viewport byte-identically.

## Making the suite actually gate something

A regression suite nobody runs protects nothing. Verified before this change:
`.github/workflows/` held only `quality-gate.yml` and `parity.yml`, both
**Python-only** — no job built `frontend/` or ran a browser test. A broken chart
host could merge with every gate green.

`.github/workflows/frontend-gate.yml` now clones the library at its pinned
commit, builds it, then runs typecheck, build and the E2E suite.

Two obstacles had to be cleared first.

### The library is not in the repo

`frontend/` depends on `openalgo-charts: file:../openalgo-charts`, a gitignored
independent clone, so `npm ci` cannot resolve on a fresh checkout. The workflow
clones it, checks out the pinned SHA, asserts HEAD equals that SHA (so a moved
tag cannot silently change the UI), and builds — upstream tracks zero `dist/`
files and has no `prepare` hook, so the build is mandatory.

### CI has no market data

`data/ohlcv/` is gitignored, so the chart serves `source: "none"` with zero bars
and every browser assertion passes vacuously — the failure mode the suite exists
to prevent. `trading/scripts/seed_e2e_datalake.py` writes the smallest datalake
that makes the real read path produce real bars.

It goes through `ParquetStorage.upsert`, so the fixture carries the production
schema, OHLC validation and session mask; a hand-rolled `to_parquet` could write
rows the store would reject inbound, testing a datalake that cannot exist. It is
idempotent — it exits without writing when the symbol already has bars, so a
developer with the full lake is unaffected:

```
seed_e2e_datalake: RELIANCE already has 63539 bars in <repo>/data — nothing to do
```

`ParquetStorage.upsert` insert-or-replaces by `(symbol, timeframe, timestamp)` and
never truncates, so `--force` overwrites the overlap rather than clearing the
symbol, and `--days` can only widen coverage. Stated here because the first draft
of the seeder printed a count that quietly included pre-existing rows.

`TRADEX_DATALAKE_ROOT` (`datalake/paths.py`) points the store elsewhere, default
unchanged. That is what made the fixture path *provable* without touching the real
lake.

### Verified

| check | result |
|---|---|
| seeder on an empty root | 1,875 bars, correct Hive layout, round-trips through `ParquetStorage` |
| seeder idempotency | second run writes nothing |
| API on fixture parquet | 1m `1125`→`1875` bars `source: datalake`; 5m resamples to exactly `225`; D aggregates to 3 sessions |
| **E2E on a fixture-only lake** | **4/4 pass** (scratch root + `E2E_PORT`) — CI's exact condition |
| E2E on the real lake | 4/4 pass, seeder no-ops |
| backend | 3032 passed, 2 skipped |

That fixture-only run earned its keep: it failed first, because the spec
hardcoded `const API = 'http://127.0.0.1:8000'` instead of using `baseURL`, so
`E2E_PORT` was silently ignored. The spec now uses relative request paths and
Playwright resolves them. Worth noting the local run on the real lake passed both
before and after — only the fixture-only run could see the bug.

### Two more things the run surfaced

The server writes its workspace sqlite relative to its own cwd, which under
`webServer` is `frontend/` — so every run left an untracked
`frontend/runtime/workspace.sqlite` in the checkout (it is not covered by
`.gitignore`, which only excludes `/runtime/` and `trading/runtime/`). The config
now sets `TRADEX_RUNTIME_DIR` / `TRADEX_WORKSPACE_DB` to temp state, which also
stops the suite from reading or writing a developer's dev-session blobs.

`reuseExistingServer` is now `!process.env.CI`, the Playwright convention: reuse a
local dev server, but always boot a fresh one in CI, where reusing a stale server
would hide a broken boot.

## Known, not addressed

**Revision churn on load.** A restore emits structural events (the widget
re-applies panes and indicators), which the host maps to `layout` → save. So a
*trusted* restore still writes a byte-identical blob and bumps the revision. It is
idempotent, pre-existing, and the 409 optimistic-concurrency path handles the
cross-tab case, so it is left alone rather than risking that path.

**Dual persistence — resolved.** Chart state used to be persisted twice, the
widget's own `localStorage` and the server blob, with the server winning where
they disagreed. The server blob is now the only store; see
`2026-09-13-single-store-persistence.md`. The fingerprint rule below is unchanged
in behaviour, but it now runs *before* the widget is built, against a datalake
probe rather than against whatever the widget had loaded.
