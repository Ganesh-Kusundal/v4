import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

/**
 * Regression cover for the restored-viewport defect.
 *
 * A saved chart viewport is a range of *logical bar indices*, so it only means
 * anything against the series it was captured on. The host restored it whenever
 * the layout id matched (`NSE_RELIANCE_1m_default`), and the engine's own guard
 * only compares symbol/exchange/interval — neither can see a dataset that
 * changed *under a stable identity*. A blob saved while the 1m chart had zero
 * bars therefore restored its `{from: -112.47, to: 3}` over ~870 bars and framed
 * the far-left edge, then re-saved it, so the bad view survived every reload.
 *
 * The observable that separates "view kept" from "view corrected" is a *write*:
 * the host persists only when the stored view could not be trusted for the
 * series on screen, so a revision bump means the guard fired. Asserting on
 * revision rather than only on the viewport is what keeps these tests honest —
 * a guard that silently did nothing would leave the seed in place and still
 * satisfy a viewport assertion.
 */

/**
 * Where FastAPI mounts the built frontend. Both this and the API paths below
 * are relative, so Playwright resolves them against the config's `baseURL`.
 * Hardcoding an origin here would silently ignore `E2E_PORT` and pin the suite
 * to one port.
 */
const APP = '/ui/';
/** Layout id the host derives: `${exchange}_${symbol}_${interval}_default`. */
const LAYOUT = 'NSE_RELIANCE_1m_default';

/** The degenerate viewport an empty series persisted. */
const STALE_VIEWPORT = { from: -112.47521515925209, to: 3 };
/** A last-bar time this datalake never produced. */
const ALIEN_SERIES = 1_700_000_000;
/** Long enough for the save debounce plus a round trip to land. */
const SETTLE_MS = 6000;

interface Blob {
  revision?: number;
  data?: {
    v?: number;
    series?: number | null;
    state?: Record<string, unknown>;
    chart?: Record<string, unknown>;
  };
}

const blobUrl = (): string => `/api/charts/workspace/${LAYOUT}`;

const readBlob = async (request: APIRequestContext): Promise<Blob | null> => {
  const res = await request.get(blobUrl());
  if (!res.ok()) return null;
  return (await res.json()) as Blob;
};

/** Seed a blob unconditionally and report the revision it landed at. */
const writeBlob = async (request: APIRequestContext, data: unknown): Promise<number> => {
  const res = await request.put(blobUrl(), { data: { data, revision: null, force: false } });
  expect(res.ok(), `PUT workspace failed: ${res.status()}`).toBeTruthy();
  return ((await res.json()) as { revision: number }).revision;
};

const clearBlob = async (request: APIRequestContext): Promise<void> => {
  for (const interval of ['1m', '5m', '15m', '30m', '1h', 'D']) {
    await request.delete(`/api/charts/workspace/NSE_RELIANCE_${interval}_default`);
  }
};

/** The stored chart state, tolerating either blob shape. */
const chartOf = (blob: Blob | null): Record<string, unknown> | undefined => {
  const data = blob?.data;
  if (data === undefined) return undefined;
  return data.state?.chart ?? data.chart;
};

/** The drawings the blob carries. `chart.drawings` is the draw tier's own slot. */
const drawingsOf = (blob: Blob | null): unknown[] => {
  const slot = chartOf(blob)?.drawings as { drawings?: unknown[] } | undefined;
  return slot?.drawings ?? [];
};

const viewportOf = (blob: Blob | null): { from?: number; to?: number } | undefined =>
  chartOf(blob)?.viewport as { from?: number; to?: number } | undefined;

/** Wait until the chart has real bars on screen. */
const waitForBars = async (page: Page): Promise<void> => {
  await expect(page.getByText(/\d+\s+bars/).first()).toBeVisible({ timeout: 30_000 });
};

/**
 * Load the app once so it writes a genuine, validly-fingerprinted blob, then
 * hand back that blob's state. Seeding a synthetic state instead would risk a
 * state the engine refuses to apply, which would pass the assertions for the
 * wrong reason.
 */
const captureRealState = async (page: Page, request: APIRequestContext): Promise<Blob> => {
  await clearBlob(request);
  await page.goto(APP);
  await waitForBars(page);
  await expect.poll(async () => (await readBlob(request))?.data?.v, { timeout: 30_000 }).toBe(2);
  const blob = await readBlob(request);
  expect(blob?.data?.series, 'a captured blob must carry a series fingerprint').toBeTruthy();
  return blob as Blob;
};

test.describe('workspace restore: the view belongs to the series', () => {
  test.afterEach(async ({ request }) => {
    await clearBlob(request);
  });

  test('the first view of a layout is persisted with a fingerprint', async ({ page, request }) => {
    // Nothing stored: the reference case that makes every other test possible.
    // The widget emits `layout` only for structural changes, so before this was
    // fixed a layout visited but never edited left no blob at all.
    await clearBlob(request);
    await page.goto(APP);
    await waitForBars(page);

    await expect.poll(async () => (await readBlob(request))?.data?.v, { timeout: 30_000 }).toBe(2);
    const blob = await readBlob(request);
    expect(blob?.data?.series, 'the recorded view must name its series').toBeTruthy();
    expect(chartOf(blob), 'the layout itself must be stored').toBeTruthy();
  });

  test('a viewport captured against another dataset is not restored', async ({ page, request }) => {
    const captured = await captureRealState(page, request);

    // Same layout, same symbol/interval — only the dataset behind it is
    // different. This is precisely what the engine's identity check cannot see.
    const seeded: Blob = {
      ...captured,
      data: {
        ...captured.data,
        series: ALIEN_SERIES,
        state: {
          ...captured.data?.state,
          chart: { ...chartOf(captured), viewport: STALE_VIEWPORT },
        },
      },
    };
    const seededRevision = await writeBlob(request, seeded.data);

    await page.goto(APP);
    await waitForBars(page);

    // A corrected view is written back, which is the guard having fired.
    await expect
      .poll(async () => (await readBlob(request))?.revision, { timeout: 30_000 })
      .toBeGreaterThan(seededRevision);

    const after = await readBlob(request);
    expect(viewportOf(after)?.from).not.toBe(STALE_VIEWPORT.from);
    // ...and it now names the series it actually belongs to.
    expect(after?.data?.series).not.toBe(ALIEN_SERIES);
  });

  test('a viewport that matches the series is restored', async ({ page, request }) => {
    const captured = await captureRealState(page, request);

    // An in-range window over the ~870 loaded bars, with the fingerprint the
    // capture recorded — so this view genuinely does belong to the series.
    const wanted = { from: 300, to: 450 };
    // The control that keeps this test from passing vacuously: the engine's own
    // fit sits at the right edge of the loaded bars, so if the view were ignored
    // (or stripped) the restored blob could not read back as `wanted`.
    expect(viewportOf(captured)?.from, 'engine fit must differ from the seed').not.toBe(
      wanted.from,
    );
    const seeded: Blob = {
      ...captured,
      data: {
        ...captured.data,
        state: { ...captured.data?.state, chart: { ...chartOf(captured), viewport: wanted } },
      },
    };
    await writeBlob(request, seeded.data);

    await page.goto(APP);
    await waitForBars(page);
    // Give any write-back the debounce plus a round trip to land, so the value
    // read below is the host's settled opinion rather than the seed.
    await page.waitForTimeout(SETTLE_MS);

    // Proves the guard is a comparison, not an unconditional strip: a view that
    // belongs to the series is applied and kept.
    const after = await readBlob(request);
    expect(viewportOf(after)?.from).toBe(wanted.from);
    expect(after?.data?.series, 'the kept blob must stay fingerprinted').toBeTruthy();
  });

  test('a divergent localStorage state cannot influence the layout', async ({ page, request }) => {
    const captured = await captureRealState(page, request);
    const stored = captured.data?.state as Record<string, unknown> | undefined;

    // The widget's own namespace, exactly as `persist: true` addressed it:
    // `STORAGE_PREFIX + 'default' + ':' + STATE_KEY`. Seed it with a state that
    // disagrees with the server blob about the instrument. This is what the
    // widget used to restore from, *before* the server blob was applied over it,
    // which is how one layout could be restored from two sources that disagreed.
    const KEY = 'oac-widget:default:state';
    const divergent = { ...stored, symbol: 'TCS', interval: '5m' };
    await page.addInitScript(
      (v: { key: string; value: string }) => window.localStorage.setItem(v.key, v.value),
      { key: KEY, value: JSON.stringify(divergent) },
    );

    await page.goto(APP);
    await waitForBars(page);

    // The chart is on the instrument the *server* stored, not localStorage's.
    await expect(page.getByText(/RELIANCE/).first()).toBeVisible();
    await expect(page.getByText(/TCS/)).toHaveCount(0);

    // And nothing wrote the key back: the widget's store is not in the loop at
    // all, so it can neither supply nor shadow chart state.
    const untouched = await page.evaluate((k: string) => window.localStorage.getItem(k), KEY);
    expect(untouched).toBe(JSON.stringify(divergent));
  });

  test('drawings reach the single store', async ({ page, request }) => {
    await clearBlob(request);
    await page.goto(APP);
    await waitForBars(page);
    await expect.poll(async () => (await readBlob(request))?.data?.v, { timeout: 30_000 }).toBe(2);
    expect(drawingsOf(await readBlob(request))).toHaveLength(0);

    // The draw tier emits `draw:add`/`draw:update` but never `objects:change`,
    // and the widget's `layout` event covers only the latter. So a host that
    // saves on `layout` alone drops drawings entirely — they used to survive on
    // localStorage, which is no longer a store. Drag a trend line and require it
    // to land in the blob.
    await page.getByRole('button', { name: 'Trend Line' }).click();
    const chart = page.getByRole('application', { name: 'Price chart' });
    const box = await chart.boundingBox();
    expect(box, 'the chart must be laid out before it can be drawn on').not.toBeNull();
    const { x, y, width, height } = box as { x: number; y: number; width: number; height: number };
    await page.mouse.move(x + width * 0.3, y + height * 0.3);
    await page.mouse.down();
    await page.mouse.move(x + width * 0.6, y + height * 0.5, { steps: 12 });
    await page.mouse.up();

    await expect
      .poll(async () => drawingsOf(await readBlob(request)).length, { timeout: 30_000 })
      .toBeGreaterThan(0);
  });

  test('a legacy blob without a fingerprint keeps the layout and drops the view', async ({
    page,
    request,
  }) => {
    const captured = await captureRealState(page, request);

    // Exactly what the pre-fix build stored: the bare engine state, no envelope.
    const legacy = {
      ...captured.data?.state,
      chart: { ...chartOf(captured), viewport: STALE_VIEWPORT },
    };
    const seededRevision = await writeBlob(request, legacy);

    await page.goto(APP);
    await waitForBars(page);

    // The view cannot be trusted, so it is recomputed and rewritten...
    await expect
      .poll(async () => (await readBlob(request))?.revision, { timeout: 30_000 })
      .toBeGreaterThan(seededRevision);

    const after = await readBlob(request);
    expect(viewportOf(after)?.from).not.toBe(STALE_VIEWPORT.from);
    // ...and the blob is upgraded to the fingerprinted format, so the next
    // restore can decide properly instead of guessing.
    expect(after?.data?.v).toBe(2);
    expect(after?.data?.series).toBeTruthy();
    // The layout survives the strip: only the view is discarded.
    expect(chartOf(after), 'panes/indicators must outlive a stripped view').toBeTruthy();
  });

  test('delayed initial bars never create a null workspace fingerprint', async ({ page, request }) => {
    await clearBlob(request);
    const puts: Array<{ data?: { series?: unknown }; revision?: unknown; force?: unknown }> = [];
    let markChartRequest: (() => void) | undefined;
    const chartRequested = new Promise<void>((resolve) => {
      markChartRequest = resolve;
    });
    let delayed = false;
    await page.route('**/api/charts/history/**', async (route) => {
      const url = new URL(route.request().url());
      if (!delayed && url.searchParams.has('from')) {
        delayed = true;
        markChartRequest?.();
        await new Promise((resolve) => setTimeout(resolve, 9000));
      }
      await route.continue();
    });
    await page.route('**/api/charts/workspace/**', async (route) => {
      if (route.request().method() === 'PUT') {
        puts.push(route.request().postDataJSON() as (typeof puts)[number]);
      }
      await route.continue();
    });

    await page.goto(APP);
    await chartRequested;
    await page.waitForTimeout(7500);
    expect(puts).toHaveLength(0);

    await waitForBars(page);
    await expect.poll(async () => (await readBlob(request))?.data?.v, { timeout: 30_000 }).toBe(2);
    const after = await readBlob(request);
    expect(after?.data?.series).toBeTruthy();
    expect(puts.length).toBeGreaterThan(0);
    expect(puts.every((body) => typeof body.data?.series === 'number')).toBe(true);
  });

  test('a concurrent revision conflict keeps the local save and surfaces a conflict', async ({
    page,
    request,
  }) => {
    const captured = await captureRealState(page, request);
    const seededRevision = await writeBlob(request, captured.data);
    const puts: Array<{ revision?: unknown; force?: unknown }> = [];
    let release: (() => void) | undefined;
    const hold = new Promise<void>((resolve) => {
      release = resolve;
    });
    let markFirstPut: (() => void) | undefined;
    const firstPut = new Promise<void>((resolve) => {
      markFirstPut = resolve;
    });
    await page.route('**/api/charts/workspace/**', async (route) => {
      if (route.request().method() === 'PUT') {
        puts.push(route.request().postDataJSON() as (typeof puts)[number]);
        if (puts.length === 1) {
          markFirstPut?.();
          await hold;
        }
      }
      await route.continue();
    });

    await page.goto(APP);
    await waitForBars(page);
    await page.getByRole('button', { name: 'Trend Line' }).click();
    const chart = page.getByRole('application', { name: 'Price chart' });
    const box = await chart.boundingBox();
    expect(box).not.toBeNull();
    const { x, y, width, height } = box as { x: number; y: number; width: number; height: number };
    await page.mouse.move(x + width * 0.3, y + height * 0.3);
    await page.mouse.down();
    await page.mouse.move(x + width * 0.6, y + height * 0.5, { steps: 12 });
    await page.mouse.up();

    await firstPut;
    const serverData = {
      ...captured.data,
      state: { ...captured.data?.state, conflictMarker: 'server' },
    };
    await writeBlob(request, serverData);
    release?.();

    await expect(page.getByText(/workspace conflict/i).first()).toBeVisible({ timeout: 15_000 });
    await page.waitForTimeout(2500);
    expect(puts).toHaveLength(1);
    expect(puts[0]?.revision).toBe(seededRevision);
    expect(puts[0]?.force).toBe(false);
    const after = await readBlob(request);
    expect(after?.revision).toBe(seededRevision + 1);
    expect((after?.data?.state as { conflictMarker?: unknown } | undefined)?.conflictMarker).toBe('server');

    const overwrite = page.getByRole('button', { name: 'Overwrite' });
    await expect(overwrite).toBeVisible();
    await overwrite.click();
    await expect.poll(() => puts.length).toBe(2);
    expect(puts[1]?.force).toBe(true);
    await expect.poll(async () => (await readBlob(request))?.revision).toBe(seededRevision + 2);
  });

  test('a rejected restore is replaced with a usable workspace', async ({ page, request }) => {
    const captured = await captureRealState(page, request);
    const invalid = {
      ...captured.data,
      state: { ...captured.data?.state, version: 99 },
    };
    const seededRevision = await writeBlob(request, invalid);
    await page.goto(APP);
    await waitForBars(page);

    await expect
      .poll(async () => (await readBlob(request))?.revision, { timeout: 30_000 })
      .toBeGreaterThan(seededRevision);
    const after = await readBlob(request);
    expect(after?.data?.v).toBe(2);
    expect((after?.data?.state as { version?: unknown } | undefined)?.version).not.toBe(99);
  });
});
