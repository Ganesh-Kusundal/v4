# P3 Task 3 Report — bracket order UI form

**Status:** DONE

## Summary
Added a bracket order form (entry / stop-loss / target inputs + BUY/SELL buttons) to the shellbar, placing super-orders through the Task 1 backend endpoint `POST /orders/bracket`, then repainting the on-chart bracket via the Task 2 host.

## Form wiring
- **`frontend/src/trade.ts`** — added `placeBracket(opts)` exactly per the plan/brief spec: POSTs `{exchange, symbol, side, order_type: "LIMIT", quantity, price, stop_loss_price, target_price}` to `/orders/bracket`, throws on non-OK (surfacing status + response text), resolves with `order_id`. All existing exports (`BookRow`, `BookPosition`, `mapOrdersToTrading`, `mapPositionsToTrading`, `TradingHostHandle`, `createTradingHost`) left intact.
- **`frontend/src/main.ts`** — bracket ticket built next to the existing buy/sell order buttons, mirroring the `placeOrder` wiring:
  - `brkEntry` / `brkStop` / `brkTarget` number inputs (class `field--brk`) + `brkBuyBtn` / `brkSellBtn` (classes `tbtn--buy`/`tbtn--sell`), plus a `pill--brk` "BRK" label; appended into `shellbar.append(...)` between the trade toggle and the compare/settings cluster.
  - `submitBracket(side)` reads the shared `qtyInput` for quantity, validates all three prices (`Number.isFinite` and `> 0`) before sending, calls `placeBracket({exchange, symbol, side, quantity, price, stopLoss, target})`, then on success calls `fetchBook()` → `tradeHost.sync(book)` to repaint the on-chart bracket.
- **`frontend/index.html`** — added `.field--brk` (matches `.field--qty` styling, placeholder + focus states) and `.pill--brk`.

## Current-price prefill
- Extracted `currentPrice()` in `main.ts` — returns the last loaded bar's close from `lastRawBars` (the same source the Task 2 host's `getLtp` closure used), `undefined` when no bars are loaded.
- The `createTradingHost` `getLtp` closure was refactored to reuse `currentPrice()` (same semantics, no behavior change).
- `prefillBracket()` fills `brkEntry` from `currentPrice()`; called once at construction and again in `loadHistory()` after each bar load so the entry prefills track the chart's current symbol/price.
- Stop/target intentionally left blank — the plan/spec does not imply percentage defaults, so they are required user input.

## Error handling
- Client-side validation rejects missing/non-positive entry/stop/target with a clear status message (`#status`) + a log line, before any network call.
- Server errors (422 broker-unsupported / bad protective prices, 503 no session, any non-OK) throw from `placeBracket`; the `.catch` path mirrors `placeOrder`'s error path: `statusText.textContent = "bracket rejected: <detail>"` plus `logLine(...)` with the exact HTTP status + response body.

## Verify
```
cd frontend
npm run typecheck   # exit 0 (tsc --noEmit)
npm run build       # vite build succeeds (23 modules, dist/index.html + index-*.js)
```

## Concerns
- Non-bracket `/orders` mutations flow through `TradexTradeFeed` and get refetched on WS control messages; the bracket form POSTs directly to `/orders/bracket`, so repaint relies on the explicit `tradeHost.sync(await fetchBook())` in the success handler (backend does not currently emit a WS control frame for bracket placement). If the broker/backend later emits `order`/`fill` control messages, the WS hub refetch will also cover it.
- `submitBracket` reuses `qtyInput` as the shared quantity for both market and bracket tickets — intentional (single ticket), but a user could surprise themselves by changing qty before clicking a bracket button.
- No percentage-based stop/target defaults per spec (left blank, required); a future nicety would prefill them relative to entry.