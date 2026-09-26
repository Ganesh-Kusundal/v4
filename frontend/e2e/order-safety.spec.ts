import { expect, test, type Page } from '@playwright/test';

const waitForFeedReady = async (page: Page): Promise<void> => {
  await expect(page.locator('#mode')).toContainText(/· READY$/);
  await expect(page.locator('#feed-status')).toHaveText('Feed ready');
};

const panePoint = async (
  page: Page,
  paneIndex: number,
  vertical = 0.5,
): Promise<{ x: number; y: number }> => {
  const chart = page.locator('.oac-chart');
  await expect(chart).toBeVisible();
  const box = await chart.boundingBox();
  expect(box).not.toBeNull();
  const weights = [1, ...Array.from({ length: paneIndex }, () => 0.32)];
  const total = weights.reduce((sum, weight) => sum + weight, 0);
  const top = weights.slice(0, paneIndex).reduce((sum, weight) => sum + weight, 0) / total;
  const height = weights[paneIndex]! / total;
  return {
    x: box!.x + box!.width * 0.35,
    y: box!.y + box!.height * (top + vertical * height),
  };
};

const openMenu = async (page: Page, paneIndex = 0, vertical = 0.5) => {
  const point = await panePoint(page, paneIndex, vertical);
  await page.mouse.click(point.x, point.y, { button: 'right' });
  const menu = page.getByRole('menu', { name: 'Chart menu' });
  await expect(menu).toBeVisible();
  return menu;
};

const countOrderPosts = async (page: Page): Promise<() => number> => {
  let posts = 0;
  await page.route('**/orders', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.continue();
      return;
    }
    posts += 1;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ order_id: 'unexpected-order', status: 'SUBMITTED' }),
    });
  });
  return () => posts;
};

test.beforeEach(async ({ page }) => {
  await page.route(/\/api\/charts\/workspace$/, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.goto('/ui/');
  const collapse = page.getByRole('button', { name: /collapse all/i });
  await expect(collapse).toBeVisible();
  await collapse.click();
});

test('exposes the order-safety controls with safe defaults', async ({ page }) => {
  await expect(page.getByText('INTRADAY', { exact: true })).toBeVisible();
  const arm = page.getByRole('checkbox', { name: /arm trading/i });
  await expect(arm).toBeVisible();
  await expect(arm).not.toBeChecked();
  await expect(page.locator('#qty')).toHaveValue('1');
  await expect(page.locator('#order-status')).toBeVisible();
  await expect(page.locator('#mode')).toContainText(/paper|unknown|live/i);
});

test('keeps keyed sessions fail closed when mode is not authoritative', async ({ page }) => {
  await page.route('**/ui/', async (route) => {
    const response = await route.fetch();
    const body = (await response.text()).replace('content="" data-applied="false"', 'content="test-key" data-applied="true"');
    await route.fulfill({ response, body });
  });
  await page.goto('/ui/');
  await expect(page.locator('#mode')).toContainText('MODE UNKNOWN');
  await expect(page.getByRole('checkbox', { name: /arm trading/i })).toBeDisabled();
});

test('does not place an order while disarmed', async ({ page }) => {
  const posts = await countOrderPosts(page);
  const menu = await openMenu(page);
  await menu.getByRole('menuitem', { name: /buy market/i }).click();
  await expect(page.locator('#order-status')).toContainText(/arm trading/i);
  expect(posts()).toBe(0);
});

test('requires an exact confirmation and submits the selected quantity', async ({ page }) => {
  const requests: Array<{ quantity?: unknown; headers: Record<string, string> }> = [];
  await page.route('**/orders', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.continue();
      return;
    }
    const body = route.request().postDataJSON() as { quantity?: unknown };
    requests.push({ quantity: body.quantity, headers: route.request().headers() });
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ order_id: 'test-1', status: 'SUBMITTED' }),
    });
  });
  await waitForFeedReady(page);
  await page.locator('#qty').fill('3');
  await page.getByRole('checkbox', { name: /arm trading/i }).check();
  const menu = await openMenu(page);
  let readMessage: (message: string) => void = () => undefined;
  const confirmationMessage = new Promise<string>((resolve) => {
    readMessage = resolve;
  });
  page.once('dialog', (dialog) => {
    readMessage(dialog.message());
    void dialog.accept();
  });
  await menu.getByRole('menuitem', { name: /buy market/i }).click();
  expect(await confirmationMessage).toBe('Confirm INTRADAY BUY MARKET 3 NSE:RELIANCE MARKET');
  await expect.poll(() => requests.length).toBe(1);
  expect(requests[0]?.quantity).toBe(3);
  expect(requests[0]?.headers['idempotency-key']).toBeTruthy();
  await expect(page.getByRole('checkbox', { name: /arm trading/i })).not.toBeChecked();
});

test('rejects a non-positive quantity before confirmation', async ({ page }) => {
  const posts = await countOrderPosts(page);
  await waitForFeedReady(page);
  await page.locator('#qty').fill('0');
  await page.getByRole('checkbox', { name: /arm trading/i }).check();
  const menu = await openMenu(page);
  await menu.getByRole('menuitem', { name: /buy market/i }).click();
  await expect(page.locator('#order-status')).toContainText(/quantity must be a positive integer/i);
  expect(posts()).toBe(0);
});

test('rejects a buy stop above the latest bar close', async ({ page }) => {
  const posts = await countOrderPosts(page);
  await waitForFeedReady(page);
  await page.getByRole('checkbox', { name: /arm trading/i }).check();
  const menu = await openMenu(page, 0, 0.12);
  await menu.getByRole('menuitem', { name: /buy stop at/i }).click();
  await expect(page.locator('#order-status')).toContainText(/invalid stop-side relationship/i);
  expect(posts()).toBe(0);
});

test('rejects an order from an indicator pane without an HTTP mutation', async ({ page }) => {
  const posts = await countOrderPosts(page);
  await waitForFeedReady(page);
  await page.getByRole('checkbox', { name: /arm trading/i }).check();
  await page.getByRole('button', { name: 'Indicators', exact: true }).click();
  const picker = page.getByRole('dialog', { name: 'Indicators' });
  await expect(picker).toBeVisible();
  await picker.getByRole('searchbox', { name: 'Search indicators' }).fill('RSI');
  await picker.getByRole('option', { name: 'RSI', exact: true }).click();
  await picker.getByRole('button', { name: 'Done' }).click();
  const menu = await openMenu(page, 1);
  await menu.getByRole('menuitem', { name: /buy market/i }).click();
  await expect(page.locator('#order-status')).toContainText(/price pane only/i);
  expect(posts()).toBe(0);
});

test('disables submission while replay is active', async ({ page }) => {
  const posts = await countOrderPosts(page);
  await waitForFeedReady(page);
  await page.getByRole('checkbox', { name: /arm trading/i }).check();
  await page.locator('#replay-start').click();
  await expect(page.locator('#order-status')).toContainText(/replay active/i);
  const menu = await openMenu(page);
  await menu.getByRole('menuitem', { name: /buy market/i }).click();
  await expect(page.locator('#order-status')).toContainText(/replay active/i);
  expect(posts()).toBe(0);
});
