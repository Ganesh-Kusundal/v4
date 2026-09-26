# Phased App Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the live-trading safety, authentication-boundary, replay, feed, account, workspace, and UI regressions found in the 2026-09-23 review without discarding the current uncommitted pivot.

**Architecture:** Use safety-first vertical slices. A small app-level replay guard protects HTTP order mutations; a server-owned replay run owns its dataset, cursor, and aggregator; the chart data controller gets an explicit replay lifecycle; feed history remains authoritative and resyncs after stream gaps. The current API-key model remains loopback/development-only, with non-loopback live binds rejected until the follow-up authentication design exists.

**Tech Stack:** Python 3.11+, FastAPI, Uvicorn, asyncio, pytest, React-free TypeScript/JavaScript modules, Vite, Playwright, openalgo-charts 2.3.2, Vitest.

## Global Constraints

- Preserve the nine existing uncommitted files; never reset, stash, or overwrite unrelated user work.
- Do not commit, amend, or push unless the user explicitly requests it.
- Keep the product policy INTRADAY-only; do not add CNC/NRML product handling.
- Keep the API-key/page-injection model only for trusted loopback development; live non-loopback binds fail closed.
- Replay cleanup must clear live suppression and order-guard ownership on every terminal path.
- Replay step completion must match the active run's instrument, interval, run ID, and `source="sim"`.
- A current-bucket bar without authoritative current-bucket data is provisional and must trigger a bounded resync; never silently publish it as finalized OHLCV.
- Do not silently overwrite a newer workspace revision; surface a conflict.
- Do not add a new frontend unit-test dependency; use the existing openalgo-charts Vitest suite and frontend Playwright suite.
- Do not add comments to source files; keep the existing code style and error-handling conventions.
- Use standard-library/project utilities before adding dependencies.

---

## File Map and Boundaries

### Safety and server boundary

- Create `trading/src/tradex_trading/interface/replay_guard.py`: one thread-safe app/session-level replay ownership registry.
- Modify `trading/src/tradex_trading/interface/fastapi_app.py`: construct the guard, pass it to order and WebSocket routers, and validate live bind hosts.
- Modify `trading/src/tradex_trading/interface/routes/orders.py`: reject mutations while the guard is active; preserve idempotency and INTRADAY behavior.
- Modify `trading/src/tradex_trading/interface/routes/stream.py`: own the replay run, acquire/release the guard, marshal bar callbacks to the event loop, and preserve replay metadata.
- Create `trading/src/tradex_trading/interface/replay_run.py`: immutable replay dataset/run state and target-bar cursor helpers, isolated from the WebSocket transport.
- Modify `trading/src/tradex_trading/runtime/bar_aggregator.py`: add an explicit current-bucket seed/reset contract and preserve frame metadata.
- Create `trading/tests/interface/test_replay_guard.py` and `trading/tests/interface/test_live_bind_policy.py`.
- Extend `trading/tests/interface/test_ws_bars_replay.py` with terminal cleanup, fresh-aggregator, target-bar cursor, malformed-parameter, and live-recovery cases.

### Frontend order/account surface

- Create `frontend/src/order-safety.ts`: pure order-intent validation and a small control-state contract.
- Modify `frontend/index.html`: add explicit INTRADAY, quantity, arm, readiness, and mode controls.
- Modify `frontend/src/main.ts`: wire guarded context-menu orders, replay/feed state, latest quote state, account callbacks, and volume updates.
- Modify `frontend/src/replay.ts`: expose replay state changes and use server target-bar indices.
- Modify `frontend/src/account-panel.ts`: restore cancel, modify, and position-exit actions with idempotency keys and visible failures.
- Modify `frontend/src/theme.css`: style the compact order controls and status states.
- Create `frontend/e2e/order-safety.spec.ts` and `frontend/e2e/account-actions.spec.ts`.

### Chart/feed integration

- Modify `frontend/src/feed.ts`: forward abort signals and subscription metadata, preserve bar metadata, repair gaps, remove the global anchor, and provide explicit page exhaustion.
- Modify `frontend/src/main.ts`: enable bounded controller repair options and update the volume series from controller snapshots.
- Modify `openalgo-charts/src/feed/types.ts`: extend bar metadata with replay/provisional fields and the replay lifecycle contract.
- Modify `openalgo-charts/src/feed/data-controller.ts`: add replay snapshot/restore/replace operations while preserving existing refresh behavior.
- Modify `openalgo-charts/src/widget/widget.ts`: expose replay lifecycle and exchange-selector options.
- Modify `openalgo-charts/src/widget/topbar.ts`: render an explicit exchange selector.
- Modify `openalgo-charts/tests/data-controller-repair.test.ts`, `openalgo-charts/tests/feed-cache.test.ts`, and `openalgo-charts/tests/widget-data-loading.test.ts` for the new contracts.
- Create `frontend/e2e/replay.spec.ts` and `frontend/e2e/feed-recovery.spec.ts`.

### Workspace, panels, and cleanup

- Modify `frontend/src/workspace.ts`: wait for a real series before saving, preserve revision conflicts, and honor restore failures.
- Modify `frontend/src/screener.ts`: make loading/empty/error states exclusive and recoverable.
- Modify `frontend/src/main.ts`: bound startup probes and fall back to the default instrument.
- Modify `openalgo-charts/src/widget/widget.ts` and `frontend/src/main.ts` for explicit exchange propagation.
- Modify `trading/src/tradex_trading/interface/routes/chart.py` only where the symbol-search contract must preserve a real exchange; do not broaden authentication in this phase.
- Delete only modules proven unreferenced by the import audit: `frontend/src/chart-lifecycle.ts`, `frontend/src/chart-state.ts`, `frontend/src/chart-types.ts`, and any additional orphan confirmed by the audit.
- Update `docs/superpowers/specs/2026-09-23-phased-app-remediation-design.md` and add a production-authentication follow-up document without implementing that follow-up.

---

### Task 0: Capture the Baseline and Test Harness

**Files:**
- Read: `docs/superpowers/specs/2026-09-23-phased-app-remediation-design.md`
- Read: `pyproject.toml`, `frontend/package.json`, `openalgo-charts/package.json`
- Create: `frontend/e2e/replay.spec.ts`
- Create: `frontend/e2e/order-safety.spec.ts`
- Create: `frontend/e2e/feed-recovery.spec.ts`
- Create: `frontend/e2e/account-actions.spec.ts`

**Interfaces:**
- Produces a recorded baseline command log and four stable E2E suites that later tasks extend.
- Does not change production code.

- [ ] **Step 1: Record the user-owned worktree state**

Run:

```text
git status --short --untracked-files=all
git diff --stat
git diff --check
```

Expected: the nine existing modified files are recorded, and `git diff --check` exits successfully.

- [ ] **Step 2: Record baseline verification**

Run from the repository root:

```text
.venv/bin/python -m pytest trading/tests/interface trading/tests/runtime -p no:cacheprovider --import-mode=importlib -c pyproject.toml
cd frontend && npm run typecheck && npm run build
cd ../openalgo-charts && npm run test
```

Expected: record the exact pass/failure counts. Do not hide existing failures; carry them into the phase test matrix.

- [ ] **Step 3: Add a minimal E2E server fixture contract**

Use the existing `frontend/playwright.config.ts` and `frontend/e2e/global-setup.ts`; the configured `webServer` already seeds the datalake and starts the paper FastAPI server. Each new suite must begin with:

```ts
import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.goto('/ui/');
});
```

All suites navigate to `/ui/`; the application currently treats the hash as a non-routing fragment, so stable element IDs and `data-testid` attributes provide test-specific context without changing production routing.

- [ ] **Step 4: Add failing smoke assertions for the missing regressions**

Add assertions for:

```ts
await expect(page.getByRole('checkbox', { name: /arm trading/i })).toBeVisible();
await expect(page.locator('#replay-start')).toBeEnabled();
await expect(page.locator('#feed-status')).toHaveText(/ready|stale|reconnecting/i);
await expect(page.locator('#account-orders')).toBeVisible();
```

Run the four E2E files. Expected: at least the new order/replay assertions fail against the current implementation, proving the tests exercise the defects.

- [ ] **Step 5: Record the baseline checkpoint**

Save the command output in the task notes or PR description, not in generated repository files. Do not commit.

---

### Task 1: Add the Replay Guard and Enforce It at the HTTP Order Boundary

**Files:**
- Create: `trading/src/tradex_trading/interface/replay_guard.py`
- Modify: `trading/src/tradex_trading/interface/fastapi_app.py:166-183`
- Modify: `trading/src/tradex_trading/interface/routes/orders.py:38-50, 285-497`
- Create: `trading/tests/interface/test_replay_guard.py`
- Modify: `trading/tests/interface/test_fastapi_app.py` only for the new keyed replay-order case

**Interfaces:**
- Produces `ReplayGuard.acquire(run_id: str) -> None`, `ReplayGuard.release(run_id: str) -> None`, `ReplayGuard.active -> bool`, and an app-level guard instance shared by the WebSocket and orders routers.
- `create_app(session: Any | None = None, api_key: str | None = None, outbound_max: int = OUTBOUND_QUEUE_MAX, replay_guard: ReplayGuard | None = None)` accepts an injected guard for integration tests and constructs one when omitted.
- Consumes the existing `session.mode` and order idempotency contract.

- [ ] **Step 1: Write the failing guard unit test**

```python
from tradex_trading.interface.replay_guard import ReplayGuard


def test_replay_guard_blocks_until_all_run_ids_are_released():
    guard = ReplayGuard()
    guard.acquire("run-a")
    guard.acquire("run-b")
    assert guard.active is True
    guard.release("run-a")
    assert guard.active is True
    guard.release("run-b")
    assert guard.active is False


def test_replay_guard_release_is_idempotent():
    guard = ReplayGuard()
    guard.acquire("run-a")
    guard.release("run-a")
    guard.release("run-a")
    assert guard.active is False
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```text
.venv/bin/python -m pytest trading/tests/interface/test_replay_guard.py -p no:cacheprovider --import-mode=importlib -c pyproject.toml
```

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement the minimal thread-safe guard**

Create a `threading.Lock`-protected set of active run IDs. `acquire` must add a non-empty ID, `release` must discard it, and `active` must read the set under the lock. Do not couple the guard to asyncio or the session object.

- [ ] **Step 4: Add the route-level failing test**

Extend the test with a paper session and a guard that has an active run. Submit a valid order with `X-API-Key` and `Idempotency-Key`; assert HTTP 422 and the detail `orders are disabled during replay`. Release the run and assert the request reaches the normal paper-session path.

- [ ] **Step 5: Wire one guard through `create_app`**

Construct one `ReplayGuard` in `create_app`, pass it to `create_orders_router`, and pass the same instance to the WebSocket router. Change the router factory signatures with keyword-only arguments so existing test factories remain explicit.

- [ ] **Step 6: Make `_require_trading_mode` consult the guard**

Keep the existing `session.mode == "replay"` check. Add:

```python
if replay_guard.active:
    raise HTTPException(
        status_code=422,
        detail="orders are disabled during replay",
    )
```

Do not add a second mode value or infer replay from the frontend.

- [ ] **Step 7: Run the focused tests**

Run:

```text
.venv/bin/python -m pytest trading/tests/interface/test_replay_guard.py trading/tests/interface/test_fastapi_app.py -p no:cacheprovider --import-mode=importlib -c pyproject.toml
```

Expected: PASS, with the existing public-GET policy tests unchanged.

- [ ] **Step 8: Checkpoint the boundary**

Do not commit. Record that the app now has a single shared replay ownership source and that the stream integration is still pending.

---

### Task 2: Enforce the Loopback-Only Live Bind Policy

**Files:**
- Modify: `trading/src/tradex_trading/interface/fastapi_app.py:239-360`
- Create: `trading/tests/interface/test_live_bind_policy.py`
- Create: `trading/tests/interface/test_cli_serve.py`
- Modify: `trading/src/tradex_trading/interface/cli.py:74-99, 370-415` only if the host value must be passed explicitly

**Interfaces:**
- Produces `_require_live_bind_loopback(broker: str | None, host: str) -> None` and an actionable `ValueError` for live non-loopback binds.
- Preserves `_require_api_key_for_live` and the current paper/dev behavior.

- [ ] **Step 1: Write the bind-policy matrix test**

```python
import pytest

from tradex_trading.interface.fastapi_app import _require_live_bind_loopback


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_live_loopback_hosts_are_allowed(host):
    _require_live_bind_loopback("DHAN", host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10"])
def test_live_non_loopback_hosts_are_rejected(host):
    with pytest.raises(ValueError, match="loopback"):
        _require_live_bind_loopback("DHAN", host)


def test_paper_non_loopback_bind_is_not_blocked():
    _require_live_bind_loopback("PAPER", "0.0.0.0")
```

- [ ] **Step 2: Run the test and verify the missing helper failure**

```text
.venv/bin/python -m pytest trading/tests/interface/test_live_bind_policy.py -p no:cacheprovider --import-mode=importlib -c pyproject.toml
```

Expected: collection fails because the helper is not defined.

- [ ] **Step 3: Implement exact host classification**

Normalize the host string, accept only `127.0.0.1`, `::1`, and `localhost`, and reject all other live binds with a message naming the broker and host and recommending a trusted authentication layer. Do not add a bypass environment variable.

- [ ] **Step 4: Apply the check before uvicorn starts**

Call the helper after broker/key validation and before the readiness probe in `start_fastapi_server`. For worker/reload factory mode, store the validated host in the serve environment and call the same helper in `serve_app` before constructing a live session.

- [ ] **Step 5: Add CLI coverage**

Test `cmd_serve` with a mocked `start_fastapi_server` and assert the host and broker are passed unchanged. Assert a live non-loopback request returns a failure code and prints the loopback policy message.

- [ ] **Step 6: Run the bind and CLI tests**

```text
.venv/bin/python -m pytest trading/tests/interface/test_live_bind_policy.py trading/tests/interface/test_cli_serve.py -p no:cacheprovider --import-mode=importlib -c pyproject.toml
```

---

### Task 3: Restore Guarded Order Entry and Account Management

**Files:**
- Create: `frontend/src/order-safety.ts`
- Modify: `frontend/index.html:15-30`
- Modify: `frontend/src/main.ts:30-88, 108-138`
- Modify: `frontend/src/replay.ts:1-150`
- Modify: `frontend/src/account-panel.ts:1-225`
- Modify: `frontend/src/theme.css`
- Modify: `frontend/e2e/order-safety.spec.ts`
- Modify: `frontend/e2e/account-actions.spec.ts`

**Interfaces:**
- `order-safety.ts` produces `validateOrderIntent(intent, state, lastQuote)`, `OrderSafetyState`, and `OrderControls` APIs.
- `main.ts` consumes `OrderRequest` from the chart context menu and calls the existing `/orders`, `/orders/{id}`, and chart account/book read contracts.
- `replay.ts` exposes `onStateChange(state)` and a `replaying` state that the order controls consume.

- [ ] **Step 1: Write E2E assertions for the unsafe current path**

Add tests that:

```ts
test('does not place an order while disarmed', async ({ page }) => {
  await page.goto('/ui/');
  const chart = page.locator('.oac-chart');
  await chart.click({ button: 'right', position: { x: 180, y: 120 } });
  await page.getByRole('button', { name: /market buy/i }).click().catch(() => undefined);
  await expect(page.locator('#order-status')).toContainText(/arm/i);
});

test('rejects an order from an indicator pane', async ({ page }) => {
  await page.goto('/ui/');
  const chart = page.locator('.oac-chart');
  await chart.click({ button: 'right', position: { x: 180, y: 420 } });
  await expect(page.locator('#order-status')).toContainText(/price pane/i);
});
```

Use the chart's rendered pane boundaries to choose the coordinates; do not expose a production debug order hook.

- [ ] **Step 2: Run the E2E tests and verify they fail**

```text
cd frontend && npx playwright test e2e/order-safety.spec.ts --project=chromium
```

Expected: the unarmed and indicator-pane cases fail against the current one-click path.

- [ ] **Step 3: Define the pure validation contract**

```ts
export type OrderSafetyState = {
  armed: boolean;
  ready: boolean;
  replaying: boolean;
  mode: 'paper' | 'live' | 'replay';
  quantity: number;
};

export function validateOrderIntent(
  intent: { side: 'BUY' | 'SELL'; type: 'MARKET' | 'LIMIT' | 'SL'; price?: number; triggerPrice?: number; paneIndex: number },
  state: OrderSafetyState,
  lastQuote: number | null,
): string | null;
```

Return stable error strings for `not armed`, `feed not ready`, `replay active`, `invalid quantity`, `price pane only`, `missing price`, and invalid stop-side relationships. For `BUY`/`SL`, require `triggerPrice < lastQuote`; for `SELL`/`SL`, require `triggerPrice > lastQuote`.

- [ ] **Step 4: Add the controls to the shell**

Add an INTRADAY badge, numeric quantity input defaulting to `1`, `Arm trading` checkbox, mode/readiness label, and a status region with stable IDs. Keep the controls compact and keyboard accessible. `Arm trading` starts unchecked after every reload and returns to unchecked after a successful order.

- [ ] **Step 5: Wire validation and confirmation in `main.ts`**

Before `POST /orders`:

1. read the quantity and arm state;
2. reject a missing latest quote for stop orders;
3. call `validateOrderIntent`;
4. show an exact confirmation string containing `INTRADAY`, instrument, side, type, quantity, and price/trigger;
5. call the existing idempotent POST only after confirmation.

Use the existing `Idempotency-Key` generation. Do not add product fields or a second order endpoint.

- [ ] **Step 6: Disable order controls during replay and stale feeds**

Subscribe to replay state and feed/controller status. Set `ready=false` while replay is active, the WebSocket is reconnecting, or the controller reports `stale`/`error`. Keep the chart visible and show the reason in the status region.

- [ ] **Step 7: Restore account actions**

Add row-level buttons to `account-panel.ts`:

```ts
type AccountPanelOptions = {
  canSubmit: () => boolean;
  onError: (message: string) => void;
};
```

- Cancel: `DELETE /orders/{orderId}` with `X-API-Key` and a fresh `Idempotency-Key`.
- Modify: `PUT /orders/{orderId}` with the existing quantity/price shape and a fresh key.
- Exit: `POST /orders` with the opposite side, `MARKET`, `quantity=Math.abs(netQty)`, no price, and a fresh key.

Only remove a row after a successful response. On failure, retain the row and show the typed server error.

- [ ] **Step 8: Run order/account E2E tests**

```text
cd frontend && npx playwright test e2e/order-safety.spec.ts e2e/account-actions.spec.ts --project=chromium
```

Expected: unarmed, quantity, pane, stop-side, replay-disabled, cancel, modify, and exit cases pass.

---

### Task 4: Replace Replay State with a Server-Owned Run and Terminal Cleanup

**Files:**
- Create: `trading/src/tradex_trading/interface/replay_run.py`
- Modify: `trading/src/tradex_trading/interface/routes/stream.py:404-880`
- Modify: `trading/src/tradex_trading/runtime/bar_aggregator.py:94-192`
- Modify: `trading/tests/interface/test_ws_bars_replay.py`

**Interfaces:**
- `ReplayRun` owns `run_id`, instrument, normalized timeframe, immutable M1 candles, target-bar keys, cursor, speed, pause/step flags, fresh aggregator, and terminal status.
- `create_ws_router(session, metrics, *, replay_guard)` receives the shared guard from Task 1.
- `BarFrame` carries `source`, `closed`, and `run_id` metadata without changing OHLCV fields.

- [ ] **Step 1: Add failing lifecycle tests**

Add five tests with these exact behaviors:

```python
def test_replay_done_releases_live_key_and_guard(replay_fixture):
    replay_fixture.start()
    replay_fixture.finish_naturally()
    assert replay_fixture.guard.active is False
    assert replay_fixture.live_key_is_clear() is True


def test_replay_setup_error_releases_guard(replay_fixture):
    replay_fixture.start_with_empty_history()
    assert replay_fixture.guard.active is False
    assert replay_fixture.last_terminal_frame()["type"] == "replay_error"


def test_replay_task_exception_releases_guard(replay_fixture):
    replay_fixture.start_with_provider_exception()
    assert replay_fixture.guard.active is False
    assert replay_fixture.live_key_is_clear() is True


def test_replay_seek_never_reuses_partial_bucket(replay_fixture):
    replay_fixture.start_and_pause_mid_bar()
    replay_fixture.seek_to_previous_bar()
    first = replay_fixture.first_frame_after_seek()
    assert first["time"] < replay_fixture.partial_bar_time()
    assert first["source"] == "sim"


def test_invalid_replay_parameters_return_error_without_closing_socket(replay_fixture):
    replay_fixture.send({"type": "replay_start", "speed": "not-a-number"})
    assert replay_fixture.socket_is_open() is True
    assert replay_fixture.last_terminal_frame()["type"] == "replay_error"
```

`replay_fixture` must provide the fake datalake, bus, WebSocket test client, and guard described by the existing `_app`/`_patch_lake` helpers. Each test must publish a live quote after terminal cleanup and assert that a live bar frame is delivered.

- [ ] **Step 2: Run the new tests and verify failure**

```text
.venv/bin/python -m pytest trading/tests/interface/test_ws_bars_replay.py -p no:cacheprovider --import-mode=importlib -c pyproject.toml
```

Expected: the new lifecycle cases fail while the existing 16 cases remain the baseline.

- [ ] **Step 3: Define `ReplayRun` with idempotent terminal cleanup**

Use a dataclass with this concrete surface:

```python
@dataclass
class ReplayRun:
    run_id: str
    instrument: str
    timeframe: Timeframe
    candles: tuple[Candle, ...]
    target_keys: tuple[int, ...]
    cursor: int = 0
    speed: float = 1.0
    paused: bool = False
    step_requested: bool = False
    aggregator: BarAggregator | None = None
    task: asyncio.Task[None] | None = None
    terminal: bool = False
```

Provide `target_index_for_key`, `close`, and `cancel_and_drain` helpers. `close` must be safe to call more than once.

- [ ] **Step 4: Validate replay input before suppression**

Reject non-finite or out-of-range speed/ticks/minutes and unsupported methods with an error frame. Load the replay dataset before acquiring the replay guard. A failed load must not set a live-suppression key.

- [ ] **Step 5: Create a fresh aggregator for every run**

Do not read or write `bar_aggregators` for replay frames. Use a run-owned `BarAggregator`; live aggregators remain untouched. Flush and dispose only the run aggregator in cleanup.

- [ ] **Step 6: Make cleanup universal**

Use one `finally`-style path for normal completion, explicit stop, setup error, task exception, and socket teardown. It must cancel/drain the task, clear the run key, release the replay guard exactly once, and emit one terminal frame when the socket is writable.

- [ ] **Step 7: Scope step closure to the run**

A frame completes a step only when:

```python
frame.source == "sim" and frame.run_id == run.run_id
and frame.instrument == run.instrument
and frame.timeframe == run.timeframe.value
and frame.closed is True
```

- [ ] **Step 8: Marshal live bar callbacks to the event loop**

Replace the direct bus subscription with a callback that schedules the actual `_on_bar_quote` call through the WebSocket loop's `call_soon_threadsafe`. Keep the existing indicator/control callback wrappers.

- [ ] **Step 9: Run the complete replay test module**

```text
.venv/bin/python -m pytest trading/tests/interface/test_ws_bars_replay.py trading/tests/runtime/test_bar_aggregator.py -p no:cacheprovider --import-mode=importlib -c pyproject.toml
```

Expected: PASS, including fresh-aggregator, guard-release, error, step-scope, and live-recovery cases.

---

### Task 5: Implement Target-Bar Replay Semantics and Chart Snapshot/Restore

**Files:**
- Modify: `trading/src/tradex_trading/interface/routes/stream.py:675-853`
- Modify: `frontend/src/replay.ts:1-150`
- Modify: `frontend/src/main.ts:93-138`
- Modify: `openalgo-charts/src/feed/types.ts`
- Modify: `openalgo-charts/src/feed/data-controller.ts:100-180, 325-430`
- Modify: `openalgo-charts/src/widget/widget.ts:127-162`
- Modify: `openalgo-charts/tests/data-controller-repair.test.ts`
- Create: `openalgo-charts/tests/replay-data-controller.test.ts`
- Modify: `frontend/e2e/replay.spec.ts`

**Interfaces:**
- `replay_started` reports `run_id`, `interval`, `start_time`, and `total_bars`, where `total_bars` is the number of target-interval bars.
- `replay_seek` accepts `bar_index: number`; the client no longer computes `start_time + 60 * index`.
- `DataLoadingController.enterReplay(runId: string, bars: readonly Bar[])`, `replaceReplayBars(bars: readonly Bar[])`, `pushReplayBar(bar: Bar, meta?: LiveBarMeta)`, and `exitReplay()` preserve the live bar store.
- `Widget.enterReplay(runId: string)` snapshots the chart view and delegates the bar lifecycle; `Widget.exitReplay()` restores the view and delegates the bar restore.

- [ ] **Step 1: Add failing chart-controller tests**

Test that a live series `[10:00, 10:05, 10:10]` enters replay at `10:00`, accepts a synthetic `10:00` bar, hides later live bars, seeks to `10:05`, and restores the exact original bars and viewport after exit. Assert that `source`, `closed`, and `run_id` reach the listener.

- [ ] **Step 2: Run the focused chart tests and verify failure**

```text
cd openalgo-charts && npm run test -- tests/replay-data-controller.test.ts
```

Expected: FAIL because no replay lifecycle API exists.

- [ ] **Step 3: Add replay lifecycle state to `DataLoadingController`**

Store a private `_liveBars`, `_replayBars`, and `_replayRunId`. `enterReplay` snapshots the current bar store, pauses live stream delivery, and publishes an explicitly marked replay series. `pushReplayBar` accepts only the active run and bypasses the older-than-tail rejection. `exitReplay` restores the live bar store, clears replay state, and restarts the stream. The widget, not the data controller, owns viewport snapshot/restore.

- [ ] **Step 4: Expose the methods through `Widget`**

Add methods to the `Widget` interface and implementation. `enterReplay` snapshots the current chart state through the chart's state API, delegates bar storage to `dataController.enterReplay`, and records the active run ID. `exitReplay` delegates the bar restore and restores the chart state. Return `false` or throw a typed error when the widget has no managed data controller; the host must not silently use a different series owner.

- [ ] **Step 5: Change replay cursor messages**

Update the server to calculate `target_keys` from the requested interval's bucket boundaries. `replay_seek` maps `bar_index` to the first M1 candle in that target bucket. Preserve paused state and speed unless the client explicitly changes them.

- [ ] **Step 6: Update `replay.ts` state handling**

Handle `replay_loading`, `replay_started`, `replay_stepped`, `replay_done`, `replay_error`, and `replay_stopped`. Call the chart lifecycle methods from the main host. Enable the scrubber only after `total_bars` is assigned. Send `bar_index`, not a synthetic minute timestamp.

- [ ] **Step 7: Add browser replay coverage**

Cover:

```text
start -> scrubber enabled
start -> pause/resume
step -> one target bar
seek backward -> future bars hidden
seek forward -> correct bar
done -> live series restored
error -> live series restored and order controls re-enabled only after feed ready
socket close -> replay cleanup
```

- [ ] **Step 8: Run chart and browser tests**

```text
cd openalgo-charts && npm run test -- tests/replay-data-controller.test.ts tests/data-controller-repair.test.ts
cd ../frontend && npx playwright test e2e/replay.spec.ts --project=chromium
```

Expected: PASS.

---

### Task 6: Preserve Live Bar Metadata, Seed/Resync, and Repair Stream Gaps

**Files:**
- Modify: `frontend/src/feed.ts:90-500`
- Modify: `frontend/src/main.ts:93-138`
- Modify: `openalgo-charts/src/feed/types.ts:38-74`
- Modify: `openalgo-charts/src/feed/data-controller.ts` only if the metadata contract requires a new field
- Modify: `trading/src/tradex_trading/interface/routes/stream.py:429-444, 521-570`
- Modify: `trading/src/tradex_trading/runtime/bar_aggregator.py:101-192`
- Modify: `openalgo-charts/tests/feed-cache.test.ts`
- Modify: `openalgo-charts/tests/data-controller-repair.test.ts`
- Modify: `frontend/e2e/feed-recovery.spec.ts`

**Interfaces:**
- `BarSocket` preserves `source`, `closed`, `provisional`, and `run_id` in the callback metadata.
- `V4DataFeed.subscribeBars` forwards `BarSubscriptionOptions`, including `seedFrom`, `onResync`, and a `BarSubscriptionSeed` shaped as `{ lastClosed?: Bar; currentBucket?: Bar }` when the client has authoritative current-bucket data.
- `V4DataFeed.getBarsPage` returns `{ bars: [], hasMore: false }` for exhausted history instead of throwing `NotFoundError`.
- `DataLoadingController` receives `pollIntervalMs`, `refreshOnBarClose`, and `refreshOnGap` from `main.ts`.

- [ ] **Step 1: Add failing feed tests**

Add tests that:

1. abort an old history request and assert it does not update the active symbol;
2. load an older page and assert it cannot move the newest anchor backward;
3. request an empty page before the oldest bar and assert `hasMore=false` with no error;
4. reconnect after a dropped bar and assert an authoritative refresh occurs before ready;
5. receive a provisional live frame and assert a later history refresh repairs its open/high/low/volume.

- [ ] **Step 2: Run the tests and verify failure**

```text
cd openalgo-charts && npm run test -- tests/feed-cache.test.ts tests/data-controller-repair.test.ts
```

Expected: new cases fail because the current feed discards options and treats empty pages as errors.

- [ ] **Step 3: Make history cancellation and anchors per request**

Replace the module-global `datalakeClockSec` with a map keyed by `exchange:symbol:interval`. Only move a key forward. Pass `req.signal` into the fetch signal composition. Do not use another symbol's anchor as a silent fallback.

- [ ] **Step 4: Implement `getBarsPage` exhaustion semantics**

Calculate a fixed interval window ending at `before - 1`, return normalized bars oldest-first, and set `hasMore` from the returned count. An empty response must be a valid exhausted page, not a thrown error.

- [ ] **Step 5: Forward subscription options and bar metadata**

Change the internal bar callback type to carry a metadata object. `V4DataFeed` forwards `opts` to the WebSocket subscription; `BarSocket` sends `seed` and `provisional` metadata; the server returns the same fields in every bar frame. A run with no current-bucket snapshot marks its first live frames provisional.

- [ ] **Step 6: Add current-bucket seed/resync behavior**

When a current-bucket snapshot is available, initialize the server aggregator from that snapshot. When it is not available, send provisional frames and schedule the existing `refreshOnBarClose`/`refreshOnGap` repair. Do not label the provisional bar final based only on a last closed bar.

- [ ] **Step 7: Enable bounded recovery in `main.ts`**

Use a fixed interval refresh configuration:

```ts
loading: {
  timeoutMs: 10_000,
  pollIntervalMs: 30_000,
  refreshOnBarClose: { delayMs: 2_500, retries: 2, retryDelayMs: 5_000 },
  refreshOnGap: true,
  refreshWindowBars: 200,
}
```

Use `withBarCache` so the live WebSocket does not duplicate a history request. On WS close, call the controller's resync callback before declaring the feed ready again.

- [ ] **Step 8: Update volume from the controller snapshot**

Subscribe directly to the controller and set the volume series for both history and live reasons:

```ts
const unsubscribeVolume = widget.dataController?.subscribe(snapshot => {
  volumeSeries.setData(snapshot.bars.map(bar => ({
    time: bar.time,
    value: bar.volume ?? 0,
    color: bar.close >= bar.open ? 'rgba(38, 166, 154, 0.5)' : 'rgba(239, 83, 80, 0.5)',
  })));
});
```

Store the returned unsubscribe function in the host teardown path. Do not rely on `widget.on('data')`, which suppresses ordinary live updates.

- [ ] **Step 9: Run feed and browser tests**

```text
cd openalgo-charts && npm run test -- tests/feed-cache.test.ts tests/data-controller-repair.test.ts
cd ../frontend && npx playwright test e2e/feed-recovery.spec.ts --project=chromium
```

Expected: PASS with a history refresh after a forced WS gap and a repaired provisional bar.

---

### Task 7: Restore Non-NSE Selection, Workspace Safety, Screener Recovery, and Startup Bounds

**Files:**
- Modify: `openalgo-charts/src/widget/widget.ts:58-102, 127-162`
- Modify: `openalgo-charts/src/widget/topbar.ts:190-220, 285-330, 493-528`
- Modify: `openalgo-charts/tests/widget-shell.test.ts`
- Modify: `frontend/src/main.ts:1-140`
- Modify: `frontend/src/workspace.ts:180-360`
- Modify: `frontend/src/screener.ts:130-185`
- Modify: `trading/src/tradex_trading/interface/routes/chart.py:250-350` only for exchange-preserving symbol search
- Modify: `frontend/index.html`
- Create: `frontend/e2e/instrument-selection.spec.ts`
- Modify: `frontend/e2e/workspace-viewport.spec.ts`
- Modify: `frontend/e2e/screener.spec.ts`
- Create: `frontend/e2e/startup.spec.ts`

**Interfaces:**
- `WidgetOptions.exchanges?: readonly string[]` and `TopbarOptions.onExchangeChange?(exchange)` make exchange an explicit widget state transition without breaking existing topbar consumers.
- `main.ts` passes `['NSE', 'NFO', 'BSE', 'BFO', 'MCX']` and uses `widget.exchange()` for all host requests.
- `workspace.ts` exposes a visible conflict state instead of retrying a rejected revision.

- [ ] **Step 1: Add failing widget and browser assertions**

Test selecting NFO and BSE from the topbar, switching back to NSE, and verifying the history URL contains the selected exchange. Add a workspace test that holds a first save pending, changes the server revision, and asserts the UI reports a conflict rather than issuing a null-revision overwrite.

- [ ] **Step 2: Run the tests and verify failure**

```text
cd openalgo-charts && npm run test -- tests/widget-shell.test.ts
cd ../frontend && npx playwright test e2e/instrument-selection.spec.ts e2e/workspace-viewport.spec.ts e2e/startup.spec.ts --project=chromium
```

Expected: exchange selection and conflict/startup assertions fail.

- [ ] **Step 3: Add the exchange selector**

Extend the topbar with a compact select for configured exchanges. On change, call `onExchangeChange`, preserve the current symbol, call `setSymbol(symbol, exchange)`, and refresh the displayed exchange. Keep symbol search exchange-aware and never use the datalake's hardcoded NSE as the selected exchange. Update the chart symbol-search serializer to preserve the broker/exchange value when the backing search supplies it; do not invent NFO/BSE data from an NSE-only result.

- [ ] **Step 4: Fix slow workspace saves**

Do not schedule a successful save from a five-second timer that observed `series=null`. Persist only after the first non-null bar snapshot. On HTTP 409, retain the local blob, render a conflict message, and require an explicit reload/overwrite action; do not automatically resend with `revision=null`.

- [ ] **Step 5: Fix screener state transitions**

`executeScan` must set loading false on every success/empty path and clear the prior error before rendering a successful result. A zero-row scanner result must render the existing empty message, not a spinner.

- [ ] **Step 6: Bound startup work**

Wrap strategy loading and the datalake probe in a shared timeout helper with a five-second deadline. On timeout, continue with the default `NSE:RELIANCE` chart, show a non-blocking startup warning, and keep order controls disarmed until the feed is ready.

- [ ] **Step 7: Run the UI regression suites**

```text
cd openalgo-charts && npm run test -- tests/widget-shell.test.ts
cd ../frontend && npx playwright test e2e/instrument-selection.spec.ts e2e/workspace-viewport.spec.ts e2e/screener.spec.ts e2e/startup.spec.ts --project=chromium
```

Expected: PASS.

---

### Task 8: Clean the Orphaned Frontend Graph and Restore Quality Gates

**Files:**
- Audit: `frontend/src/chart-lifecycle.ts`, `frontend/src/chart-state.ts`, `frontend/src/chart-types.ts`, `frontend/src/shellbar.ts`, `frontend/src/trade-bar.ts`
- Modify or delete only after audit: the orphan files above
- Modify: `trading/tests/interface/test_ws_bars_replay.py` imports
- Modify: `frontend/package.json` only if a focused import-surface check is needed
- Create: `docs/superpowers/specs/2026-09-23-production-authentication-design.md`

**Interfaces:**
- Production entrypoint has one order-safety path, one replay path, one feed path, and one interval-mapping source.
- The follow-up authentication spec is documentation only; it must not be implemented in this plan.

- [ ] **Step 1: Run the import audit**

Run:

```text
git grep -n -E "chart-lifecycle|chart-state|chart-types|shellbar|trade-bar" -- frontend/src frontend/e2e openalgo-charts/src
```

Expected: only self-imports or references from files being deleted. Record every hit before deleting anything.

- [ ] **Step 2: Fix introduced Ruff findings**

Move `datetime`, `timedelta`, and `Decimal` helper imports to module scope in `trading/tests/interface/test_ws_bars_replay.py`. Preserve existing baseline line-length findings unless the touched block is being rewritten.

Run:

```text
.venv/bin/ruff check trading/src/tradex_trading/interface/replay_guard.py trading/src/tradex_trading/interface/routes/stream.py trading/src/tradex_trading/interface/routes/orders.py trading/src/tradex_trading/interface/fastapi_app.py trading/tests/interface/test_ws_bars_replay.py trading/tests/interface/test_replay_guard.py trading/tests/interface/test_live_bind_policy.py
```

Expected: no new errors; document any pre-existing E501 findings separately.

- [ ] **Step 3: Delete only proven orphans**

Delete the chart modules/shell files confirmed unused by the audit. Do not delete `chart-types.ts` if `feed.ts` or another production import still needs it; if retained, move its interval mapping into a focused production module and make all consumers import that module.

- [ ] **Step 4: Add the authentication follow-up document**

Document the threat model, HttpOnly session, CSRF, WebSocket upgrade authentication, revocation, and migration from query-string API keys. Explicitly mark it unimplemented and keep the loopback-only live bind gate active.

- [ ] **Step 5: Run type/build and import checks**

```text
(cd frontend && npm run typecheck && npm run build)
(cd openalgo-charts && npm run typecheck && npm run test)
git diff --check
```

Expected: no new compile/import errors and a clean diff check.

---

### Task 9: Full Verification and Release Gate

**Files:**
- Read: all files changed by Tasks 1-8
- Modify: `docs/superpowers/specs/2026-09-23-phased-app-remediation-design.md` only if verification reveals a requirement mismatch

**Interfaces:**
- Consumes every prior task's interfaces.
- Produces a verified phase gate; no new behavior is added here.

- [ ] **Step 1: Run the focused security/replay suite**

```text
.venv/bin/python -m pytest trading/tests/interface/test_replay_guard.py trading/tests/interface/test_live_bind_policy.py trading/tests/interface/test_ws_bars_replay.py trading/tests/interface/test_fastapi_app.py trading/tests/runtime/test_bar_aggregator.py -p no:cacheprovider --import-mode=importlib -c pyproject.toml
```

Expected: zero failures; expected xfails remain explicitly identified.

- [ ] **Step 2: Run the full Python suite**

```text
.venv/bin/python -m pytest domain/tests brokers/tests trading/tests tests -p no:cacheprovider --import-mode=importlib -c pyproject.toml
```

Expected: zero failures. If a baseline test fails, stop and classify it before merging further phase changes.

- [ ] **Step 3: Run chart-library verification**

```text
cd openalgo-charts
npm run lint
npm run typecheck
npm run test
npm run build
```

Expected: all commands exit successfully. Do not waive failures caused by the new lifecycle or metadata changes.

- [ ] **Step 4: Run frontend verification**

```text
cd frontend
npm run typecheck
npm run build
npm run e2e
```

Expected: TypeScript/build pass and all Playwright tests pass, including order safety, account actions, replay, feed recovery, exchange selection, workspace, screener, and startup cases.

- [ ] **Step 5: Verify the deployment boundary manually**

Start a live-mode test server with `127.0.0.1` and confirm it starts only with the required key. Attempt the same live server with `0.0.0.0` and confirm startup fails with the loopback policy message. Do not start a real broker for this check; use the existing mocked/live-session test fixture.

- [ ] **Step 6: Inspect the final diff without touching user changes**

```text
git status --short --untracked-files=all
git diff --stat
git diff --check
```

Confirm that no `frontend/dist`, test stamp, generated cache, or unrelated user file is staged or committed. No commit is created without explicit user instruction.

---

## Execution Order and Rollback Boundaries

Execute Tasks 0 through 9 in order. Tasks 1-2 are safety prerequisites; Tasks 3-4 must pass before any live-trading rehearsal; Tasks 5-6 must pass before replay/feed E2E is considered complete; Tasks 7-8 are additive reliability/cleanup work; Task 9 is the release gate.

If a task fails, stop at that task and revert only the files introduced or modified by that task using the user's approved change-management process. Never reset the original nine-file worktree.

## Follow-up Work Not Included

1. Production HttpOnly-session authentication, CSRF, WebSocket upgrade authentication, revocation, and key migration.
2. Multi-client replay ownership and scoped leases.
3. Product support beyond INTRADAY.
4. Broker-specific order capabilities that require a separate execution/API design.
