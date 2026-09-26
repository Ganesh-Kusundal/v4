import { expect, test, type Page, type WebSocketRoute } from '@playwright/test';

type WireFrame = Record<string, unknown>;

type ReplayHarness = {
  sent: WireFrame[];
  send: (frame: WireFrame) => void;
  close: () => Promise<void>;
};

const RUN_ID = 'replay-test-run';

const replayBar = (time: number, close: number): WireFrame => ({
  type: 'bar',
  instrument: 'NSE:RELIANCE',
  interval: '1m',
  time,
  open: close,
  high: close + 2,
  low: close - 2,
  close,
  volume: 100,
  closed: true,
  source: 'sim',
  run_id: RUN_ID,
});

const openReplayHarness = async (page: Page): Promise<ReplayHarness> => {
  const sent: WireFrame[] = [];
  let socket: WebSocketRoute | null = null;
  await page.routeWebSocket(/\/ws\/stream/, (route) => {
    socket = route;
    route.onMessage((message) => {
      const frame = JSON.parse(typeof message === 'string' ? message : message.toString()) as WireFrame;
      sent.push(frame);
      if (frame.type === 'replay_start') {
        route.send(JSON.stringify({
          type: 'replay_started',
          run_id: RUN_ID,
          instrument: 'NSE:RELIANCE',
          interval: '1m',
          start_time: 1,
          total_bars: 3,
        }));
      } else if (frame.type === 'replay_pause') {
        route.send(JSON.stringify({ type: 'replay_paused', run_id: RUN_ID }));
      } else if (frame.type === 'replay_resume') {
        route.send(JSON.stringify({ type: 'replay_resumed', run_id: RUN_ID }));
      } else if (frame.type === 'replay_step') {
        route.send(JSON.stringify({ type: 'replay_stepped', run_id: RUN_ID, bar_index: 1 }));
      }
    });
  });
  await page.route(/\/api\/charts\/workspace$/, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.goto('/ui/');
  await expect.poll(() => socket !== null).toBe(true);
  await expect(page.locator('#feed-status')).toHaveText('Feed ready');
  return {
    sent,
    send: (frame) => {
      if (socket === null) throw new Error('replay socket is not connected');
      socket.send(JSON.stringify(frame));
    },
    close: async () => {
      if (socket !== null) await socket.close({ code: 1001, reason: 'test disconnect' });
    },
  };
};

const settleChart = async (page: Page): Promise<void> => {
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
};

const chartHash = async (page: Page): Promise<number> => {
  await settleChart(page);
  return page.evaluate(() => {
    const canvas = [...document.querySelectorAll('canvas')]
      .sort((left, right) => right.width * right.height - left.width * left.height)[0];
    if (canvas === undefined) return 0;
    const bytes = canvas.getContext('2d')?.getImageData(0, 0, canvas.width, canvas.height).data;
    if (bytes === undefined) return 0;
    let hash = 2166136261;
    for (const byte of bytes) hash = Math.imul(hash ^ byte, 16777619);
    return hash >>> 0;
  });
};

test('enables the replay start control', async ({ page }) => {
  await page.goto('/ui/');
  await expect(page.locator('#replay-start')).toBeEnabled();
});

test('starts and stops against the server replay protocol', async ({ page }) => {
  await page.route(/\/api\/charts\/workspace$/, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.goto('/ui/');
  await expect(page.locator('#feed-status')).toHaveText('Feed ready');
  await page.locator('#replay-start').click();
  await expect(page.locator('#replay-scrub')).toBeEnabled();
  await expect(page.locator('#arm')).toBeDisabled();
  await page.locator('#replay-stop').click();
  await expect(page.locator('#replay-start')).toBeEnabled();
  await expect(page.locator('#replay-scrub')).toBeDisabled();
  await expect(page.locator('#arm')).toBeEnabled();
});

test('drives replay by target index and restores the live chart on done', async ({ page }) => {
  const harness = await openReplayHarness(page);
  const liveHash = await chartHash(page);
  await page.locator('#replay-start').click();
  await expect(page.locator('#replay-scrub')).toBeEnabled();
  await expect(page.locator('#replay-scrub')).toHaveAttribute('max', '2');
  await expect(page.locator('#replay-count')).toHaveText('1 / 3');
  await expect(page.locator('#arm')).toBeDisabled();
  const replayHash = await chartHash(page);
  expect(replayHash).not.toBe(liveHash);

  await page.locator('#replay-pause').click();
  await page.locator('#replay-resume').click();
  await page.locator('#replay-step').click();
  await expect(page.locator('#replay-count')).toHaveText('2 / 3');
  await expect.poll(() => harness.sent.map((frame) => frame.type)).toEqual(
    expect.arrayContaining(['replay_pause', 'replay_resume', 'replay_step']),
  );

  await page.locator('#replay-scrub').evaluate((input) => {
    const scrub = input as HTMLInputElement;
    scrub.value = '2';
    scrub.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await expect.poll(() => harness.sent.find((frame) => frame.type === 'replay_seek')).toEqual({
    type: 'replay_seek',
    index: 2,
  });

  harness.send(replayBar(120, 120));
  await expect.poll(() => chartHash(page)).not.toBe(replayHash);
  const activeSimHash = await chartHash(page);
  harness.send({ ...replayBar(150, 150), source: 'live' });
  harness.send({ ...replayBar(150, 150), run_id: 'stale-run' });
  harness.send({ ...replayBar(150, 150), run_id: undefined });
  await expect.poll(() => chartHash(page)).toBe(activeSimHash);
  const steppedHash = await chartHash(page);
  harness.send({
    type: 'replay_seeked',
    run_id: RUN_ID,
    index: 2,
    bars: [replayBar(180, 180)],
  });
  await expect.poll(() => chartHash(page)).not.toBe(steppedHash);

  harness.send({ type: 'replay_done', run_id: RUN_ID });
  await expect(page.locator('#replay-start')).toBeEnabled();
  await expect(page.locator('#replay-scrub')).toBeDisabled();
  await expect(page.locator('#arm')).toBeEnabled();
  await expect.poll(() => chartHash(page)).not.toBe(steppedHash);
  harness.send({ type: 'replay_stopped', run_id: RUN_ID });
  await expect(page.locator('#replay-start')).toBeEnabled();
});

test('stops replay when the chart symbol or interval changes', async ({ page }) => {
  const harness = await openReplayHarness(page);
  const status = page.locator('[data-panel="replay"] .v4-dock__error');
  await page.locator('#replay-start').click();
  await expect(page.locator('#replay-scrub')).toBeEnabled();
  await page.locator('.oac-topbar').getByRole('radio', { name: 'Interval 5m' }).click();
  await expect.poll(() => harness.sent.find((frame) => frame.type === 'replay_stop')).toEqual({
    type: 'replay_stop',
  });
  await expect(page.locator('#replay-start')).toBeEnabled();
  await expect(page.locator('#replay-scrub')).toBeDisabled();
  await expect(page.locator('#feed-status')).not.toHaveText('Replay active');
  await expect(status).toContainText(/symbol or interval changed/i);

  await expect(page.locator('#feed-status')).toHaveText('Feed ready');
  await page.locator('#replay-start').click();
  await expect(page.locator('#replay-scrub')).toBeEnabled();
  const symbol = page.locator('.oac-topbar').getByRole('textbox', { name: 'Symbol' });
  await symbol.fill('TCS');
  await symbol.press('Enter');
  await expect.poll(() => harness.sent.filter((frame) => frame.type === 'replay_stop')).toHaveLength(2);
  await expect(page.locator('#replay-start')).toBeEnabled();
  await expect(page.locator('#replay-scrub')).toBeDisabled();
  await expect(status).toContainText(/symbol or interval changed/i);
});

test('ignores stale or unscoped replay frames during the active run', async ({ page }) => {
  const harness = await openReplayHarness(page);
  await page.locator('#replay-start').click();
  await expect(page.locator('#replay-scrub')).toBeEnabled();
  const replayHash = await chartHash(page);
  harness.send({ type: 'replay_loading' });
  harness.send({ type: 'replay_loading', run_id: 'stale-run' });
  harness.send({ type: 'replay_started', run_id: 'stale-run', start_time: 1, total_bars: 99 });
  harness.send({ type: 'replay_done' });
  harness.send({ type: 'replay_stopped', run_id: 'stale-run' });
  harness.send({ type: 'replay_error', message: 'stale error' });
  harness.send({ type: 'error', message: 'stale generic error' });
  harness.send({ type: 'replay_paused' });
  harness.send({ type: 'replay_resumed', run_id: 'stale-run' });
  harness.send({ type: 'replay_stepped', bar_index: 2 });
  harness.send({ type: 'replay_seeked', index: 2, bars: [replayBar(180, 180)] });
  harness.send(replayBar(120, 120));
  await expect.poll(() => chartHash(page)).not.toBe(replayHash);
  await expect(page.locator('#replay-count')).toHaveText('1 / 3');
  await expect(page.locator('#replay-start')).toBeDisabled();
  await expect(page.locator('#arm')).toBeDisabled();
  await expect(page.locator('[data-panel="replay"] .v4-dock__error')).toBeHidden();
  harness.send({ type: 'replay_done', run_id: RUN_ID });
  await expect(page.locator('#replay-start')).toBeEnabled();
});

test('cleans replay state immediately when the socket closes', async ({ page }) => {
  const harness = await openReplayHarness(page);
  await page.locator('#replay-start').click();
  await expect(page.locator('#replay-scrub')).toBeEnabled();
  await harness.close();
  await expect(page.locator('#replay-start')).toBeEnabled({ timeout: 500 });
  await expect(page.locator('#replay-scrub')).toBeDisabled();
  await expect(page.locator('#feed-status')).not.toHaveText('Replay active');
});

test('restores guarded controls after a replay error', async ({ page }) => {
  const harness = await openReplayHarness(page);
  await page.locator('#replay-start').click();
  await expect(page.locator('#replay-scrub')).toBeEnabled();
  harness.send({ type: 'replay_error', run_id: RUN_ID, message: 'synthetic failure' });
  await expect(page.locator('#replay-start')).toBeEnabled();
  await expect(page.locator('#replay-scrub')).toBeDisabled();
  await expect(page.locator('#arm')).toBeEnabled();
  await expect(page.locator('#feed-status')).toHaveText('Feed ready');
});
