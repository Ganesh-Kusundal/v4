# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: trade-tier.e2e.ts >> Trade tier UI >> Test 5: Bracket order renders — Brk Buy places SL/TP legs
- Location: e2e/trade-tier.e2e.ts:209:3

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: locator('#chart canvas').first()
Expected: visible
Timeout: 20000ms
Error: element(s) not found

Call log:
  - Expect "toBeVisible" with timeout 20000ms
  - waiting for locator('#chart canvas').first()

```

```yaml
- banner:
  - button "Trade"
- main
- complementary:
  - heading "◈ Watchlist" [level=3]
```

# Test source

```ts
  1   | import { test, expect, type Page } from '@playwright/test';
  2   | import { mkdirSync } from 'node:fs';
  3   | import { join } from 'node:path';
  4   | 
  5   | const SCREENSHOT_DIR = join(process.cwd(), 'e2e', 'screenshots');
  6   | mkdirSync(SCREENSHOT_DIR, { recursive: true });
  7   | 
  8   | // ── Locators ─────────────────────────────────────────────────────────────────
  9   | // The shellbar Buy/Brk Buy buttons both carry .tbtn--buy; the shellbar qty
  10  | // input shares .field--qty with the legacy #qty. Use exact-name locators.
  11  | const shellBuyBtn = (page: Page) => page.getByRole('button', { name: 'Buy', exact: true });
  12  | const shellSellBtn = (page: Page) => page.getByRole('button', { name: 'Sell', exact: true });
  13  | const brkBuyBtn = (page: Page) => page.getByRole('button', { name: 'Brk Buy' });
  14  | const shellQty = (page: Page) => page.locator('.field--qty').first(); // shellbar instance
  15  | const brkEntry = (page: Page) => page.locator('input[placeholder="Entry"]');
  16  | const brkStop = (page: Page) => page.locator('input[placeholder="Stop"]');
  17  | const brkTarget = (page: Page) => page.locator('input[placeholder="Target"]');
  18  | 
  19  | // ── Helpers ──────────────────────────────────────────────────────────────────
  20  | 
  21  | /** Read pixel data from the chart canvas and confirm it is not a blank fill. */
  22  | async function canvasIsNotBlank(page: Page): Promise<boolean> {
  23  |   return page.evaluate(() => {
  24  |     const canvas = document.querySelector('#chart canvas') as HTMLCanvasElement | null;
  25  |     if (!canvas || canvas.width === 0 || canvas.height === 0) return false;
  26  |     const ctx = canvas.getContext('2d');
  27  |     if (!ctx) return false;
  28  |     const { width, height } = canvas;
  29  |     const data = ctx.getImageData(0, 0, width, height).data;
  30  |     // Sample every 400th byte; a rendered chart has many distinct colors.
  31  |     const colors = new Set<number>();
  32  |     for (let i = 0; i < data.length; i += 400) {
  33  |       colors.add((data[i] << 16) | (data[i + 1] << 8) | data[i + 2]);
  34  |     }
  35  |     return colors.size > 5;
  36  |   });
  37  | }
  38  | 
  39  | /** Fetch the full book snapshot from the backend (proxied through Vite). */
  40  | async function fetchBook(page: Page): Promise<{ orders: any[]; positions: any[] }> {
  41  |   const resp = await page.request.get('/api/charts/book');
  42  |   expect(resp.ok()).toBeTruthy();
  43  |   return resp.json();
  44  | }
  45  | 
  46  | /** Place an order directly via the backend API (bypasses the UI). */
  47  | async function placeOrderApi(
  48  |   page: Page,
  49  |   body: Record<string, unknown>,
  50  | ): Promise<{ order_id: string; status: string }> {
  51  |   const resp = await page.request.post('/orders', {
  52  |     headers: { 'Content-Type': 'application/json' },
  53  |     data: body,
  54  |   });
  55  |   expect(resp.ok(), `POST /orders failed: ${resp.status()} ${resp.statusText()}`).toBeTruthy();
  56  |   const json = await resp.json();
  57  |   return { order_id: json.order_id, status: json.status };
  58  | }
  59  | 
  60  | // ── Tests ────────────────────────────────────────────────────────────────────
  61  | 
  62  | test.describe('Trade tier UI', () => {
  63  |   test.beforeEach(async ({ page }) => {
  64  |     await page.goto('/');
  65  |     // Chart is created dynamically by main.ts — wait for the chart container
  66  |     // and the canvas inside it to be visible.
  67  |     await expect(page.locator('#chart')).toBeVisible({ timeout: 30_000 });
> 68  |     await expect(page.locator('#chart canvas').first()).toBeVisible({ timeout: 20_000 });
      |                                                         ^ Error: expect(locator).toBeVisible() failed
  69  |     // Allow the chart renderer to paint (canvas dimensions settle).
  70  |     await page.waitForTimeout(1_000);
  71  |   });
  72  | 
  73  |   test('Test 1: Chart loads with trade host — canvas paints, no console errors', async ({
  74  |     page,
  75  |   }) => {
  76  |     const errors: string[] = [];
  77  |     page.on('console', (msg) => {
  78  |       if (msg.type() === 'error') errors.push(msg.text());
  79  |     });
  80  |     page.on('pageerror', (err) => errors.push(String(err)));
  81  | 
  82  |     // Canvas is already visible from beforeEach; confirm it has real dimensions.
  83  |     const box = await page.locator('#chart canvas').first().boundingBox();
  84  |     expect(box).not.toBeNull();
  85  |     expect(box!.width).toBeGreaterThan(100);
  86  |     expect(box!.height).toBeGreaterThan(100);
  87  | 
  88  |     // Canvas must not be a blank fill — the candle renderer drew something.
  89  |     expect(await canvasIsNotBlank(page)).toBeTruthy();
  90  | 
  91  |     // Trade host is wired: the shellbar has the Buy/Sell/Bracket controls.
  92  |     await expect(shellBuyBtn(page)).toBeVisible();
  93  |     await expect(shellSellBtn(page)).toBeVisible();
  94  |     await expect(brkBuyBtn(page)).toBeVisible();
  95  | 
  96  |     expect(errors, `console/page errors: ${errors.join(' | ')}`).toHaveLength(0);
  97  | 
  98  |     await page.screenshot({ path: join(SCREENSHOT_DIR, '01-chart-loaded.png') });
  99  |   });
  100 | 
  101 |   test('Test 2: Place order via UI → appears on chart', async ({ page }) => {
  102 |     // Read the default quantity from the shellbar qty input.
  103 |     const qtyValue = await shellQty(page).inputValue();
  104 |     expect(qtyValue).toBe('10');
  105 | 
  106 |     // Intercept POST /orders to confirm the request traverses the spine.
  107 |     const orderPromise = page.waitForResponse(
  108 |       (r) => r.url().endsWith('/orders') && r.request().method() === 'POST',
  109 |       { timeout: 15_000 },
  110 |     );
  111 | 
  112 |     await shellBuyBtn(page).click();
  113 | 
  114 |     // Status text reflects the sent order (placeOrder logs synchronously).
  115 |     await expect(page.locator('#status')).toContainText('order BUY sent', {
  116 |       timeout: 10_000,
  117 |     });
  118 | 
  119 |     const resp = await orderPromise;
  120 |     expect(resp.status()).toBe(200);
  121 |     const body = await resp.json();
  122 |     expect(body.order_id).toBeTruthy();
  123 | 
  124 |     // Poll the book until the order (or its resulting position) shows up.
  125 |     await expect
  126 |       .poll(async () => (await fetchBook(page)).orders.length, { timeout: 15_000 })
  127 |       .toBeGreaterThan(0);
  128 | 
  129 |     // The orders dock panel (updated via WS) should show the order row.
  130 |     await page.locator('#bottom-tabs button[data-tab="orders"]').click();
  131 |     await expect(page.locator('#bottom-panels .dock-panel.active[data-tab="orders"]')).toBeVisible();
  132 |     await expect(
  133 |       page.locator('#bottom-panels .dock-panel.active table'),
  134 |     ).toContainText('BUY', { timeout: 10_000 });
  135 | 
  136 |     await page.screenshot({ path: join(SCREENSHOT_DIR, '02-order-sent.png') });
  137 |   });
  138 | 
  139 |   test('Test 3: Order line renders — LIMIT order appears in book, canvas not blank', async ({
  140 |     page,
  141 |   }) => {
  142 |     // Place a LIMIT order via API (UI only exposes MARKET + bracket).
  143 |     const bookBefore = await fetchBook();
  144 |     const orderCountBefore = bookBefore.orders.length;
  145 | 
  146 |     // Use a price far from market so the order rests instead of filling.
  147 |     await placeOrderApi(page, {
  148 |       exchange: 'NSE',
  149 |       symbol: 'RELIANCE',
  150 |       side: 'BUY',
  151 |       order_type: 'LIMIT',
  152 |       quantity: 5,
  153 |       price: 1.0,
  154 |     });
  155 | 
  156 |     // Book should now contain the resting LIMIT order.
  157 |     await expect
  158 |       .poll(async () => (await fetchBook(page)).orders.length, { timeout: 15_000 })
  159 |       .toBe(orderCountBefore + 1);
  160 | 
  161 |     const book = await fetchBook();
  162 |     const order = book.orders.find(
  163 |       (o: any) => o.type === 'LIMIT' && o.qty === 5 && o.price === 1.0,
  164 |     );
  165 |     expect(order).toBeDefined();
  166 |     expect(order.side).toBe('BUY');
  167 |     expect(order.symbol).toBe('RELIANCE');
  168 | 
```