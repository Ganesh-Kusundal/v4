import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

/**
 * Regression cover for comparing two backtest runs on one chart.
 *
 * The defect this replaces: a chart held one run — `latest` — which every pane
 * overwrote, so with two Backtest Equity panes the price pane's markers, zones
 * and brackets described **whichever fetch finished last**. Nothing on screen
 * said which, and nothing could be asserted: the identity of the drawn run
 * depended on request timing.
 *
 * What a browser test can observe here is the strip that now owns that decision
 * (its own DOM) plus the request/response pairs each row's numbers come from.
 * The chart is canvas, so "which run is drawn" is asserted through the strip's
 * attributes plus a hash of the price pane — the only place a moved overlay
 * exists — and every run's *statistics* are compared against the payload the pane
 * itself received, not against a second, independently-computed request.
 */

const APP = '/ui/';
/** Layout id the host derives: `${exchange}_${symbol}_${interval}_default`. */
const LAYOUT = 'NSE_RELIANCE_1m_default';

interface Fill {
  time: number;
  side: string;
  price: number;
  qty: number | null;
  rejected: boolean;
}

interface Metrics {
  total_return: number;
  sharpe: number;
  max_drawdown: number;
  num_trades: number;
  num_rejected: number;
  total_fees: number;
}

interface BacktestPayload {
  metrics: Metrics | null;
  trades: Fill[];
}

const waitForBars = async (page: Page): Promise<void> => {
  await expect(page.getByText(/\d+\s+bars/).first()).toBeVisible({ timeout: 30_000 });
};

/**
 * Empty the stored layout between tests.
 *
 * The suite runs one server for the whole file, and the workspace blob is the
 * single source of chart state — so a test that leaves three Backtest panes
 * behind would have the next one boot *into* them, and every count in this file
 * is a count of panes.
 */
const clearBlob = async (request: APIRequestContext): Promise<void> => {
  await request.delete(`/api/charts/workspace/${LAYOUT}`);
};

/** Add a Backtest Equity pane through the picker, the way a user does. */
const addBacktestPane = async (page: Page): Promise<void> => {
  await page.getByRole('button', { name: 'Indicators' }).click();
  await page.getByRole('option', { name: 'Backtest Equity' }).click();
  await page.getByRole('button', { name: 'Done' }).click();
};

/**
 * Point one pane at a strategy, through its settings dialog.
 *
 * `row` selects the pane by its position in the Objects inventory, which is
 * `chart.indicators()` order — instance creation order, i.e. the order the panes
 * were added. The rows show no strategy, so an index is the only handle they
 * offer; the caller asserts the *outcome* (which run appeared) rather than
 * trusting the index.
 */
const setStrategy = async (page: Page, strategy: string, row = 0): Promise<void> => {
  await page.getByRole('button', { name: 'Objects' }).click();
  await page
    .locator('.oac-objects__row', { hasText: 'Backtest Equity' })
    .nth(row)
    .getByRole('button', { name: 'Settings' })
    .click();
  await page.getByLabel('Strategy').selectOption(strategy);
  await page.getByRole('button', { name: 'OK' }).click();
  await page.getByRole('button', { name: 'Close' }).click();
};

/** Remove one pane, from the same inventory. */
const removePane = async (page: Page, row: number): Promise<void> => {
  await page.getByRole('button', { name: 'Objects' }).click();
  await page
    .locator('.oac-objects__row', { hasText: 'Backtest Equity' })
    .nth(row)
    .getByRole('button', { name: /^Remove / })
    .click();
  await page.getByRole('button', { name: 'Close' }).click();
};

/** Hash of the biggest canvas — the price pane, where the overlays are drawn. */
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

const runsPanel = (page: Page) => page.locator('[data-panel="runs"]');
const runRows = (page: Page) => runsPanel(page).locator('tbody tr[data-run]');
const ownerRow = (page: Page) => runsPanel(page).locator('tbody tr[data-owner="1"]');
const tradePanel = (page: Page) => page.locator('[data-panel="trades"]');

/** The strategy `/backtest` defaults to, i.e. what a fresh pane runs. */
const defaultStrategy = async (request: APIRequestContext): Promise<string> => {
  const res = await request.get('/api/charts/strategies');
  expect(res.status()).toBe(200);
  const body = (await res.json()) as { backtestable: Array<{ id: string }> };
  const ids = body.backtestable.map((s) => s.id);
  expect(ids.length).toBeGreaterThanOrEqual(3);
  return ids[0];
};

/** A live record of every backtest response, keyed by the strategy it ran. */
const recordRuns = (page: Page): Map<string, BacktestPayload> => {
  const seen = new Map<string, BacktestPayload>();
  page.on('response', (res) => {
    if (!res.url().endsWith('/api/charts/backtest') || res.request().method() !== 'POST') return;
    if (!res.ok()) return;
    const sent = JSON.parse(res.request().postData() ?? '{}') as { strategy?: string };
    void res.json().then(
      (payload: BacktestPayload) => {
        if (typeof sent.strategy === 'string') seen.set(sent.strategy, payload);
      },
      () => {},
    );
  });
  return seen;
};

/** The strip's numbers, formatted the way the pane's statistics block does. */
const pct = (v: number): string => `${(v * 100).toFixed(2)}%`;
const fixed = (v: number): string => v.toFixed(2);
const dollars = (text: string): number => Number(text.replace(/[^0-9.-]/g, ''));

test.describe('comparing backtest runs', () => {
  test.afterEach(async ({ request }) => {
    await clearBlob(request);
  });

  test('a run is its strategy, so two panes on one strategy are one run and one overlay', async ({
    page,
  }) => {
    await clearBlob(page.request);
    await page.goto(APP);
    await waitForBars(page);

    const seen = recordRuns(page);
    await addBacktestPane(page);
    await addBacktestPane(page);
    await expect.poll(() => seen.size, { timeout: 30_000 }).toBe(1);
    await expect(runsPanel(page)).toBeVisible();

    // Both panes ran the same strategy, so there is one run — the alternative
    // (a row per pane) would show the same run twice and let one of them own the
    // chart with a letter the other also claims.
    await expect(runRows(page)).toHaveCount(1);
    await expect(runsPanel(page)).toHaveAttribute('data-runs', 'A');
    await expect(ownerRow(page)).toHaveAttribute('data-run', [...seen.keys()][0]);
  });

  test('two strategies are two runs, side by side with their own metrics', async ({
    page,
    request,
  }) => {
    await clearBlob(request);
    await page.goto(APP);
    await waitForBars(page);

    const first = await defaultStrategy(request);
    const other = 'bracket_breakout';
    expect(other, 'the fixture needs two distinct strategies').not.toBe(first);

    const seen = recordRuns(page);
    await addBacktestPane(page);
    await addBacktestPane(page);
    await setStrategy(page, other, 1);
    await expect.poll(() => seen.size, { timeout: 30_000 }).toBe(2);

    await expect(runRows(page)).toHaveCount(2);
    await expect(runsPanel(page)).toHaveAttribute('data-runs', 'AB');
    // The first strategy to register owns the price pane; the second is a row.
    await expect(ownerRow(page)).toHaveAttribute('data-run', first);
    await expect(runsPanel(page)).toHaveAttribute('data-owner_strategy', first);

    // Every cell of both rows against the payload that row's own pane received:
    // a row count would pass for a strip of the right length showing another
    // run's numbers.
    const grid = await runsPanel(page)
      .locator('tbody tr')
      .evaluateAll((trs) =>
        trs.map((tr) => [...tr.querySelectorAll('td')].map((td) => (td.textContent ?? '').trim())),
      );
    const strategies = [first, other];
    strategies.forEach((strategy, i) => {
      const payload = seen.get(strategy);
      expect(payload?.metrics, `${strategy} must have produced a run`).not.toBeNull();
      const m = payload?.metrics as Metrics;
      const cells = grid[i];
      // Column order: Net Profit, Sharpe, Max Drawdown, Trades, Fees.
      expect(dollars(cells[0]), `${strategy} net profit`).toBeCloseTo(m.total_return * 100, 2);
      expect(dollars(cells[1]), `${strategy} sharpe`).toBeCloseTo(m.sharpe, 2);
      expect(dollars(cells[2]), `${strategy} max drawdown`).toBeCloseTo(m.max_drawdown * 100, 2);
      expect(Number(cells[3]), `${strategy} trades`).toBe(m.num_trades);
      expect(dollars(cells[4]), `${strategy} fees`).toBeCloseTo(m.total_fees, 2);
      // Textual equality with the pane's own block, not just numeric: the strip
      // is the second rendering of one engine value, and a reformat is a bug.
      expect(cells[0]).toBe(pct(m.total_return));
      expect(cells[1]).toBe(fixed(m.sharpe));
      expect(cells[2]).toBe(pct(m.max_drawdown));
      expect(cells[4]).toBe(fixed(m.total_fees));
    });
  });

  test('the strip decides what the price pane draws, and the trades table follows', async ({
    page,
    request,
  }) => {
    await clearBlob(request);
    await page.goto(APP);
    await waitForBars(page);

    const first = await defaultStrategy(request);
    const other = 'bracket_breakout';
    const seen = recordRuns(page);
    await addBacktestPane(page);
    await addBacktestPane(page);
    await setStrategy(page, other, 1);
    await expect.poll(() => seen.size, { timeout: 30_000 }).toBe(2);
    await expect(runRows(page)).toHaveCount(2);

    // The table describes the run on the chart: a table of a run whose zones are
    // not drawn would be rows that select nothing.
    await expect(tradePanel(page)).toHaveAttribute('data-strategy', first);
    await settle(page);
    const withFirst = await pricePaneHash(page);

    // Hand the price pane to the other run. The strip is DOM; the overlay is a
    // canvas, so the pixels are the only place the move can be seen.
    await ownerRow(page).getByRole('button', { name: 'Chart' }).click();
    await expect(ownerRow(page)).toHaveAttribute('data-run', other);
    await expect(tradePanel(page)).toHaveAttribute('data-strategy', other);
    let moved = false;
    for (let attempt = 0; attempt < 3 && !moved; attempt += 1) {
      await settle(page);
      moved = (await pricePaneHash(page)) !== withFirst;
    }
    expect(moved, 'the overlay must repaint when the owner changes').toBe(true);

    // The other run is beside it now, not instead of it.
    await settle(page);
    const beforeCompare = await pricePaneHash(page);
    await runRows(page)
      .filter({ hasText: '' })
      .first()
      .getByRole('button', { name: 'Compare' })
      .click();
    await expect(runsPanel(page)).toHaveAttribute('data-compare', 'A');
    let drew = false;
    for (let attempt = 0; attempt < 3 && !drew; attempt += 1) {
      await settle(page);
      drew = (await pricePaneHash(page)) !== beforeCompare;
    }
    expect(drew, 'a comparison run must reach the price pane').toBe(true);

    // Removing the comparison leaves the owner's overlay exactly as it was: the
    // comparison adds a layer, it does not restyle the run underneath.
    await runRows(page)
      .first()
      .getByRole('button', { name: 'Compare' })
      .click();
    await expect(runsPanel(page)).toHaveAttribute('data-compare', '');
    let restored = false;
    for (let attempt = 0; attempt < 3 && !restored; attempt += 1) {
      await settle(page);
      restored = (await pricePaneHash(page)) === beforeCompare;
    }
    expect(restored, 'clearing the comparison must restore the owner’s pane').toBe(true);
  });

  test('a newly published run never takes the overlay from the run that owns it', async ({
    page,
    request,
  }) => {
    await clearBlob(request);
    await page.goto(APP);
    await waitForBars(page);

    const ids = await request.get('/api/charts/strategies');
    const catalogue = (await ids.json()) as { backtestable: Array<{ id: string }> };
    const [first, second, third] = catalogue.backtestable.map((s) => s.id);
    expect(new Set([first, second, third]).size, 'the fixture needs three strategies').toBe(3);

    await addBacktestPane(page);
    await addBacktestPane(page);
    await setStrategy(page, second, 1);
    await expect(runRows(page)).toHaveCount(2);

    // Give the chart to the *second* run, so the next publish is a different run
    // than the owner — which is the exact shape of the old bug.
    await ownerRow(page).getByRole('button', { name: 'Chart' }).click();
    await expect(runsPanel(page)).toHaveAttribute('data-owner_strategy', second);

    const seen = recordRuns(page);
    await addBacktestPane(page);
    await setStrategy(page, third, 2);
    await expect.poll(() => seen.get(third) !== undefined, { timeout: 30_000 }).toBe(true);

    await expect(runRows(page)).toHaveCount(3);
    await expect(runsPanel(page)).toHaveAttribute('data-runs', 'ABC');
    // The publish landed and the overlay did not move: ownership is state, not
    // an ordering.
    await expect(runsPanel(page)).toHaveAttribute('data-owner_strategy', second);
    await expect(runsPanel(page)).toHaveAttribute('data-compare', '');
    await expect(
      runRows(page).filter({ has: page.getByRole('button', { name: /Compare C/ }) }),
    ).toHaveCount(1);
    await expect(
      runRows(page).filter({ has: page.getByRole('button', { name: /Compare C/ }) }).getByRole('button', { name: 'Chart' }),
    ).toHaveAttribute('aria-pressed', 'false');
  });

  test('closing the owner’s pane hands the price pane to the earliest remaining run', async ({
    page,
    request,
  }) => {
    await clearBlob(request);
    await page.goto(APP);
    await waitForBars(page);

    const ids = await request.get('/api/charts/strategies');
    const catalogue = (await ids.json()) as { backtestable: Array<{ id: string }> };
    const [first, second, third] = catalogue.backtestable.map((s) => s.id);

    await addBacktestPane(page);
    await addBacktestPane(page);
    await setStrategy(page, second, 1);
    await addBacktestPane(page);
    await setStrategy(page, third, 2);
    await expect(runRows(page)).toHaveCount(3);
    await expect(runsPanel(page)).toHaveAttribute('data-runs', 'ABC');

    // ABC with C on the chart; closing C's pane must not leave the overlay
    // describing a run whose producer is gone.
    await ownerRow(page).getByRole('button', { name: 'Chart' }).click();
    await expect(runsPanel(page)).toHaveAttribute('data-owner_strategy', third);
    await removePane(page, 2);

    await expect(runRows(page)).toHaveCount(2);
    await expect(runsPanel(page)).toHaveAttribute('data-runs', 'AB');
    // The earliest remaining run, not the most recent publish.
    await expect(runsPanel(page)).toHaveAttribute('data-owner_strategy', first);

    // And with every pane gone the overlay clears rather than stranding on a
    // dead run: the strip hides, and the trades table goes back to its hint.
    await removePane(page, 1);
    await removePane(page, 0);
    await expect(runRows(page)).toHaveCount(0);
    await expect(runsPanel(page)).toBeHidden();
    await expect(tradePanel(page)).toHaveAttribute('data-strategy', '');
  });
});
