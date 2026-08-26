# P4 Task 2 Report — keyboard shortcuts (shortcuts.ts)

- **Status:** DONE
- **Date:** 2026-08-26
- **Commit:** (see git log — this report is authored after the code commit)

## Scope decision: `global` (not `hover`)

`createShellShortcuts` defaults to `scope: "global"` and `main.ts` calls it with no override.

Rationale: this is a terminal shell, not a single-chart page. The library default
`'hover'` only resolves keys while the pointer is over the chart (or the chart is
focused), which leaves the shellbar, panels, watchlist and dock dead on the
keyboard. `'global'` makes the keymap act anywhere in the shell. To keep that from
hijacking typing, the `start()` handler calls the library's own
`ShortcutManager.shouldIgnore(ev.target)` and bails when the target is an
input/select/textArea/contentEditable (symbol search, bracket ticket, qty field,
indicator filter). That is a library helper, not reimplemented logic.

## Single-owner wiring (the key library surprise)

The reference `Chart` **already owns** a `ShortcutManager` plus a document-level
`keydown` handler (`Chart._onKeyDown` → `_runShortcut`) that implements every
command in `DEFAULT_KEYMAP`. `main.ts` created the chart with no `shortcuts`
option, so the chart silently had the built-in keymap active in `'hover'` scope.

If the shell had simply added its own global handler on top, every key pressed
over the chart would fire **twice** (once via the shell's window handler, once via
the chart's document handler). Fix: the chart is now created with
`shortcuts: false`, making the shell's `createShellShortcuts` handler the single
owner of key routing. Nothing else in the shell references `chart.shortcuts`.

## Per-command mapping (all 14 commands — no no-ops)

Every `DEFAULT_KEYMAP` command routes to an **existing, public** chart op. The
brief allowed no-ops where the shell lacked a handler, but the reference chart
exposes a public handler for all of them, so none were left as no-ops. No chart
feature was invented — everything below is public API verified in
`node_modules/openalgo-charts/dist/index.d.ts`.

| Command | Key(s) | Shell action |
|---|---|---|
| `panLeft` | `←` | shift visible logical range −10 bars |
| `panRight` | `→` | shift visible logical range +10 bars |
| `panLeftFast` | `⌘←` / `Ctrl+←` | shift −50 bars |
| `panRightFast` | `⌘→` / `Ctrl+→` | shift +50 bars |
| `panUp` | `↑` | `chart.panes()[0].priceScale.panByPixels(20)` |
| `panDown` | `↓` | `chart.panes()[0].priceScale.panByPixels(-20)` |
| `zoomIn` | `=` / `Shift+=` / `Num+` | `chart.timeScale.zoomAtX(width/2, 1.1)` |
| `zoomOut` | `-` / `Num-` | `chart.timeScale.zoomAtX(width/2, 1/1.1)` |
| `resetScale` | `Home` / `0` | `chart.setVisibleLogicalRange({ from: 0, to: lastRawBars.length + 5 })` (mirrors `main.ts` reset view) |
| `fitContent` | `Alt+F` | `fitChart()` — shared helper, same call as the Fit button |
| `screenshot` | `Alt+Shift+S` | `chart.downloadScreenshot()` (documented in the reference as "what the screenshot keyboard shortcut runs") |
| `toggleGridVert` | `Alt+V` | `chart.setGridOptions({ vertLines: !chart.gridOptions().vertLines })` |
| `toggleGridHorz` | `Alt+H` | `chart.setGridOptions({ horzLines: !chart.gridOptions().horzLines })` |
| `toggleCrosshairMagnet` | `Alt+M` | `chart.applyOptions({ crosshairMode: magnet ? "normal" : "magnet" })` |

Notes:

- Pan uses `chart.getVisibleLogicalRange()`/`chart.setVisibleLogicalRange()`
  (chart-level), which delegate to the `timeScale` **and** emit the viewport-moved
  event — so an arrow-key pan announces itself to the LinkGroup exactly like a
  drag, matching the reference's `_runShortcut` intent.
- `zoomAtX` anchors at the plot's horizontal centre (`timeScale.width / 2`), with
  the reference's `1.1` / `1/1.1` factors.
- `panUp`/`panDown` move the **price** scale (the reference does
  `_panes[0].priceScale.panByPixels(±20)`), not the time range — a time-range shift
  would be the wrong semantics for a vertical pan.
- `toggleCrosshairMagnet` toggles the **chart's** crosshair snap via
  `chart.crosshairMode()` + `applyOptions`. It is deliberately NOT wired to the
  draw-rail `#magnet` checkbox, which governs the drawing controller's magnet (a
  different feature).

## Fit button touch (small correctness fix, same call shared)

The brief said `fitContent` should "reuse the same call as the Fit button". The
existing Fit button called `timeScale.fitContent()` **with no argument**; the
`TimeScale.fitContent(barCount)` requires a count and computes
`baseIndex = barCount - 1`, so a bare call produces `NaN` geometry (a latent bug).
The button and the shortcut now share one helper `fitChart()` that calls
`chart.fitContent()` — the chart-level no-arg convenience that sizes to the loaded
bars (`dataLayer.length`). Same intent, correct, and typed without the old cast.

## shellbar "Keys" hint

`createShellShortcuts` exposes `list()` (command/label/combos from
`DEFAULT_KEYMAP`) so a future "Keys" hint can render the bindings. No shellbar
pill was added this task — the shellbar is already crowded and the hint is
optional per the brief; the surface is there when Task 4 wants it.

## Verify output

```
$ cd frontend
$ npm run typecheck
> tsc --noEmit            # exit 0, no output

$ npm run build
> vite build
vite v6.4.3 building for production...
transforming...
✓ 25 modules transformed.
rendering chunks...
computing gzip size...
dist/index.html                 25.21 kB │ gzip:   5.97 kB
dist/assets/index-DWy3_9-P.js  433.96 kB │ gzip: 116.14 kB │ map: 1,160.43 kB
✓ built in 633ms
```

Both green.

## Library-API surprises

1. **The chart ships its own shortcut engine.** `ShortcutManager` +
   `DEFAULT_KEYMAP` are base-bundle exports and `Chart` already instantiates a
   `ShortcutManager` and runs every keymap command through `_runShortcut`. A
   shell-level handler must therefore pass `shortcuts: false` at `createChart`
   (or configure the chart's own manager) or every key fires twice. This is the
   decision the brief's "DECIDE and document" callout pointed at, and it is
   documented in a code comment at the `createChart` call.
2. **`manager.resolve(ev)` needs no cast.** `ShortcutManager.resolve` takes the
   non-exported `KeyLike` (`{ code?, key?, ctrlKey?, metaKey?, altKey?,
   shiftKey? }`); a real `KeyboardEvent` satisfies it structurally, so the brief's
   `ev as never` is unnecessary — `resolve(ev)` typechecks and `eventToCombo`
   maps `event.code` combos (`ArrowLeft`, `Equal`, `Digit0`, `Home`,
   `Alt+KeyF`, …) exactly as `DEFAULT_KEYMAP` declares them.
3. **`scope: 'global'` is a first-class option** (`ShortcutScope = 'hover' |
   'global'`), so no adaptation was needed for the shell.
4. **screenshot / grid / crosshair-magnet are public, not no-ops.** The brief
   hedged them as likely no-ops, but the chart exposes `downloadScreenshot()`,
   `setGridOptions()`/`gridOptions()`, and `applyOptions({ crosshairMode })` +
   `crosshairMode()`. All wired.
5. **`TimeScale.fitContent(barCount)` requires its argument** — the old Fit
   button's bare call was a latent NaN bug; fixed via the chart-level
   `fitContent()` convenience (see above).

## Commit

```
git add frontend/src/shortcuts.ts frontend/src/main.ts
git commit -m "feat(ui): keyboard shortcuts — ShortcutManager onto shell actions"
```

`frontend/index.html` is listed in the brief's stage command but is unchanged
(the keymap needs no DOM), so it is a no-op in `git add`. Only frontend files
were staged; `shell/*`, `trade-feed.ts`, `feed.ts`, `transforms.ts`,
`profiles.ts`, `seasonality.ts`, `trade.ts`, and all backend files were untouched.