import { expect, test } from '@playwright/test';

/**
 * Screener panel: the backend's discovered scanners are listed, one can be
 * run, and the resulting candidates carry a "Chart" action that flips the
 * widget to that symbol.
 */

test.describe('screener panel', () => {
  test('lists discovered scanners and runs one', async ({ page }) => {
    await page.goto('/ui/');
    // The screener panel loads its scanners asynchronously.
    const panel = page.locator('[data-panel="screener"]');
    await expect(panel).toBeVisible();

    // The select should list the discovered scanners.
    const select = panel.locator('select');
    // Options exist in the DOM even when the dropdown is closed — poll count.
    await expect.poll(async () => select.locator('option').count(), { timeout: 15_000 }).toBeGreaterThan(0);

    const firstValue = await select.locator('option').first().getAttribute('value');
    expect(firstValue).toBeTruthy();
    await select.selectOption(firstValue!);

    const runBtn = panel.locator('button', { hasText: 'Run scan' });
    await runBtn.click();

    // Results table should appear with at least one candidate row.
    const tbody = panel.locator('tbody');
    await expect(tbody.locator('tr').first()).toBeVisible({ timeout: 30_000 });

    // Each candidate row has a "Chart" button.
    const chartBtn = tbody.locator('tr').first().locator('button', { hasText: 'Chart' });
    await expect(chartBtn).toBeVisible();
  });

  test('Chart action flips the widget to the candidate symbol', async ({ page }) => {
    await page.goto('/ui/');
    const panel = page.locator('[data-panel="screener"]');
    await expect(panel).toBeVisible();

    const select = panel.locator('select');
    await expect.poll(async () => select.locator('option').count(), { timeout: 15_000 }).toBeGreaterThan(0);
    const firstValue = await select.locator('option').first().getAttribute('value');
    expect(firstValue).toBeTruthy();
    await select.selectOption(firstValue!);
    await panel.locator('button', { hasText: 'Run scan' }).click();

    const tbody = panel.locator('tbody');
    await expect(tbody.locator('tr').first()).toBeVisible({ timeout: 30_000 });

    // Read the symbol from the row, click Chart, verify the chart loads it.
    const firstRow = tbody.locator('tr').first();
    const symbolCell = firstRow.locator('td').nth(1);
    const candidateSymbol = await symbolCell.textContent();
    expect(candidateSymbol).toBeTruthy();

    await firstRow.locator('button', { hasText: 'Chart' }).click();

    // The widget fires a 'symbol' event and reloads history for the new symbol.
    await expect.poll(async () => {
      const resp = await page.request.get(`/api/charts/history/NSE:${candidateSymbol!.trim()}?interval=1m&limit=1`);
      const data = await resp.json();
      return (data.bars?.length ?? 0) > 0;
    }, { timeout: 15_000 }).toBe(true);
  });
});
