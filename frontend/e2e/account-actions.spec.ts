import { expect, test, type Page } from '@playwright/test';

const account = {
  account_id: 'paper',
  balance: '100000',
  margin: '0',
  unrealized_pnl: '0',
  positions: [{ symbol: 'RELIANCE', exchange: 'NSE', net_qty: 4, avg_price: 100, unrealized_pnl: 2 }],
};

const book = {
  orders: [{ id: 'order-1', symbol: 'RELIANCE', exchange: 'NSE', side: 'BUY', type: 'LIMIT', qty: 5, filledQty: 0, price: 99, triggerPrice: null, status: 'working' }],
  positions: [{ symbol: 'RELIANCE', exchange: 'NSE', netQty: 4, avgPrice: 100 }],
};

const waitForTrading = async (page: Page): Promise<void> => {
  await expect(page.locator('#mode')).toContainText(/· READY$/);
  const arm = page.getByRole('checkbox', { name: /arm trading/i });
  await arm.check();
  await expect(arm).toBeChecked();
  await expect(page.locator('#order-status')).toContainText(/ready for guarded intraday orders/i);
};

test.beforeEach(async ({ page }) => {
  await page.route(/\/api\/charts\/workspace$/, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.route('**/api/charts/account', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(account) });
  });
  await page.route('**/api/charts/book', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(book) });
  });
  await page.goto('/ui/');
  await expect(page.locator('#mode')).toContainText(/· READY$/);
});

test('exposes stable account order and position surfaces', async ({ page }) => {
  await expect(page.locator('#account-orders')).toBeVisible();
  await expect(page.locator('#account-positions')).toBeVisible();
  const cancel = page.locator('#account-orders [data-action="cancel"]');
  const modify = page.locator('#account-orders [data-action="modify"]');
  const exit = page.locator('#account-positions [data-action="exit"]');
  await expect(cancel).toBeVisible();
  await expect(modify).toBeVisible();
  await expect(exit).toBeVisible();
  await expect(cancel).toBeDisabled();
  await expect(modify).toBeDisabled();
  await expect(exit).toBeDisabled();
  await waitForTrading(page);
  await expect(cancel).toBeEnabled();
  await expect(modify).toBeEnabled();
  await expect(exit).toBeEnabled();
});

test('cancels a working order with authentication and a fresh idempotency key', async ({ page }) => {
  let request: { method?: string; key?: string; auth?: string } | null = null;
  await page.route('**/orders/order-1', async (route) => {
    request = {
      method: route.request().method(),
      key: route.request().headers()['idempotency-key'],
      auth: route.request().headers()['x-api-key'],
    };
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ order_id: 'order-1', status: 'cancelled' }),
    });
  });
  await waitForTrading(page);
  const cancel = page.locator('#account-orders [data-action="cancel"]');
  await cancel.click();
  await expect.poll(() => request?.method).toBe('DELETE');
  expect(request?.key).toMatch(/^[0-9a-f-]{36}$/i);
  await expect(page.locator('#account-orders tr[data-order-id="order-1"]')).toHaveCount(0);
});

test('modifies a working order through the existing PUT contract', async ({ page }) => {
  let body: Record<string, unknown> | null = null;
  let key = '';
  await page.route('**/orders/order-1', async (route) => {
    body = route.request().postDataJSON() as Record<string, unknown>;
    key = route.request().headers()['idempotency-key'] ?? '';
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ order_id: 'order-1', status: 'modified' }),
    });
  });
  await waitForTrading(page);
  const row = page.locator('#account-orders tr[data-order-id="order-1"]');
  await row.getByRole('spinbutton', { name: 'Quantity for order-1' }).fill('6');
  await row.getByRole('spinbutton', { name: 'Price for order-1' }).fill('98.5');
  await row.getByRole('button', { name: 'Modify' }).click();
  await expect.poll(() => body?.quantity).toBe('6');
  expect(body?.price).toBe('98.5');
  expect(key).toMatch(/^[0-9a-f-]{36}$/i);
  await expect(row).toBeVisible();
});

test('exits a position with the opposite market side and no price', async ({ page }) => {
  let body: Record<string, unknown> | null = null;
  let key = '';
  await page.route('**/orders', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    body = route.request().postDataJSON() as Record<string, unknown>;
    key = route.request().headers()['idempotency-key'] ?? '';
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ order_id: 'exit-1', status: 'SUBMITTED' }),
    });
  });
  await waitForTrading(page);
  await page.locator('#account-positions [data-action="exit"]').click();
  await expect.poll(() => body?.side).toBe('SELL');
  expect(body?.order_type).toBe('MARKET');
  expect(body?.quantity).toBe(4);
  expect(body).not.toHaveProperty('price');
  expect(key).toMatch(/^[0-9a-f-]{36}$/i);
  await expect(page.locator('#account-positions [data-action="exit"]')).toHaveCount(0);
});

test('disables account mutations while replay is active', async ({ page }) => {
  let posts = 0;
  await page.route('**/orders', async (route) => {
    if (route.request().method() === 'POST') posts += 1;
    await route.continue();
  });
  await waitForTrading(page);
  const cancel = page.locator('#account-orders [data-action="cancel"]');
  const exit = page.locator('#account-positions [data-action="exit"]');
  await expect(cancel).toBeEnabled();
  await expect(exit).toBeEnabled();
  await page.locator('#replay-start').click();
  await expect(page.locator('#order-status')).toContainText(/replay active/i);
  await expect(cancel).toBeDisabled();
  await expect(exit).toBeDisabled();
  expect(posts).toBe(0);
});

test('retains a row and shows a typed error when a mutation fails', async ({ page }) => {
  await page.route('**/orders/order-1', async (route) => {
    await route.fulfill({
      status: 422,
      contentType: 'application/json',
      body: JSON.stringify({ error: { message: 'broker rejected modify' } }),
    });
  });
  await waitForTrading(page);
  const row = page.locator('#account-orders tr[data-order-id="order-1"]');
  await row.getByRole('button', { name: 'Modify' }).click();
  await expect(row.getByRole('alert')).toContainText(/modify failed: broker rejected modify/i);
  await expect(row).toBeVisible();
  await expect(row.getByRole('button', { name: 'Modify' })).toBeVisible();
});
