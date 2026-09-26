import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.goto('/ui/');
});

test('exposes feed readiness status', async ({ page }) => {
  await expect(page.locator('#feed-status')).toHaveText(/ready|stale|reconnecting/i);
});
