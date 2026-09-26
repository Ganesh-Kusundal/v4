import { expect, test } from '@playwright/test';

const APP = '/ui/';

test.beforeEach(async ({ request }) => {
  for (const interval of ['1m', '5m', '15m', '30m', '1h', 'D']) {
    await request.delete(`/api/charts/workspace/NSE_RELIANCE_${interval}_default`);
  }
});

test('switches the chart exchange and refreshes host history', async ({ page }) => {
  const historyRequests: string[] = [];
  page.on('request', (request) => {
    const url = request.url();
    if (url.includes('/api/charts/history/')) historyRequests.push(url);
  });

  await page.goto(APP);
  const exchange = page.getByLabel('Exchange');
  await expect(exchange).toBeVisible();
  await expect(exchange).toHaveValue('NSE');
  await expect(page.getByText(/\d+\s+bars/).first()).toBeVisible();

  await exchange.selectOption('NFO');
  await expect.poll(() => historyRequests.some((url) => /NFO(?::|%3A)/i.test(url))).toBe(true);
  await expect(exchange).toHaveValue('NFO');

  await exchange.selectOption('NSE');
  await expect.poll(() => historyRequests.filter((url) => /NSE(?::|%3A)/i.test(url)).length).toBeGreaterThan(1);
  await expect(exchange).toHaveValue('NSE');
});
