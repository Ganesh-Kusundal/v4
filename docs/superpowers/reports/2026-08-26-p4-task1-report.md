# P4 Task 1 Report — chart linking (linking.ts)

- **Status:** DONE
- **Date:** 2026-08-26
- **Commit:** (see git log — this report is authored after the code commit)

## Scope correction applied

The plan's Task 1 text ("bind primary to comparison panes") is wrong and was **not** implemented. The comparison overlay (`shell/comparison.ts`) adds series to the **same** chart via `comparisonController`/`addComparison` — it does not create separate `Chart` instances. `LinkGroup` links separate chart instances, so:

- The link group binds **ONE chart (the primary)** today — a valid one-member `LinkGroup`; crosshair/viewport sync on a single member are no-ops, but the wiring is real and parity-correct with the reference.
- No multi-chart grid was built (YAGNI). Comparison panes were NOT "linked" as charts — they aren't separate charts.
- The module is structured (`linkChart(chart)` → `ensureLinkGroup().add(chart)`) so a future host that owns multiple charts can add each with one call.

## How the primary chart is linked

`frontend/src/linking.ts` (new module):

```ts
import { createLinkGroup, LinkGroup, type LinkChart } from "openalgo-charts";

let group: LinkGroup | null = null;

export function ensureLinkGroup(): LinkGroup {
  if (group) return group;
  group = createLinkGroup({ crosshair: true, viewport: true, symbol: false, whenMissing: "nearest" });
  return group;
}

export function linkChart(chart: LinkChart): void {
  ensureLinkGroup().add(chart);
}

export function unlinkAll(): void {
  group?.destroy();
  group = null;
}
```

- Group options: `crosshair: true`, `viewport: true`, `symbol: false`, `whenMissing: "nearest"` — symbol mirroring is off (the shell's single chart has no peer to share an instrument with), matching the reference's default posture.
- `linkChart(chart)` is called in `frontend/src/main.ts` immediately after `createChart(...)` returns, so the primary is a member from boot.
- Symbol changes need no special handling: the group follows its member automatically and `symbol` sync is off (viewport/crosshair linking does not depend on instrument).
- The brief's snippet typed the chart as `unknown` and cast to `never`; I used the exported `LinkChart` type instead — `Chart` satisfies it structurally (verified in the library d.ts), so `linkChart(chart as never)` in `main.ts` is just because `createChart`'s return is otherwise cast through `Record<string, unknown>` in this codebase.

## Link toggle wiring

A self-contained shellbar toggle (mirrors the Compare button pattern in `main.ts` — no separate modal module needed since `LinkGroup` ships no DOM):

```ts
let linked = true;
const linkBtn = document.createElement("button");
linkBtn.className = "tbtn is-on";
linkBtn.textContent = "Link";
linkBtn.title = "Chart linking — LinkGroup (crosshair + viewport mirroring)";
linkBtn.addEventListener("click", () => {
  linked = !linked;
  linkBtn.classList.toggle("is-on", linked);
  if (linked) linkChart(chart as never); else unlinkAll();
});
```

Appended to `#shellbar` after `cmpBtn, setBtn` in the `shellbar.append(...)` call. Default state is **on** (chart linked at boot); toggling off calls `unlinkAll()` (destroys the group, releases listeners/crosshairs), toggling on re-creates the group and re-adds the chart. `index.html` needed no change — the button is created entirely in `main.ts`, same as Compare/Settings.

## Verify output

```
$ cd frontend
$ npm run typecheck
> tsc --noEmit            # exit 0, no output

$ npm run build
> vite build
vite v6.4.3 building for production...
transforming...
✓ 24 modules transformed.
rendering chunks...
computing gzip size...
dist/index.html                 25.21 kB │ gzip:   5.97 kB
dist/assets/index-PCTkLlT_.js  432.66 kB │ gzip: 115.60 kB │ map: 1,153.88 kB
✓ built in 746ms
```

Both green.

## Library-API surprises

1. **`LinkGroup` has NO `unlink()` method.** The brief's provided snippet calls `group?.unlink()` — that method does not exist on the class (verified against the reference source `src/link/group.ts` and the installed `dist/index.d.ts`). The correct teardown is `destroy()` ("Unlink everything: no listeners, no linked crosshairs, no references"), which `unlinkAll()` uses.
2. **`whenMissing: "nearest"` needs no cast.** The options type is `LinkMissingPolicy = 'nearest' | 'hide'`, so the literal is accepted as-is.
3. **`createLinkGroup`/`LinkGroup`/`LinkChart` are real base-bundle exports** (confirmed in `dist/openalgo-charts.mjs` and `dist/index.d.ts`) — no reimplementation needed.
4. **`group.add(chart)` double-adds are safe** — the reference updates member options instead of double-subscribing, so the boot-time `linkChart` plus a later toggle re-add can never leak listeners.

## Commit

```
git add frontend/src/linking.ts frontend/src/main.ts
git commit -m "feat(ui): chart linking — LinkGroup over the primary chart"
```

`frontend/index.html` was listed in the brief's stage command but is unchanged (the toggle is created in `main.ts`), so it is a no-op in `git add`. Only frontend files were staged; `shell/comparison.ts`, `trade-feed.ts`, `feed.ts`, `transforms.ts`, `profiles.ts`, `seasonality.ts`, `trade.ts`, and all backend files were untouched.