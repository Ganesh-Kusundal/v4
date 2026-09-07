# TradeX v4 Terminal — Zero-Parity UI/UX Design Spec

**Status:** Draft for review
**Date:** 2026-09-05
**Reference:** `openalgo-charts-master` (clean charting UI)
**Approach:** Progressive Shell Replacement — replace only the shell, preserve all backend flows

---

## 1. Architecture Overview

### 1.1 Current State

The frontend has clean module boundaries with backend flows centralized in dedicated modules:

- `src/feed.ts` — Data feed to backend (`/api/charts/history`, `/api/charts/symbols`)
- `src/trade.ts` — Trading host wiring
- `src/trade/order-engine.ts` — Order state machine (SUBMITTED → ACKNOWLEDGED, idempotency, uncertain tracking)
- `src/trade/trade-controller.ts` — Book reconciliation into primitives
- `src/trade/bracket.ts`, `order-line.ts`, `position.ts`, `pnl.ts`, `validation.ts` — trade primitives
- `src/trade-feed.ts` — Trade WebSocket + order placement flow
- `src/panels/strategies.ts` — Strategy catalogue + equity curve
- `src/backend-indicators.ts` — Indicator catalogue + Tier-2 compute
- `src/transforms.ts` — Chart-type transforms (Heikin Ashi, Renko, etc.)
- `src/profiles.ts` — Volume profile / TPO / footprint
- `src/seasonality.ts` — Seasonality overlay
- `src/shortcuts.ts` — Keyboard shortcut host
- `src/linking.ts` — Cross-chart link group
- `src/http.ts` — Fetch wrapper

The shell around these flows is currently:
- `index.html` — 390 lines of inline CSS and HTML chrome
- `src/main.ts` — 1000+ lines of shell construction
- `src/primitives.ts` — Chrome class with glass, brass, grain

### 1.2 Target State

Replace only the shell modules. The backend flows remain untouched.

**What changes:**
- `index.html` — strip all inline CSS/HTML chrome, become a minimal `<div id="chart">` host
- `src/main.ts` — rewire shell construction, add command palette, remove shellbar building
- `src/primitives.ts` — gut the `Chrome` class (no glass, no brass, no grain)
- `src/shell/watchlist.ts` — convert to overlay panel
- `src/shell/bottom-dock.ts` — convert to auto-hide overlay
- `src/shell/indicator-modal.ts` — move into command palette
- `src/shell/chart-settings.ts` — move into command palette
- `src/shell/comparison.ts` — move into command palette
- `src/shell/replay-bar.ts` — simplify (no pill, no glass)
- `src/shell/draw-rail.ts` — convert to toggleable overlay
- New: `src/palette.ts` — command palette module
- New: `src/theme.ts` — chart theme override for openalgo-charts

**What stays (preserved as-is):**
- `src/feed.ts`, `src/trade.ts`, `src/trade/*.ts`, `src/trade-feed.ts`
- `src/panels/strategies.ts`, `src/backend-indicators.ts`
- `src/transforms.ts`, `src/profiles.ts`, `src/seasonality.ts`
- `src/shortcuts.ts`, `src/linking.ts`, `src/http.ts`

### 1.3 Component Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ App Shell (main.ts)                                             │
│  ├─ createChart() with theme override                           │
│  ├─ Shellbar (brand, symbol, TF, qty, buy, sell, status)        │
│  ├─ Command Palette (Ctrl+K)                                    │
│  ├─ Overlay Panels (watchlist, orders, strategies)              │
│  ├─ Draw Rail (Ctrl+D)                                          │
│  └─ Keyboard Shortcuts                                          │
│                                                                 │
│ Data Flows (preserved)                                          │
│  ├─ feed.ts → /api/charts/history, /api/charts/symbols          │
│  ├─ trade-feed.ts → POST /orders, WS /ws/stream                 │
│  ├─ backend-indicators.ts → POST /api/charts/indicators/compute │
│  ├─ transforms.ts → POST /api/charts/transforms/*              │
│  ├─ profiles.ts → POST /api/charts/profiles/compute            │
│  └─ strategies.ts → POST /api/charts/strategies/*              │
│                                                                 │
│ Trade Primitives (preserved)                                    │
│  ├─ order-engine.ts → state machine + idempotency               │
│  ├─ trade-controller.ts → book reconciliation                   │
│  └─ order-line.ts, position.ts, bracket.ts, pnl.ts             │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. Command Palette

### 2.1 Purpose

Replace the dense shellbar with a `Ctrl+K` command palette. The shellbar shrinks to ~10 essential controls; everything else lives in the palette.

### 2.2 Shellbar (What Stays Visible)

```
[TradeX]  NSE:RELIANCE  [1m] [5m] [15m] [1D]  Qty [10]  BUY  SELL  ● LIVE
```

- Brand (plain text, no brass gradient)
- Symbol button (click → opens watchlist overlay)
- TF pills (flat, no brass)
- Qty input (compact)
- BUY / SELL (colored border + text, no fill)
- Status dot + text

### 2.3 Palette Structure

A modal overlay centered on screen. Search input at top, results below. Categories:

| Category | Actions |
|---|---|
| **Indicators** | Add RSI, Add MACD, Add EMA, Add Bollinger, Add VWAP, … (from catalogue) |
| **Transforms** | Candles, Heikin Ashi, Renko, Range, Line Break, Point & Figure, Kagi |
| **Profiles** | Volume Profile, Market Profile (TPO), Footprint, Seasonality |
| **Compare** | Add comparison symbol |
| **Replay** | Start bar replay, Start tick replay, Exit replay |
| **Layout** | Save layout, Restore layout, Fit content |
| **Settings** | Chart settings, Toggle grid, Toggle crosshair magnet |
| **View** | Toggle watchlist, Toggle orders panel, Toggle strategies panel |

### 2.4 Behavior

- Type to filter (e.g., "ren" → "Transform: Renko")
- `Enter` executes, `Esc` closes
- Each action maps to an existing function in `main.ts` (no new logic)
- Actions that need a modal (indicator settings, chart settings, compare) open their existing modal after palette closes

### 2.5 Keyboard Shortcuts (Preserved from `src/shortcuts.ts`)

- `Ctrl+K` — command palette
- `Ctrl+W` — toggle watchlist
- `Ctrl+O` — toggle orders panel
- `Ctrl+B` — toggle strategies panel
- `Ctrl+S` — save layout
- `Ctrl+Shift+S` — restore layout
- `F` — fit content
- `Ctrl+Shift+P` — screenshot
- Arrow keys, `+`/`-` — pan/zoom

---

## 3. Chart Theme Override

### 3.1 Palette (Financial Dashboard from skill)

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

### 3.2 Chart Canvas Override

Pass a custom theme object to `createChart()`:

| Element | Reference | Library Default |
|---|---|---|
| Background | `#0d0e12` | `#0a0e14` |
| Grid lines | `#1e2a3a` | library default |
| Bull candle | `#26A69A` | library default |
| Bear candle | `#EF5350` | library default |
| Volume up | `#26A69A` 40% opacity | `#26a69a` |
| Volume down | `#EF5350` 40% opacity | `#ef5350` |
| Crosshair | `#6b7a90` | library default |
| Text | `#c7ccd8` | library default |

### 3.3 Removed Decorative Effects

- SVG grain texture (`body::before`)
- `backdrop-filter: blur()` on legend, replay bar, shellbar
- Brass gradients
- `box-shadow` on shellbar
- Radial gradients on body

---

## 4. Overlay Panels

### 4.1 Layout Change

**Current:** `grid-template-columns: minmax(0,1fr) 320px` — permanent side panel.
**Target:** `grid-template-columns: 1fr` — chart only. Panels overlay on top.

### 4.2 Watchlist Overlay

- **Trigger:** `Ctrl+W` or click symbol button in shellbar
- **Position:** fixed right, top: 40px, bottom: 0, width: 280px
- **Behavior:** `transform: translateX(100%)` → `translateX(0)` with 200ms ease
- **Content:** search input + symbol list (from `/api/charts/symbols?universe=nifty500`)
- **Active state:** highlight selected row
- **No price column** (reference doesn't show live prices in watchlist — only symbol + exchange)

### 4.3 Orders Panel

- **Trigger:** `Ctrl+O`
- **Position:** fixed bottom, left: 0, right: 0, height: 140px (auto-hide)
- **Behavior:** `height: 0` → `height: 140px` with 200ms ease
- **Content:** tabbed (Orders / Logs / Scanner / Backtest)
- **Orders table:** side, symbol, type, qty, price, status (from `/api/charts/book` via `tradeFeed.subscribeOrders`)
- **Logs:** existing `logLine()` output

### 4.4 Strategies Panel

- **Trigger:** `Ctrl+B`
- **Position:** fixed right (below watchlist), top: 40px, bottom: 0, width: 280px
- **Behavior:** same as watchlist but on top of it (z-index stacking)
- **Content:** strategy catalogue + scanner results + backtest equity curve

### 4.5 Draw Rail

- **Trigger:** `Ctrl+D` or icon in shellbar
- **Position:** fixed left, top: 40px, bottom: 0, width: 44px
- **Content:** drawing tool buttons (vertical stack)

### 4.6 Z-Index Layering

```
0   — chart canvas
10  — crosshair, legend
15  — draw rail (left)
20  — side overlays (right)
25  — bottom overlay
30  — command palette backdrop
35  — command palette modal
```

### 4.7 Chart Resize

When any overlay opens, the chart's ResizeObserver detects the container shrinking and calls `chart.applyOptions({ width, height })`. When overlay closes, chart expands back. No manual resize wiring needed.

---

## 5. Preserved Backend Flows

### 5.1 Indicator Flow (Preserved Exactly)

```
User action → Ctrl+K → "Add RSI" → chart.addIndicator("backend:rsi", { symbol, exchange, interval })
  → library calls registered backend indicator
  → backend indicator calls POST /api/charts/indicators/compute (existing)
  → response → series data rendered
```

The UI never computes an indicator. `backend-indicators.ts` stays the single registry.

### 5.2 Transform Flow (Preserved Exactly)

```
User action → Ctrl+K → "Heikin Ashi" → applyTransform("heikin-ashi")
  → computeTransform() → POST /api/charts/transforms/heikin-ashi (existing)
  → response → new bar series rendered
```

The UI never computes a transform. `transforms.ts` stays the single registry.

### 5.3 Profile Flow (Preserved Exactly)

```
User action → Ctrl+K → "Volume Profile" → applyProfile("volume-profile")
  → computeProfile() → POST /api/charts/profiles/compute (existing)
  → response → chart.addPrimitive(profilePrimitive)
```

The UI never computes a profile. `profiles.ts` stays the single registry.

### 5.4 Trade Flow (Preserved Exactly)

```
User action → BUY/SELL button → tradeHost.orderEngine.placeOrder({ symbol, exchange, side, type, qty })
  → TradexTradeFeed.place() → POST /orders (existing)
  → WS order-update → tradeFeed.subscribeOrders() → TradeController.reconcile() → primitives update
```

The UI never holds order state. `order-engine.ts` and `trade-controller.ts` stay the single source of truth.

### 5.5 Replay Flow (Preserved Exactly)

```
User action → Ctrl+K → "Start bar replay" → enterBarReplay()
  → ReplayController(chart, { series, bars, speed, onFrame })
```

Replay is driven by existing `ReplayController` from the library. No new logic.

### 5.6 What This Means

The UI shell after this redesign is ~30% of its current size. It becomes:
1. **Chart host** — creates the chart, handles resize
2. **Shellbar** — minimal controls (symbol, TF, qty, buy, sell, status)
3. **Command palette** — search-driven access to all secondary actions
4. **Overlay panels** — watchlist, orders, strategies (toggleable)
5. **Theme** — CSS variables + chart theme override

Every computation, every data fetch, every state machine stays in the existing modules. The UI only wires user intent to existing functions.

---

## 6. Implementation Plan

### 6.1 Phase 1: Flatten the Visual Foundation (Day 1)

**Goal:** Remove noise, establish clean dark theme.

- Replace 20+ color variables with 8 semantic tokens
- Remove grain texture (`body::before`)
- Remove all `backdrop-filter: blur()`
- Remove brass gradients → flat fills
- Reduce `box-shadow` usage → flat borders
- Set `font-family: "Inter", system-ui, sans-serif` on body
- Remove Newsreader, Sora, Fragment Mono imports
- Add `font-variant-numeric: tabular-nums` to price displays

### 6.2 Phase 2: Thin Shellbar (Day 1-2)

**Goal:** 40px top bar, ~10 visible controls, command palette for rest.

- Shellbar: reduce to brand + sym + TF + qty + buy + sell + overflow + status
- Move indicators/transforms/profiles/replay/compare/settings to command palette
- Build command palette module (`src/palette.ts`)

### 6.3 Phase 3: Chart-First Layout (Day 2)

**Goal:** Chart fills viewport, panels overlay or auto-hide.

- CSS Grid restructure: `grid-template-columns: 1fr`
- Side panel: `position: fixed`, hidden by default, toggle with `Ctrl+W`
- Bottom dock: `position: fixed`, height 0, toggle with `Ctrl+O`
- Legend: remove glass card, plain text only
- Replay bar: remove pill shape + blur, flat card

### 6.4 Phase 4: De-chrome Overlays (Day 2-3)

**Goal:** Overlays are minimal, not competing with chart.

- Watchlist: dark surface, no glass, plain rows
- Orders: tabular, compact rows
- Strategies: compact list
- Draw rail: flat buttons, no brass

### 6.5 Phase 5: Control Refinement (Day 3)

**Goal:** Every control is compact, clear, and quiet.

- Buy/Sell buttons: colored border + text, no fill by default
- TF pills: flat, no brass, no glow
- Indicator chips: compact (existing chips in palette)
- Status bar: inline, no pills

### 6.6 Phase 6: Accessibility & Polish (Day 3-4)

- Focus states: `outline: 2px solid var(--accent)`
- Reduced motion: disable animations
- Contrast verification: all text ≥ 4.5:1
- Keyboard nav: tab order matches visual order
- Performance: no layout thrash on resize

---

## 7. Testing Strategy

### 7.1 Manual Testing

1. **Shellbar:** all 10 controls visible at 1280×720 and 1920×1080
2. **Command palette:** `Ctrl+K` opens, filter works, `Enter` executes, `Esc` closes
3. **Overlay panels:** each toggles open/closed without chart resize issues
4. **Indicator add:** add RSI → chart renders RSI pane (existing backend flow)
5. **Transform:** Heikin Ashi → chart updates (existing backend flow)
6. **Profile:** Volume Profile → primitive renders (existing backend flow)
7. **Trade:** BUY → order placed → order line appears (existing backend flow)
8. **Replay:** Start bar replay → transport renders (existing ReplayController)
9. **Theme:** background is near-black `#020617`, no grain, no glass
10. **Keyboard:** all shortcuts functional, tab order correct

### 7.2 Visual Regression

- Screenshot comparison: v4 vs reference side-by-side
- Chart canvas: verify `#0d0e12` background (not `#0a0e14`)
- Legend: plain text, no container
- Shellbar: 40px single row, no wrapping

---

## 8. Success Criteria

| Metric | Current | Target |
|---|---|---|
| Shellbar height | ~50px (wrapping) | 40px (single row) |
| Visible controls in shellbar | 26+ | ~10 |
| Font families | 3 | 1 (+ mono for numbers) |
| Color variables | 20+ | 8 |
| Chart area (% of viewport) | ~55% | ~90% (panels hidden) |
| Decorative effects | grain + glass + gradients | none |
| Side panel | permanent 320px | toggleable overlay |
| Bottom dock | permanent 140px | auto-hide 0-140px |
| Visual noise | high | minimal |

---

## 9. Out of Scope

- DOM depth ladder (separate feature, not part of zero-parity)
- Multi-pane support beyond price + volume (separate feature)
- Profile pane separation (separate feature)
- New drawing tools (separate feature)
- Indicator/profile/transform library changes (UI-only change)
- Chart library source modifications (theme override only)
