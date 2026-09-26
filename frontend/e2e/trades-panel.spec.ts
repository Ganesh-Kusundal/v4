import { expect, test, type Page, type APIRequestContext } from '@playwright/test';

const APP = '/ui/';
const clearBlob = async (request: APIRequestContext): Promise<void> => {
  for (const interval of ['1m', '5m', '15m', '30m', '1h', 'D']) {
    await request.delete(`/api/charts/workspace/NSE_RELIANCE_${interval}_default`);
  }
};


/** One fill as `/backtest` ships it. */
interface Fill {
  time: number;
  side: string;
  price: number;
  qty: number | null;
  rejected: boolean;
}

interface BacktestPayload {
  metrics: { num_trades: number } | null;
  trades: Fill[];
}

const waitForBars = async (page: Page): Promise<void> => {
  await expect(page.getByText(/\d+\s+bars/).first()).toBeVisible({ timeout: 30_000 });
};

/** Add the backtest pane through the picker, the way a user does. */
const addBacktestPane = async (page: Page): Promise<void> => {
  await page.getByRole('button', { name: 'Indicators' }).click();
  await page.getByRole('option', { name: 'Backtest Equity' }).click();
  await page.getByRole('button', { name: 'Done' }).click();
};

/** The chart's interval pills, which is also the pane's interval. */
const chooseInterval = async (page: Page, interval: string): Promise<void> => {
  await page.getByRole('radio', { name: `Interval ${interval}` }).click();
};

/** The pane's own settings dialog, reached the way a user reaches it. */
const setStrategy = async (page: Page, strategy: string): Promise<void> => {
  await page.getByRole('button', { name: 'Objects' }).click();
  await page
    .locator('.oac-objects__row', { hasText: 'Backtest Equity' })
    .getByRole('button', { name: 'Settings' })
    .click();
  await page.getByLabel('Strategy').selectOption(strategy);
  await page.getByRole('button', { name: 'OK' }).click();
  await page.getByRole('button', { name: 'Close' }).click();
};

/**
 * Add the pane, switch it to `strategy`, and hand back the run it published —
 * from the response itself.
 *
 * The response and not a second identical request: the table renders whatever
 * the pane published, so comparing against anything else would be asserting
 * that two runs agree rather than that the table shows the run that happened.
 *
 * `bracket_breakout` and not the pane's default `sma_cross`: the default is long
 * only, and a table that renders every side's P&L with the wrong sign is exactly
 * the defect a long-only fixture cannot see.
 */
const runAndCapture = async (
  page: Page,
  configure: ReadonlyArray<(page: Page) => Promise<void>> = [
    (p) => setStrategy(p, 'bracket_breakout'),
  ],
): Promise<BacktestPayload> => {
  const posted = (): ReturnType<typeof page.waitForRequest> =>
    page.waitForRequest((r) => r.url().endsWith('/api/charts/backtest') && r.method() === 'POST', {
      timeout: 30_000,
    });
  const first = posted();
  await addBacktestPane(page);
  await first;

  let request = null;
  for (const step of configure) {
    const next = posted();
    await step(page);
    request = await next;
  }
  if (request === null) throw new Error('no run captured');
  const response = await request.response();
  expect(response?.status(), 'the backend must accept the run').toBe(200);
  return (await response?.json()) as BacktestPayload;
};

interface Trip {
  side: 'BUY' | 'SELL';
  entryTime: number;
  entryPrice: number;
  exitTime: number;
  exitPrice: number;
  qty: number;
  amount: number;
}

/**
 * Round trips from a fill list.
 *
 * Deliberately a second implementation of the same rule the app uses: signed lot,
 * weighted-average entry, closed by whatever fill takes it to zero or flips it.
 * If the app's version drifts, this one does not follow it.
 *
 * The limit is worth stating: a rule both implementations get wrong the same way
 * is not caught here. What it does catch is the mapping on top of the rule —
 * direction signs, unit changes, which list a row is built from.
 */
function roundTrips(fills: readonly Fill[]): { trips: Trip[]; open: { side: 'BUY' | 'SELL'; qty: number } | null } {
  const trips: Trip[] = [];
  let qty = 0;
  let avg = 0;
  let since = 0;
  for (const f of fills) {
    if (f.rejected || f.price <= 0) continue;
    const size = Math.abs(f.qty ?? 0);
    if (size === 0) continue;
    const signed = f.side.trim().toLowerCase().startsWith('s') ? -size : size;
    if (qty === 0) {
      qty = signed;
      avg = f.price;
      since = f.time;
      continue;
    }
    if (qty > 0 === signed > 0) {
      avg = (avg * Math.abs(qty) + f.price * size) / (Math.abs(qty) + size);
      qty += signed;
      continue;
    }
    const closed = Math.min(Math.abs(qty), size);
    const direction = qty > 0 ? 1 : -1;
    trips.push({
      side: qty > 0 ? 'BUY' : 'SELL',
      entryTime: since,
      entryPrice: avg,
      exitTime: f.time,
      exitPrice: f.price,
      qty: closed,
      amount: (f.price - avg) * closed * direction,
    });
    qty += signed;
    if (qty !== 0) {
      avg = f.price;
      since = f.time;
    }
  }
  return {
    trips,
    open: qty === 0 ? null : { side: qty > 0 ? 'BUY' : 'SELL', qty: Math.abs(qty) },
  };
}

/** `"1,234.50"`, `"+93.00"`, `"-4.20%"` → a number. */
const dollars = (text: string): number => Number(text.replace(/[^0-9.-]/g, ''));

/** Hash of the biggest canvas — the price pane, where the zones are drawn. */
const pricePaneHash = (page: Page): Promise<string> =>
  page.evaluate(() => {
    const canvas = [...document.querySelectorAll('canvas')].sort(
      (a, b) => b.width * b.height - a.width * a.height,
    )[0];
    const data = canvas.getContext('2d')?.getImageData(0, 0, canvas.width, canvas.height).data;
    if (data === undefined) return 'no-canvas';
    let h = 2166136261;
    for (let i = 0; i < data.length; i += 1) {
      h ^= data[i];
      h = Math.imul(h, 16777619);
    }
    return (h >>> 0).toString(16);
  });

/** Let the render loop paint before reading pixels: it draws on an animation frame. */
const settle = (page: Page): Promise<void> =>
  page.evaluate(() => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))));

// The panel and the bars dock under the chart, so the default 720px viewport
// leaves the price pane too short for a zone to be more than a few pixels.
// Selecting is asserted on pixels, so the chart gets room.
test.use({ viewport: { width: 1440, height: 900 } });

test.describe('per-trade table', () => {
  test.beforeEach(async ({ request }) => {
    await clearBlob(request);
  });
  test.afterEach(async ({ request }) => {
    await clearBlob(request);
  });

  test('lists every round trip of the run, priced from its fills', async ({ page }) => {
    await page.goto(APP);
    await waitForBars(page);

    const payload = await runAndCapture(page);
    expect(payload.metrics, 'the seeded datalake must cover the default window').not.toBeNull();
    const expected = roundTrips(payload.trades);
    expect(expected.trips.length, 'the fixture must produce round trips').toBeGreaterThan(0);

    const panel = page.locator('[data-panel="trades"]');
    // The panel states what it is showing; the rows must agree with it and with
    // the run's own fills.
    await expect(panel).toHaveAttribute('data-trips', String(expected.trips.length));
    await expect(panel).toHaveAttribute('data-open', expected.open === null ? '0' : '1');

    const rows = panel.locator('tbody tr[data-trip]');
    await expect(rows).toHaveCount(expected.trips.length);
    // One evaluate for the whole grid: a cell read per row would be a round trip
    // per row on a run of fifty trades.
    const grid = await panel
      .locator('tbody tr')
      .evaluateAll((trs) =>
        trs.map((tr) => [...tr.querySelectorAll('td')].map((td) => (td.textContent ?? '').trim())),
      );

    // Every row, not a sample: a mismatched row is exactly what this covers.
    expected.trips.forEach((trip, i) => {
      const cells = grid[i];
      expect(cells[0], `row ${i + 1} is numbered`).toBe(String(i + 1));
      expect(cells[1], `row ${i + 1} side`).toBe(trip.side);
      expect(dollars(cells[3]), `row ${i + 1} entry`).toBeCloseTo(trip.entryPrice, 2);
      expect(dollars(cells[5]), `row ${i + 1} exit`).toBeCloseTo(trip.exitPrice, 2);
      expect(dollars(cells[6]), `row ${i + 1} size`).toBeCloseTo(trip.qty, 4);
      // The panel displays P&L rounded to 2dp, so compare against the rounded
      // amount rather than the raw one: -0.125 legitimately renders as -0.13,
      // and a tolerance wide enough to admit that would also admit a real bug.
      const shown = dollars(cells[7]);
      expect(shown, `row ${i + 1} P&L`).toBe(
        Number(trip.amount.toFixed(2)),
      );
      // The sign is the part a mapping bug flips, so it is asserted on the text
      // rather than left to the value's own magnitude.
      expect(cells[7].startsWith('-'), `row ${i + 1} P&L sign`).toBe(trip.amount < 0);
      const pct = ((trip.exitPrice - trip.entryPrice) / trip.entryPrice) * (trip.side === 'BUY' ? 1 : -1) * 100;
      expect(dollars(cells[8]), `row ${i + 1} return`).toBeCloseTo(pct, 2);
    });

    // Both directions must be present, or the sign above proves nothing.
    expect(new Set(expected.trips.map((t) => t.side))).toEqual(new Set(['BUY', 'SELL']));

    // The open lot, when the run leaves one, is a row with no exit to report.
    if (expected.open !== null) {
      const cells = grid[grid.length - 1];
      expect(cells[1]).toBe(expected.open.side);
      expect(dollars(cells[6])).toBeCloseTo(expected.open.qty, 4);
      expect(cells[4], 'an open position has no exit time').toBe('open');
      expect(cells[5], 'an open position has no exit price').toBe('—');
      expect(cells[7], 'an open position has no P&L to report').toBe('—');
    } else {
      expect(grid).toHaveLength(expected.trips.length);
    }
  });

  test('clicking a row marks its zone on the chart, and a second click clears it', async ({
    page,
  }: {
    page: Page;
  }) => {
    await page.goto(APP);
    await waitForBars(page);

    // Hourly bars, so a round trip spans hours and tens of points instead of the
    // two minutes and half a point a 1m window produces here: a sub-pixel zone is
    // emphasised just as correctly and still cannot be seen.
    const payload = await runAndCapture(page, [
      (p) => chooseInterval(p, '1h'),
      (p) => setStrategy(p, 'bracket_breakout'),
    ]);
    expect(roundTrips(payload.trades).trips.length).toBeGreaterThan(0);

    const panel = page.locator('[data-panel="trades"]');
    const row = panel.locator('tbody tr[data-trip]').first();
    await expect(row).toBeVisible();

    // First pick and clear are discarded: picking brings a trade onto the chart
    // when it is not already there, and that scroll repaints the pane for a reason
    // that has nothing to do with the emphasis. Leaving it out means the two
    // hashes below can differ for exactly one reason — the marked zone.
    await row.click();
    await row.click();
    await expect(row).toHaveAttribute('aria-selected', 'false');

    // A live bar update repaints the pane too, and would break the restore
    // comparison below for a reason unrelated to the pick. Re-measuring moves the
    // baseline past it; a stable window is the normal case.
    let restored = false;
    for (let attempt = 0; attempt < 3 && !restored; attempt += 1) {
      await settle(page);
      const idle = await pricePaneHash(page);

      await row.click();
      await expect(row).toHaveAttribute('aria-selected', 'true');
      await expect(panel.getByRole('row', { selected: true })).toHaveCount(1);
      await settle(page);
      // Only the price pane can say this: a zone is canvas geometry, so a
      // selection that reached the pick but never the paint looks identical in
      // the DOM — which is how the statistics block shipped broken once before.
      expect(await pricePaneHash(page), 'the chart must repaint for the picked zone').not.toBe(idle);

      await row.click();
      await expect(row).toHaveAttribute('aria-selected', 'false');
      await expect(panel.locator('tr[aria-selected="true"]')).toHaveCount(0);
      await settle(page);
      // The emphasis repaints the same geometry rather than adding a layer, so
      // clearing it restores the pane exactly.
      restored = (await pricePaneHash(page)) === idle;
    }
    expect(restored, 'clearing the pick must restore the pane').toBe(true);
  });
});
