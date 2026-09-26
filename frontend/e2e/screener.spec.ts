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

  test('renders the empty state for a zero-candidate scan and clears loading', async ({ page }) => {
    await page.route('**/api/charts/strategies', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          scanners: [{ id: 'mock', universe_size: 1, conditions: ['close > 100'], rank_by: 'score', limit: 10 }],
          backtestable: [{ id: 'sma_cross', params: [] }],
        }),
      });
    });
    await page.route('**/api/charts/scanner/run', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ scanner: 'mock', window_days: 30, results: [] }),
      });
    });

    await page.goto('/ui/');
    const panel = page.locator('[data-panel="screener"]');
    await expect(panel).toBeVisible();
    await expect.poll(() => panel.locator('select option').count()).toBe(1);
    await panel.locator('button', { hasText: 'Run scan' }).click();

    await expect(panel.locator('.v4-dock__empty')).toContainText(/no candidates matched/i);
    await expect(panel.locator('.v4-dock__empty')).toBeVisible();
    await expect(panel.locator('.v4-dock__loading')).toBeHidden();
  });

  test('recovers from a failed scan and clears the stale error', async ({ page }) => {
    let attempts = 0;
    await page.route('**/api/charts/strategies', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          scanners: [{ id: 'mock', universe_size: 1, conditions: ['close > 100'], rank_by: 'score', limit: 10 }],
          backtestable: [{ id: 'sma_cross', params: [] }],
        }),
      });
    });
    await page.route('**/api/charts/scanner/run', async (route) => {
      attempts += 1;
      if (attempts === 1) {
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ error: { message: 'temporary scan failure' } }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          scanner: 'mock',
          window_days: 30,
          results: [{
            symbol: 'RELIANCE',
            exchange: 'NSE',
            score: 92.5,
            matched: ['close > 100'],
            values: { close: 105 },
            rank: 1,
          }],
        }),
      });
    });

    await page.goto('/ui/');
    const panel = page.locator('[data-panel="screener"]');
    await expect(panel).toBeVisible();
    await expect.poll(() => panel.locator('select option').count()).toBe(1);
    const runBtn = panel.locator('button', { hasText: 'Run scan' });
    const error = panel.locator('.v4-dock__error');
    await runBtn.click();

    await expect(error).toContainText(/temporary scan failure/i);
    await expect(error).toBeVisible();
    await expect(panel.locator('.v4-dock__loading')).toBeHidden();

    await runBtn.click();
    await expect(panel.locator('tbody tr')).toHaveCount(1);
    await expect(error).toBeHidden();
    await expect(panel.locator('.v4-dock__loading')).toBeHidden();
    expect(attempts).toBe(2);
  });

  test('shows only the empty state when no scanners are discovered', async ({ page }) => {
    await page.route('**/api/charts/strategies', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ scanners: [], backtestable: [{ id: 'sma_cross', params: [] }] }),
      });
    });

    await page.goto('/ui/');
    const panel = page.locator('[data-panel="screener"]');
    await expect(panel).toBeVisible();
    await expect(panel.locator('.v4-dock__empty')).toContainText(/no scanners discovered/i);
    await expect(panel.locator('.v4-dock__empty')).toBeVisible();
    await expect(panel.locator('.v4-dock__loading')).toBeHidden();
    await expect(panel.locator('.v4-dock__error')).toBeHidden();
  });

  test('shows only the error state when scanner discovery fails', async ({ page }) => {
    await page.route('**/api/charts/strategies', async (route) => {
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ error: { message: 'scanner catalogue unavailable' } }),
      });
    });

    await page.goto('/ui/');
    const panel = page.locator('[data-panel="screener"]');
    await expect(panel).toBeVisible();
    await expect(panel.locator('.v4-dock__error')).toContainText(/strategies 503/i);
    await expect(panel.locator('.v4-dock__error')).toBeVisible();
    await expect(panel.locator('.v4-dock__loading')).toBeHidden();
    await expect(panel.locator('.v4-dock__empty')).toBeHidden();
  });
});
