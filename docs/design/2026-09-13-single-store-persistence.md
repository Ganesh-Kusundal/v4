# One store for chart state

**Status:** implemented, tested
**Scope:** `frontend/src/workspace.ts`, `frontend/src/main.ts`, `frontend/src/feed.ts`
**Tests:** `frontend/e2e/workspace-viewport.spec.ts` (6 tests)

## The problem

Chart state had two stores. The widget kept its own copy in `localStorage`
(`persist: true`), restoring it *inside* `createWidget`; the host then fetched the
server workspace blob and applied it over the top, with a documented
"the server wins" rule.

Both were always going to be written, so the rule could only ever paper over the
divergence:

- **Which layout you landed on came from `localStorage`.** The layout id is
  derived from the instrument, and the widget's restore supplied the instrument —
  so `localStorage` chose the layout while the server supplied its contents. The
  two were never reconciled.
- **`localStorage` carried no series fingerprint.** The whole class of bug the
  server blob was hardened against — a viewport restored over a dataset it was
  not captured on — was still live on the other path.
- **Every write went to both**, so they could disagree indefinitely.

## The change

The server blob is the only store. `persist` is gone from `createWidget`, and the
restore happens **before** the widget exists:

```
GET /api/charts/workspace            → active layout (rule below)
GET /api/charts/workspace/{id}       → { v, series, state }
probe the datalake for that instrument → the current series identity
  series matches?  ── yes ─→ restore state as-is
                   ── no ──→ restore with chart.stripView(...)
createWidget({ symbol, exchange, interval, ... })   ← no `persist`
widget.restoreState(validatedState)
mountWorkspace(widget, boot)                        ← saves only
```

Three things fall out of doing it in this order.

**Nothing to reconcile.** With one source there is no "wins" rule, no precedence
to document, and no chance of the two disagreeing. The old code raced its restore
against the first history load and needed a "wait for the bars that decide the
series" step to break the tie; that whole step is gone from the restore path.

**One load, not two.** The widget is constructed with the *stored* instrument, so
`restoreState` finds the instrument unchanged. That matters because
`restoreState` only calls `reload()` inside its `if (!same)` branch — booting with
the defaults and then restoring would have flashed the default symbol and fetched
it, then fetched the stored one. Verified in the server log: one history request
per load.

**The fingerprint check is exact.** It compares the stored series against a
datalake probe for the *stored* instrument, rather than against "whatever the
widget has loaded so far". And because that probe is the same request that seeds
the load-window clock, validating costs no extra round trip.

`localStorage` is now never read and never written. Verified on the running app:
after clearing it and reloading, `Object.keys(localStorage)` is `[]` — the app
does not recreate its keys. Pre-existing keys from the old design are inert
leftovers; nothing reads them.

## The active-layout rule

Without `localStorage` there is no record of which layout you were in, so the
host needs one. **The active layout is the most recently saved one.**

This is deliberately not a stored pointer. A layout id is derived from its own
instrument, so the last save *is* the workspace the user was in — the rule is a
property of the data rather than a second record that can drift from it. A
pointer would be another pair of things to keep consistent, which is the failure
mode this change exists to remove.

(`updated_at` is parsed, not string-compared: ISO-8601 stays lexicographic only
while every row shares one offset.)

## Drawings were the trap

The library's persistence list is split:

```ts
// widget.ts — emits `layout` (the host's save trigger) AND saves locally
['paneAdded', 'paneResized', 'paneMoved', 'paneMaximized', 'paneRemoved',
 'indicatorRemoved', 'indicatorSettings', 'priceAxisMoved', 'objects:change']

// widget.ts — saves locally only; no bus event
['draw:add', 'draw:remove', 'draw:update', 'draw:paste', 'draw:cut']
```

The draw tier emits `draw:*` and never `objects:change`, so on paper turning off
`persist` drops every drawing: they had been surviving on `localStorage`, and the
host only saved on `layout`. The test was written first and **passed** — drawings
do reach the blob today, because the draw tier feeds the objects inventory, which
emits `objects:change`.

So the coupling holds, but it is incidental, and the cost of not depending on it
is three lines. Draw events are now wired to `save()` explicitly.

## Verification

| test | covers |
|---|---|
| a divergent localStorage state cannot influence the layout | the single-store claim |
| drawings reach the single store | the trap above |
| first view of a layout is persisted | the cold-load gap |
| a viewport captured against another dataset is not restored | the guard fires |
| a viewport that matches the series is restored | the guard is a comparison |
| a legacy blob keeps the layout and drops the view | backward compatibility |

Two negative controls, both run and both discriminating:

- **`viewTrusted = state !== null`** (no fingerprint check) → the two strip tests
  fail, the rest pass.
- **`persist: true` restored in `main.ts`** → *"a divergent localStorage state
  cannot influence the layout"* fails, because the widget overwrites the seeded
  key with its own state. That is the dual-store behaviour, caught directly.

Also: 6/6 E2E, frontend typecheck + build clean, `pytest trading/tests domain/tests
brokers/tests` → 3032 passed, 2 skipped.

## Known, not addressed

**The anchor can drift backwards.** `noteAnchor` (`feed.ts`) assigns
`anchorSec = last_closed_time` for *every* history response, but that field is the
last bar **in the returned window**. Paging older history therefore reports an
*older* value and drags the load-window clock backwards, so a later interval
switch computes its window from a stale anchor. The fix is to key the anchor per
instrument and only ever move it forward — note that "forward" cannot be global
because a daily interval's newest bar is legitimately older than a 1-minute one's.
`probeDatalakeAnchor` already sidesteps this for its own callers by returning the
probe's own answer rather than the global, which is what the fingerprint check
uses. Left alone here because it is a distinct defect in the data path, not in
persistence.
