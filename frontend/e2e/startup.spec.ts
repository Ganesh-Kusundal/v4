import { expect, test } from '@playwright/test';

test('renders defaults and keeps orders disarmed when boot endpoints stall', async ({ page }) => {
  let releaseStrategies: (() => void) | undefined;
  let releaseHistory: (() => void) | undefined;
  const strategyGate = new Promise<void>((resolve) => {
    releaseStrategies = resolve;
  });
  const historyGate = new Promise<void>((resolve) => {
    releaseHistory = resolve;
  });

  await page.route('**/api/charts/strategies', async (route) => {
    try {
      await strategyGate;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ backtestable: [] }) });
    } catch {
      return;
    }
  });
  await page.route(/\/api\/charts\/history\//, async (route) => {
    try {
      await historyGate;
      await route.continue();
    } catch {
      return;
    }
  });

  try {
    await page.goto('/ui/', { waitUntil: 'domcontentloaded', timeout: 20_000 });
    await expect(page.locator('.oac-widget')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.oac-sym__input')).toHaveValue('RELIANCE');
    await expect(page.locator('.oac-toast__msg').filter({ hasText: 'Startup degraded' }).first()).toBeVisible();
    await expect(page.locator('#mode')).toContainText('NOT READY');
    await expect(page.locator('#arm')).toBeDisabled();
  } finally {
    releaseStrategies?.();
    releaseHistory?.();
  }
});

test('exposes every configured exchange in the restored selector', async ({ page }) => {
  await page.route(/\/api\/charts\/workspace$/, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.goto('/ui/');
  const exchange = page.getByRole('combobox', { name: 'Exchange' });
  await expect(exchange).toBeVisible();
  expect(await exchange.locator('option').allTextContents()).toEqual(['NSE', 'NFO', 'BSE', 'BFO', 'MCX']);
});
