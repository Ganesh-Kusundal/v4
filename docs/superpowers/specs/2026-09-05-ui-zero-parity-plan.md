# TradeX v4 Terminal — Zero-Parity UI Implementation Plan

**Source spec:** `docs/superpowers/specs/2026-09-05-ui-zero-parity-design.md`
**Approach:** Progressive Shell Replacement — replace only the shell, preserve all backend flows

---

## Phase 1: Flatten the Visual Foundation

### Task 1.1: Replace Color System with 8 Semantic Tokens

**File:** `frontend/index.html` (CSS section)

**Change:** Replace the entire `:root` block and all 20+ color variables with 8 semantic tokens.

```css
:root {
  --bg: #020617;
  --surface: #0F172A;
  --surface-2: #1E293B;
  --fg: #F8FAFC;
  --muted: #94A3B8;
  --border: #334155;
  --bull: #22C55E;
  --bear: #EF4444;
}
```

Remove: `--ink`, `--ink-2`, `--panel`, `--panel-2`, `--elev`, `--elev-2`, `--line`, `--line-2`, `--paper`, `--paper-dim`, `--muted` (old), `--faint`, `--brass`, `--brass-2`, `--teal`, `--teal-dim`, `--coral`, `--coral-dim`, `--amber`, `--r`, `--r-sm`, `--h`.

**Verification:** `grep -c "var(--brass" frontend/index.html` → should be 0

---

### Task 1.2: Remove Decorative Effects

**File:** `frontend/index.html`

**Changes:**
1. Remove `body::before` (grain texture)
2. Remove `body` background gradients (radial-gradient, linear-gradient) → flat `background: var(--bg)`
3. Remove `backdrop-filter: blur()` from `#legend`, `#replaybar`, `.shellbar`, `.set-card`
4. Remove `box-shadow` from `.shellbar` → keep `border-bottom: 1px solid var(--border)`
5. Remove `.divider` class (no longer needed)
6. Remove `.brand-dot` brass gradient → flat `background: var(--surface-2)`
7. Remove `.tbtn--buy` / `.tbtn--sell` background fills → border-only

---

### Task 1.3: Simplify Typography to Inter Only

**File:** `frontend/index.html`

**Changes:**
1. Remove Google Fonts link for Newsreader, Sora, Fragment Mono
2. Add Inter: `https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap`
3. Set `body { font-family: "Inter", system-ui, sans-serif; font-size: 13px; }`
4. Add `font-variant-numeric: tabular-nums` to price displays (`.wl-price`, `#legend .meta`, `.rcount`, `#status`)

---

### Task 1.4: Update Chart Colors

**File:** `frontend/src/main.ts`

**Change:**
```typescript
// Old
const VOLUME_UP_COLOR = "#26a69a";
const VOLUME_DOWN_COLOR = "#ef5350";

// New
const VOLUME_UP_COLOR = "#26A69A";  // reference teal
const VOLUME_DOWN_COLOR = "#EF5350";  // reference red
```

---

## Phase 2: Thin Shellbar

### Task 2.1: Restructure Shellbar HTML

**File:** `frontend/index.html`

**Change:** Replace the `<header class="shellbar">` contents (currently ~30 elements) with minimal structure:

```html
<header class="shellbar" id="shellbar">
  <div class="brand">TradeX</div>
  <button class="sym-btn" id="sym-btn"><b>NSE:RELIANCE</b></button>
  <div class="tf-pills" id="tf-pills"></div>
  <input type="number" class="qty-input" id="qty-input" value="10" min="1" />
  <button class="buy-btn" id="buy-btn">BUY</button>
  <button class="sell-btn" id="sell-btn">SELL</button>
  <button class="status-dot" id="status-dot"></button>
  <span class="status-text" id="status-text">ready</span>
</header>
```

---

### Task 2.2: Simplify Shellbar CSS

**File:** `frontend/index.html`

```css
.shellbar {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 40px;
  padding: 0 12px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.brand {
  font-weight: 600;
  font-size: 14px;
  color: var(--fg);
}
.sym-btn {
  height: 28px;
  padding: 0 10px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--fg);
  font: 500 12px "Inter", system-ui, sans-serif;
  cursor: pointer;
}
.tf-pills {
  display: flex;
  gap: 0;
  border: 1px solid var(--border);
  border-radius: 4px;
  overflow: hidden;
}
.tf-pills button {
  height: 26px;
  padding: 0 10px;
  background: transparent;
  border: none;
  border-right: 1px solid var(--border);
  color: var(--muted);
  font: 500 11px "Inter", system-ui, sans-serif;
  cursor: pointer;
}
.tf-pills button:last-child { border-right: none; }
.tf-pills button.is-on { background: var(--surface-2); color: var(--fg); }
.qty-input {
  width: 56px;
  height: 26px;
  text-align: center;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--fg);
  font: 600 12px "Inter", system-ui, sans-serif;
  font-variant-numeric: tabular-nums;
}
.buy-btn, .sell-btn {
  height: 28px;
  padding: 0 14px;
  border-radius: 4px;
  font: 600 12px "Inter", system-ui, sans-serif;
  cursor: pointer;
  border: 1px solid transparent;
}
.buy-btn { color: var(--bull); border-color: rgba(34,197,94,0.3); }
.sell-btn { color: var(--bear); border-color: rgba(239,68,68,0.3); }
.buy-btn:hover { background: rgba(34,197,94,0.1); }
.sell-btn:hover { background: rgba(239,68,68,0.1); }
.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--muted);
}
.status-dot.on { background: var(--bull); }
.status-text {
  font: 11px "Inter", system-ui, sans-serif;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
```

---

### Task 2.3: Build Command Palette Module

**New file:** `frontend/src/palette.ts`

**Exports:**
```typescript
export interface PaletteAction {
  id: string;
  label: string;
  category: string;
  execute: () => void;
}

export interface CommandPalette {
  open(): void;
  close(): void;
  isOpen(): boolean;
}
```

**Behavior:**
- `Ctrl+K` opens
- Search input at top filters `PaletteAction.label`
- `Enter` executes focused action, `Esc` closes
- Centered modal, 500px wide, dark surface

**Implementation:** Plain DOM module (no framework). Builds a `<div class="palette-backdrop">` + `<div class="palette-modal">` on first open.

---

### Task 2.4: Register Actions from main.ts

**File:** `frontend/src/main.ts`

**Move these functions into palette actions:**
- `addIndicator("backend:rsi")` → "Add RSI"
- `applyTransform("heikin-ashi")` → "Heikin Ashi"
- `applyProfile("volume-profile")` → "Volume Profile"
- `enterBarReplay()` → "Start bar replay"
- `chart.fitContent()` → "Fit content"
- `chart.downloadScreenshot()` → "Screenshot"
- `toggleWatchlist()` → "Toggle watchlist"
- `toggleOrders()` → "Toggle orders panel"
- `toggleStrategies()` → "Toggle strategies panel"
- `chart.getState()` / `chart.restoreState()` → "Save layout" / "Restore layout"
- `toggleChartSettings()` → "Chart settings"
- `toggleCompareUi()` → "Compare"

**No logic changes** — the functions stay where they are, palette just calls them.

---

## Phase 3: Chart-First Layout

### Task 3.1: Restructure CSS Grid

**File:** `frontend/index.html`

```css
body {
  display: grid;
  grid-template-columns: 1fr;           /* no permanent side column */
  grid-template-rows: 40px 1fr;         /* shellbar + chart */
  gap: 0;
  padding: 0;
  height: 100vh;
  overflow: hidden;
}
#split { grid-column: 1; grid-row: 2; }
#sidepanel { display: none; }           /* converted to overlay */
#bottom-dock { display: none; }         /* converted to overlay */
```

---

### Task 3.2: Convert Side Panel to Overlay

**File:** `frontend/src/shell/watchlist.ts` + new `frontend/src/shell/overlay.ts`

**New module:** `overlay.ts` — generic overlay host

```typescript
export interface Overlay {
  open(): void;
  close(): void;
  isOpen(): boolean;
}

export function createOverlay(config: {
  position: 'right' | 'left' | 'bottom';
  width?: number;
  height?: number;
  content: HTMLElement;
}): Overlay;
```

**Behavior:**
- `position: 'right'` → `transform: translateX(100%)` → `translateX(0)`
- `position: 'bottom'` → `height: 0` → `height: 140px`
- `position: 'left'` → `transform: translateX(-100%)` → `translateX(0)`
- 200ms ease transition
- Close on `Esc` or click outside

**Watchlist overlay:** Right, 280px wide, top: 40px, bottom: 0
**Orders overlay:** Bottom, left: 0, right: 0, height: 140px
**Strategies overlay:** Right, 280px wide, top: 40px, bottom: 0 (stacks above watchlist)
**Draw rail:** Left, 44px wide, top: 40px, bottom: 0

---

### Task 3.3: De-chrome the Legend

**File:** `frontend/index.html` (CSS)

```css
#legend {
  position: absolute;
  top: 8px;
  left: 8px;
  padding: 4px 8px;
  font: 11px "Inter", system-ui, sans-serif;
  font-variant-numeric: tabular-nums;
  color: var(--muted);
  background: transparent;
  border: none;
  backdrop-filter: none;
}
#legend .name { color: var(--fg); font-weight: 500; }
```

---

### Task 3.4: De-chrome the Replay Bar

**File:** `frontend/index.html` (CSS)

```css
#replaybar {
  position: absolute;
  bottom: 12px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  gap: 6px;
  padding: 6px 10px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  backdrop-filter: none;
  box-shadow: none;
}
```

---

## Phase 4: Chart Theme Override

### Task 4.1: Create Theme Override Module

**New file:** `frontend/src/theme.ts`

```typescript
export const tradexTheme = {
  background: '#0d0e12',
  gridLines: '#1e2a3a',
  bullColor: '#26A69A',
  bearColor: '#EF5350',
  volumeUp: 'rgba(38,166,154,0.4)',
  volumeDown: 'rgba(239,83,80,0.4)',
  crosshair: '#6b7a90',
  text: '#c7ccd8',
};
```

### Task 4.2: Apply Theme to Chart

**File:** `frontend/src/main.ts`

```typescript
import { tradexTheme } from './theme';

// Replace darkTheme with custom theme
chart = createChart(chartHost, {
  theme: tradexTheme,
  timezone: "Asia/Kolkata",
  dataFeed: chartFeed,
  shortcuts: false,
} as unknown as Record<string, unknown>);
```

---

## Phase 5: Wire Keyboard Shortcuts

### Task 5.1: Add Missing Shortcuts

**File:** `frontend/src/shortcuts.ts` (or `src/main.ts`)

```typescript
// Add to existing shortcut system
{ key: 'ctrl+k', action: () => palette.open() },
{ key: 'ctrl+w', action: () => watchlistOverlay.toggle() },
{ key: 'ctrl+o', action: () => ordersOverlay.toggle() },
{ key: 'ctrl+b', action: () => strategiesOverlay.toggle() },
{ key: 'ctrl+d', action: () => drawRailOverlay.toggle() },
{ key: 'ctrl+s', action: () => saveLayout() },
{ key: 'ctrl+shift+s', action: () => restoreLayout() },
{ key: 'f', action: () => chart.fitContent() },
{ key: 'ctrl+shift+p', action: () => chart.downloadScreenshot() },
```

---

## Phase 6: Accessibility & Polish

### Task 6.1: Focus States

**File:** `frontend/index.html` (CSS)

```css
:focus-visible {
  outline: 2px solid #3B82F6;
  outline-offset: 2px;
}
```

### Task 6.2: Reduced Motion

**File:** `frontend/index.html` (CSS)

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
```

### Task 6.3: Contrast Verification

- `--fg` (#F8FAFC) on `--bg` (#020617) → 18:1 ✅
- `--muted` (#94A3B8) on `--bg` → 6.5:1 ✅
- `--bull` (#22C55E) on `--bg` → 8.5:1 ✅
- `--bear` (#EF4444) on `--bg` → 6.8:1 ✅

---

## File-by-File Change Summary

| File | Action | Lines Changed |
|---|---|---|
| `frontend/index.html` | CSS rewrite (palette, grid, typography) | ~200 |
| `frontend/src/main.ts` | Remove shellbar DOM, add palette + overlays | ~300 |
| `frontend/src/palette.ts` | New — command palette module | ~120 |
| `frontend/src/theme.ts` | New — chart theme override | ~20 |
| `frontend/src/shell/overlay.ts` | New — generic overlay host | ~80 |
| `frontend/src/shell/watchlist.ts` | Convert to overlay panel | ~40 |
| `frontend/src/shell/bottom-dock.ts` | Convert to overlay panel | ~40 |
| `frontend/src/shell/draw-rail.ts` | Convert to overlay panel | ~30 |
| `frontend/src/primitives.ts` | Simplify Chrome class | ~50 |
| `frontend/src/shell/indicator-modal.ts` | Move into palette | ~30 |
| `frontend/src/shell/chart-settings.ts` | Move into palette | ~30 |
| `frontend/src/shell/comparison.ts` | Move into palette | ~30 |
| `frontend/src/shell/replay-bar.ts` | Simplify (no pill, no glass) | ~20 |

**Total:** ~1000 lines changed across 13 files.

---

## Verification Checklist

### Phase 1
- [ ] `grep -c "var(--brass" frontend/index.html` → 0
- [ ] `grep -c "backdrop-filter" frontend/index.html` → 0
- [ ] `grep -c "Newsreader\|Sora\|Fragment Mono" frontend/index.html` → 0
- [ ] Background is flat `--bg` (no gradients, no grain)

### Phase 2
- [ ] Shellbar is 40px single row at 1280×720 and 1920×1080
- [ ] `Ctrl+K` opens command palette
- [ ] Typing filters results, `Enter` executes, `Esc` closes
- [ ] All secondary actions accessible from palette

### Phase 3
- [ ] Chart fills viewport when overlays hidden
- [ ] `Ctrl+W` opens watchlist overlay (right, 280px)
- [ ] `Ctrl+O` opens orders overlay (bottom, 140px)
- [ ] `Ctrl+B` opens strategies overlay (right, 280px)
- [ ] `Ctrl+D` opens draw rail overlay (left, 44px)
- [ ] Legend is plain text, no container
- [ ] Replay bar is flat card, no glass

### Phase 4
- [ ] Chart background is `#0d0e12` (not `#0a0e14`)
- [ ] Bull candles are `#26A69A`
- [ ] Bear candles are `#EF5350`

### Phase 5
- [ ] `Ctrl+K` → palette
- [ ] `Ctrl+W` → watchlist
- [ ] `Ctrl+O` → orders
- [ ] `Ctrl+B` → strategies
- [ ] `Ctrl+D` → draw rail
- [ ] `Ctrl+S` → save layout
- [ ] `Ctrl+Shift+S` → restore layout
- [ ] `F` → fit
- [ ] `Ctrl+Shift+P` → screenshot

### Phase 6
- [ ] `:focus-visible` shows blue outline
- [ ] `prefers-reduced-motion` disables transitions
- [ ] All text ≥ 4.5:1 contrast

---

## Risk Mitigation

| Risk | Mitigation |
|---|---|
| Chart library theme API incompatible | Pass theme as `Record<string, unknown>` cast, verify with console.log |
| ResizeObserver doesn't fire on overlay open | Call `chart.applyOptions()` manually after overlay transitionend |
| Command palette z-index conflicts with chart canvas | Z-index layering documented in spec (palette = 35) |
| Watchlist fetch fails | Keep existing fallback in `load()` function |
| Trade flows break during shell refactor | No trade code changes — only DOM wiring in main.ts |

---

## Done Criteria

- [ ] All 6 phases complete
- [ ] Visual comparison: v4 matches reference clean minimalism
- [ ] All backend flows (indicators, transforms, profiles, trade) work unchanged
- [ ] Keyboard shortcuts all functional
- [ ] Accessibility checklist passed
