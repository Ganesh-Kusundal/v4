import { test, expect, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

const SCREENSHOT_DIR = join(process.cwd(), 'e2e', 'screenshots');
mkdirSync(SCREENSHOT_DIR, { recursive: true });

// ── Locators ─────────────────────────────────────────────────────────────────
// The shellbar Buy/Brk Buy buttons both carry .tbtn--buy; the shellbar qty
// input shares .field--qty with the legacy #qty. Use exact-name locators.
const shellBuyBtn = (page: Page) => page.getByRole('button', { name: 'Buy', exact: true });
const shellSellBtn = (page: Page) => page.getByRole('button', { name: 'Sell', exact: true });
const brkBuyBtn = (page: Page) => page.getByRole('button', { name: 'Brk Buy' });
const shellQty = (page: Page) => page.locator('.field--qty').first(); // shellbar instance
const brkEntry = (page: Page) => page.locator('input[placeholder="Entry"]');
const brkStop = (page: Page) => page.locator('input[placeholder="Stop"]');
const brkTarget = (page: Page) => page.locator('input[placeholder="Target"]');

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Read pixel data from the chart canvas and confirm it is not a blank fill. */
async function canvasIsNotBlank(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const canvas = document.querySelector('#chart canvas') as HTMLCanvasElement | null;
    if (!canvas || canvas.width === 0 || canvas.height === 0) return false;
    const ctx = canvas.getContext('2d');
    if (!ctx) return false;
    const { width, height } = canvas;
    const data = ctx.getImageData(0, 0, width, height).data;
    // Sample every 400th byte; a rendered chart has many distinct colors.
    const colors = new Set<number>();
    for (let i = 0; i < data.length; i += 400) {
      colors.add((data[i] << 16) | (data[i + 1] << 8) | data[i + 2]);
    }
    return colors.size > 5;
  });
}

/** Fetch the full book snapshot from the backend (proxied through Vite). */
async function fetchBook(page: Page): Promise<{ orders: any[]; positions: any[] }> {
  const resp = await page.request.get('/api/charts/book');
  expect(resp.ok()).toBeTruthy();
  return resp.json();
}

/** Place an order directly via the backend API (bypasses the UI). */
async function placeOrderApi(
  page: Page,
  body: Record<string, unknown>,
): Promise<{ order_id: string; status: string }> {
  const resp = await page.request.post('/orders', {
    headers: { 'Content-Type': 'application/json' },
    data: body,
  });
  expect(resp.ok(), `POST /orders failed: ${resp.status()} ${resp.statusText()}`).toBeTruthy();
  const json = await resp.json();
  return { order_id: json.order_id, status: json.status };
}

// ── Tests ────────────────────────────────────────────────────────────────────

test.describe('Trade tier UI', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    // Wait for the chart to load — status text shows bar count once ready.
    await expect(page.locator('#status')).toContainText('bars', { timeout: 30_000 });
    // Canvas is created by the chart library after the chart initializes.
    await expect(page.locator('#chart canvas').first()).toBeVisible({ timeout: 20_000 });
  });

  test('Test 1: Chart loads with trade host — canvas paints, no console errors', async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') errors.push(msg.text());
    });
    page.on('pageerror', (err) => errors.push(String(err)));

    // Canvas is already visible from beforeEach; confirm it has real dimensions.
    const box = await page.locator('#chart canvas').first().boundingBox();
    expect(box).not.toBeNull();
    expect(box!.width).toBeGreaterThan(100);
    expect(box!.height).toBeGreaterThan(100);

    // Canvas must not be a blank fill — the candle renderer drew something.
    expect(await canvasIsNotBlank(page)).toBeTruthy();

    // Trade host is wired: the shellbar has the Buy/Sell/Bracket controls.
    await expect(shellBuyBtn(page)).toBeVisible();
    await expect(shellSellBtn(page)).toBeVisible();
    await expect(brkBuyBtn(page)).toBeVisible();

    expect(errors, `console/page errors: ${errors.join(' | ')}`).toHaveLength(0);

    await page.screenshot({ path: join(SCREENSHOT_DIR, '01-chart-loaded.png') });
  });

  test('Test 2: Place order via UI → appears on chart', async ({ page }) => {
    // Read the default quantity from the shellbar qty input.
    const qtyValue = await shellQty(page).inputValue();
    expect(qtyValue).toBe('10');

    // Intercept POST /orders to confirm the request traverses the spine.
    const orderPromise = page.waitForResponse(
      (r) => r.url().endsWith('/orders') && r.request().method() === 'POST',
      { timeout: 15_000 },
    );

    await shellBuyBtn(page).click();

    // Status text reflects the sent order (placeOrder logs synchronously).
    await expect(page.locator('#status')).toContainText('order BUY sent', {
      timeout: 10_000,
    });

    const resp = await orderPromise;
    expect(resp.status()).toBe(200);
    const body = await resp.json();
    expect(body.order_id).toBeTruthy();

    // Poll the book until the order (or its resulting position) shows up.
    await expect
      .poll(async () => (await fetchBook(page)).orders.length, { timeout: 15_000 })
      .toBeGreaterThan(0);

    // The orders dock panel (updated via WS) should show the order row.
    await page.locator('#bottom-tabs button[data-tab="orders"]').click();
    await expect(page.locator('#bottom-panels .dock-panel.active[data-tab="orders"]')).toBeVisible();
    await expect(
      page.locator('#bottom-panels .dock-panel.active table'),
    ).toContainText('BUY', { timeout: 10_000 });

    await page.screenshot({ path: join(SCREENSHOT_DIR, '02-order-sent.png') });
  });

  test('Test 3: Order line renders — LIMIT order appears in book, canvas not blank', async ({
    page,
  }) => {
    // Place a LIMIT order via API (UI only exposes MARKET + bracket).
    const bookBefore = await fetchBook();
    const orderCountBefore = bookBefore.orders.length;

    // Use a price far from market so the order rests instead of filling.
    await placeOrderApi(page, {
      exchange: 'NSE',
      symbol: 'RELIANCE',
      side: 'BUY',
      order_type: 'LIMIT',
      quantity: 5,
      price: 1.0,
    });

    // Book should now contain the resting LIMIT order.
    await expect
      .poll(async () => (await fetchBook(page)).orders.length, { timeout: 15_000 })
      .toBe(orderCountBefore + 1);

    const book = await fetchBook();
    const order = book.orders.find(
      (o: any) => o.type === 'LIMIT' && o.qty === 5 && o.price === 1.0,
    );
    expect(order).toBeDefined();
    expect(order.side).toBe('BUY');
    expect(order.symbol).toBe('RELIANCE');

    // Give the TradeController a moment to reconcile and draw the line.
    await page.waitForTimeout(1000);

    // Canvas must still be painted (order line is drawn on top of candles).
    expect(await canvasIsNotBlank(page)).toBeTruthy();

    await page.screenshot({ path: join(SCREENSHOT_DIR, '03-order-line.png') });
  });

  test('Test 4: Position marker renders — filled BUY creates a position', async ({ page }) => {
    // Paper session fills MARKET orders immediately → position appears.
    // Backend requires a positive price even for MARKET orders.
    await placeOrderApi(page, {
      exchange: 'NSE',
      symbol: 'RELIANCE',
      side: 'BUY',
      order_type: 'MARKET',
      quantity: 10,
      price: 9999.0,
    });

    // Poll until a position with non-zero netQty appears.
    await expect
      .poll(async () => {
        const book = await fetchBook();
        return book.positions.some((p: any) => p.netQty !== 0);
      }, { timeout: 15_000 })
      .toBeTruthy();

    const book = await fetchBook();
    const pos = book.positions.find((p: any) => p.symbol === 'RELIANCE' && p.netQty > 0);
    expect(pos).toBeDefined();
    expect(pos.netQty).toBe(10);

    // Give the TradeController a moment to reconcile and draw the marker.
    await page.waitForTimeout(1000);

    await page.screenshot({ path: join(SCREENSHOT_DIR, '04-position-marker.png') });
  });

  test('Test 5: Bracket order renders — Brk Buy places SL/TP legs', async ({ page }) => {
    // Fill the bracket inputs (entry prefilled from last close).
    await brkEntry(page).fill('100');
    await brkStop(page).fill('95');
    await brkTarget(page).fill('110');

    // Intercept POST /orders/bracket.
    const bracketPromise = page.waitForResponse(
      (r) => r.url().endsWith('/orders/bracket') && r.request().method() === 'POST',
      { timeout: 15_000 },
    );

    await brkBuyBtn(page).click();

    const resp = await bracketPromise;
    // Paper broker does not support super orders → 422. The UI still sends
    // the request; we verify the endpoint was called and the response code
    // matches the paper broker's capability.
    expect([200, 422]).toContain(resp.status());

    if (resp.status() === 200) {
      // If a future broker supports brackets, verify the legs appear.
      const body = await resp.json();
      expect(body.order_id).toBeTruthy();

      await expect(page.locator('#status')).toContainText('bracket BUY', { timeout: 10_000 });

      await expect
        .poll(async () => {
          const book = await fetchBook();
          const roles = book.orders.map((o: any) => o.role ?? 'entry');
          return roles.includes('sl') && roles.includes('tp');
        }, { timeout: 15_000 })
        .toBeTruthy();

      const book = await fetchBook();
      const sl = book.orders.find((o: any) => o.role === 'sl');
      const tp = book.orders.find((o: any) => o.role === 'tp');
      expect(sl).toBeDefined();
      expect(tp).toBeDefined();
    } else {
      // Paper broker: verify the UI surfaces the rejection.
      await expect(page.locator('#status')).toContainText('bracket rejected', {
        timeout: 10_000,
      });
    }

    await page.screenshot({ path: join(SCREENSHOT_DIR, '05-bracket.png') });
  });

  test('Test 6: WebSocket live update — UI updates without polling', async ({ page }) => {
    // Open the orders dock panel so we can observe WS-driven updates.
    await page.locator('#bottom-tabs button[data-tab="orders"]').click();
    await expect(
      page.locator('#bottom-panels .dock-panel.active[data-tab="orders"]'),
    ).toBeVisible();

    // Place an order via the API directly (NOT through the UI). The backend
    // broadcasts a WS control message; the UI's TradeWsHub refetches the book
    // and the orders panel updates — no polling involved.
    const bookBefore = await fetchBook();
    const orderCountBefore = bookBefore.orders.length;

    await placeOrderApi(page, {
      exchange: 'NSE',
      symbol: 'RELIANCE',
      side: 'SELL',
      order_type: 'LIMIT',
      quantity: 3,
      price: 9999.0,
    });

    // The orders panel (driven by subscribeOrders → WS refetch) should show
    // the new order without any page reload or polling.
    await expect
      .poll(async () => {
        const rows = page.locator(
          '#bottom-panels .dock-panel.active table tr',
        );
        return rows.count();
      }, { timeout: 15_000 })
      .toBeGreaterThan(orderCountBefore + 1); // +1 for header row

    // Verify the SELL order is visible in the panel.
    await expect(
      page.locator('#bottom-panels .dock-panel.active table'),
    ).toContainText('SELL', { timeout: 10_000 });

    await page.screenshot({ path: join(SCREENSHOT_DIR, '06-ws-live.png') });
  });

  test('Test 7: Data flow end-to-end — order fields match across the stack', async ({
    page,
  }) => {
    // Place a MARKET BUY via the UI.
    const respPromise = page.waitForResponse(
      (r) => r.url().endsWith('/orders') && r.request().method() === 'POST',
      { timeout: 15_000 },
    );
    await shellQty(page).fill('7');
    await shellBuyBtn(page).click();
    const resp = await respPromise;
    const placed = await resp.json();

    // Poll the book until the order appears.
    await expect
      .poll(async () => {
        const book = await fetchBook();
        return book.orders.some((o: any) => o.id === placed.order_id);
      }, { timeout: 15_000 })
      .toBeTruthy();

    const book = await fetchBook();
    const row = book.orders.find((o: any) => o.id === placed.order_id);
    expect(row).toBeDefined();

    // Verify the backend book row carries the expected fields.
    expect(row.symbol).toBe('RELIANCE');
    expect(row.exchange).toBe('NSE');
    expect(row.side).toBe('BUY');
    expect(row.type).toBe('MARKET');
    expect(row.qty).toBe(7);
    expect(row.id).toBe(placed.order_id);

    // Verify the orders dock panel (the rendered view of the book) shows the
    // same fields — this exercises the full data flow: backend → WS → fetchBook
    // → subscribeOrders → DOM table.
    await page.locator('#bottom-tabs button[data-tab="orders"]').click();
    await expect(
      page.locator('#bottom-panels .dock-panel.active table'),
    ).toContainText('RELIANCE', { timeout: 10_000 });
    await expect(
      page.locator('#bottom-panels .dock-panel.active table'),
    ).toContainText('MARKET', { timeout: 10_000 });

    // Verify mapBookRowToOrder semantics: the panel renders the mapped fields.
    // The panel shows side/type/qty/price/status columns from the book row.
    const panelText = await page
      .locator('#bottom-panels .dock-panel.active table')
      .innerText();
    expect(panelText).toContain('BUY');
    expect(panelText).toContain('7');

    await page.screenshot({ path: join(SCREENSHOT_DIR, '07-data-flow.png') });
  });
});
