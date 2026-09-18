import { expect, test, type Page } from '@playwright/test';

/**
 * Regression cover for the backtest results surface.
 *
 * The chart renders results on canvas, so a browser test cannot count markers or
 * zones. What it *can* observe is the request/response pair that every surface is
 * derived from — and that is exactly where this went wrong once: the statistics
 * block was first written as the descriptor's `attach`, which is also where
 * `createTier2Indicator` puts its fetch/align lifecycle. Spreading over it
 * deleted the fetch, so the pane rendered its legend and then sat empty forever,
 * with no error anywhere. Nothing about the rendered output said "no request was
 * ever made"; a single `POST` assertion does.
 */

const APP = '/ui/';

const waitForBars = async (page: Page): Promise<void> => {
  await expect(page.getByText(/\d+\s+bars/).first()).toBeVisible({ timeout: 30_000 });
};

/** Add the backtest pane through the picker, the way a user does. */
const addBacktestPane = async (page: Page): Promise<void> => {
  await page.getByRole('button', { name: 'Indicators' }).click();
  await page.getByRole('option', { name: 'Backtest Equity' }).click();
  await page.getByRole('button', { name: 'Done' }).click();
};

test.describe('backtest results surface', () => {
  test('adding the pane runs a backtest instead of rendering an empty one', async ({ page }) => {
    await page.goto(APP);
    await waitForBars(page);

    const posted = page.waitForRequest(
      (r) => r.url().endsWith('/api/charts/backtest') && r.method() === 'POST',
      { timeout: 30_000 },
    );
    await addBacktestPane(page);

    const request = await posted;
    const body = request.postDataJSON() as Record<string, unknown>;
    // The window is the chart's own: the pane is pointed at what is on screen.
    expect(body.symbol, 'the run must be for the charted symbol').toBeTruthy();
    expect(body.interval, 'the run must be for the charted interval').toBeTruthy();
    expect(typeof body.from, 'a window is required').toBe('number');
    expect(body.to as number).toBeGreaterThan(body.from as number);
    // A real strategy id from `/strategies`, not the single-item fallback.
    expect(typeof body.strategy).toBe('string');

    const response = await request.response();
    expect(response?.status(), 'the backend must accept the run').toBe(200);
    const payload = (await response?.json()) as { metrics?: Record<string, number> | null };
    // `{metrics: null}` means "no datalake data for this window" — the pane would
    // legitimately draw nothing, which would make the assertion above vacuous.
    expect(payload.metrics, 'the seeded datalake must cover the default window').not.toBeNull();
    expect(typeof payload.metrics?.num_trades).toBe('number');
  });

  test('a declaring strategy ships the levels the chart draws a bracket from', async ({
    request,
  }) => {
    // The chart draws an SL/TP line only where a fill carries a level, so these
    // two assertions are the whole basis of that drawing: the strategy must be
    // offerable, and its fills must carry a side-valid pair.
    const list = await request.get('/api/charts/strategies');
    expect(list.status()).toBe(200);
    const catalogue = (await list.json()) as { backtestable: Array<{ id: string }> };
    expect(catalogue.backtestable.map((s) => s.id)).toContain('bracket_breakout');

    const res = await request.post('/api/charts/backtest', {
      data: {
        exchange: 'NSE',
        symbol: 'RELIANCE',
        interval: '1h',
        strategy: 'bracket_breakout',
        // Short windows on a small fixture: the default 20-bar breakout needs
        // too much history to be sure of a trade here, and an assertion that
        // silently skips is worse than no assertion.
        params: { lookback: 5, atr_period: 5 },
        from: 0,
        to: 2_000_000_000,
      },
    });
    expect(res.status()).toBe(200);
    const body = (await res.json()) as {
      trades: Array<{ side: string; price: number; stop: number | null; target: number | null }>;
    };
    const bracketed = body.trades.filter((t) => t.stop !== null || t.target !== null);
    expect(bracketed.length, 'the fixture must produce a bracketed entry').toBeGreaterThan(0);
    for (const fill of bracketed) {
      expect(fill.stop, 'levels ship as a pair or not at all').not.toBeNull();
      expect(fill.target).not.toBeNull();
      const stop = fill.stop as number;
      const target = fill.target as number;
      // Drawn where they are, so a level on the wrong side of its entry would be
      // a bracket pointing the wrong way — a line the trader reads backwards.
      const [low, high] = fill.side === 'BUY' ? [stop, target] : [target, stop];
      expect(fill.price).toBeGreaterThan(low);
      expect(fill.price).toBeLessThan(high);
    }
  });

  test('the equity curve carries a drawdown column the engine agrees with', async ({ request }) => {
    // The drawdown panel is drawn from a column derived off the shipped equity
    // curve, so the curve and `max_drawdown` must describe the same series. This
    // is asserted against the API rather than through the DOM on purpose: it is
    // the *contract* the panel relies on, and a backend change that broke it
    // would silently make the panel disagree with the statistics block.
    const res = await request.post('/api/charts/backtest', {
      data: {
        exchange: 'NSE',
        symbol: 'RELIANCE',
        interval: '1h',
        strategy: 'sma_cross',
        from: 0,
        to: 2_000_000_000,
      },
    });
    expect(res.status()).toBe(200);
    const body = (await res.json()) as {
      metrics: { max_drawdown: number } | null;
      equity_curve: Array<{ time: number; value: number }>;
    };
    expect(body.metrics, 'the datalake must cover the full history').not.toBeNull();
    expect(body.equity_curve.length).toBeGreaterThan(1);

    let peak = Number.NEGATIVE_INFINITY;
    let worst = 0;
    for (const p of body.equity_curve) {
      peak = Math.max(peak, p.value);
      worst = Math.min(worst, peak === 0 ? 0 : (p.value / peak - 1) * 100);
    }
    expect(body.metrics?.max_drawdown).toBeCloseTo(worst / 100, 12);
  });
});
