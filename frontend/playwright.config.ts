import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end tests for the TradeX chart host.
 *
 * The app under test is the real stack: the FastAPI session serving the built
 * `frontend/dist`, backed by the parquet datalake. `webServer` boots it if
 * nothing is already listening, so a run is self-contained; with a server up it
 * is reused, which keeps a dev session's state (and its port) untouched.
 *
 * `webServer.command` seeds a datalake fixture before serving. That is not
 * belt-and-braces: `data/ohlcv/` is gitignored, so CI and a fresh clone have no
 * market data at all, and without bars every browser test would pass against
 * "no bars for this range" without exercising anything. The seeder no-ops when
 * the symbol already has bars, so a developer with the full lake is unaffected.
 */
// Not 8000: the suite always starts its own server, so it must not collide with
// a developer's `serve` on the default port.
const PORT = Number(process.env.E2E_PORT ?? 8123);
const BASE = `http://127.0.0.1:${PORT}`;

// Relative to this config's directory (`frontend/`). `uv sync` creates the venv
// at the repo root, so this resolves in CI and locally alike; the override
// exists so a differently-placed interpreter never has to be edited in here.
const PYTHON = process.env.E2E_PYTHON ?? '../.venv/bin/python';
// Post-extraction package layout: each top-level package is its own src root.
// `trading/src` is still listed for the shim tree and its test-only helpers.
const PYTHONPATH = [
  '../domain/src',
  '../brokers/src',
  '../execution/src',
  '../reactive/src',
  '../runtime/src',
  '../interfaces/src',
  '../market_data/src',
  '../config/src',
  '../strategy/src',
  '../replay/src',
  '../persistence/src',
  '../observability/src',
  '../application/src',
  '../trading/src',
].join(':');

const serve = (script: string): string => `env PYTHONPATH=${PYTHONPATH} ${PYTHON} ${script}`;

// The server writes its workspace sqlite relative to its own cwd, which is
// `frontend/` here — that left an untracked `frontend/runtime/workspace.sqlite`
// behind after every run. Temp state keeps the checkout clean and stops the
// suite from reading or writing the server blobs of a developer's dev session.
// The path is deliberately stable across runs so a failed run's state survives
// for inspection; the specs clear the layouts they use.
const STATE_DIR = process.env.E2E_STATE_DIR ?? join(tmpdir(), 'tradex-e2e-state');

export default defineConfig({
  testDir: './e2e',
  // The suite tests `/ui/`, which is `frontend/dist` on disk — so it verifies that
  // dist is a build of the current sources before any of it runs. Without that,
  // a run through the raw `playwright test` command asserts against whatever
  // bundle was last built.
  globalSetup: './e2e/global-setup.ts',
  // One server and one set of workspace rows behind every test: run in order.
  fullyParallel: false,
  workers: 1,
  // A stray `test.only` would silently turn the suite into one test.
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : [['list']],
  use: {
    // The origin, not the mount: a leading-slash path in `goto` resolves against
    // the origin and would discard a `/ui/` in `baseURL`, loading the app's
    // un-mounted root (whose `/assets/…` requests 404). Tests name `/ui/`.
    baseURL: BASE,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: [
      serve('../trading/scripts/seed_e2e_datalake.py'),
      serve(`-m tradex_interfaces.cli serve --broker paper --port ${PORT}`),
    ].join(' && '),
    url: `${BASE}/health/ready`,
    env: {
      ...process.env,
      TRADEX_RUNTIME_DIR: STATE_DIR,
      TRADEX_WORKSPACE_DB: join(STATE_DIR, 'workspace.sqlite'),
    },
    // Always fresh, never reused. A regression suite must not depend on some
    // other process's state: the workspace blob is the single source of chart
    // state, and which layout is active is "the most recently saved one", so a
    // dev server's database would decide what these tests boot into. Reusing a
    // server also hides a broken boot, which is the failure CI exists to catch.
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
});
