import './theme.css';
import 'openalgo-charts/indicators';
import { createWidget, type OrderRequest, type Widget } from 'openalgo-charts/widget';
import { mountBacktestMarkers, mountBacktestRuns, mountBacktestZones } from './backtest';
import { createDock } from './dock';
import { API_BASE, barSocket, datalakeClock, feed } from './feed';
import { authHeaders, getApiKey } from './apikey';
import { mountAccountPanel, type AccountPanelController } from './account-panel';
import { mountOrderControls, validateOrderIntent, type OrderIntent, type OrderSafetyState } from './order-safety';
import { mountReplayBar } from './replay';
import { mountRunsBar } from './runs-bar';
import { mountTradesPanel } from './trades-panel';
import { loadBacktestStrategies, registerTier2 } from './tier2';
import { mountScreener } from './screener';
import { loadActiveWorkspace, mountWorkspace, type BootWorkspace } from './workspace';
import { FeedController } from './feed-controller';

const mount = document.getElementById('app');
if (mount === null) throw new Error('frontend: no #app mount');

const DEFAULTS = { symbol: 'RELIANCE', exchange: 'NSE', interval: '1m' };
const INTERVALS = ['1m', '5m', '15m', '30m', '1h', 'D'] as const;
const EXCHANGES = ['NSE', 'NFO', 'BSE', 'BFO', 'MCX'] as const;
const STARTUP_TIMEOUT_MS = 5_000;

const defaultBoot = (): BootWorkspace => ({
  ...DEFAULTS,
  layoutId: `${DEFAULTS.exchange}_${DEFAULTS.symbol}_${DEFAULTS.interval}_default`,
  state: null,
  series: null,
  viewTrusted: false,
  revisions: {},
});

const startupWarnings: string[] = [];
const boundedStartup = async <T>(label: string, task: Promise<T>, fallback: T): Promise<T> => {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      task,
      new Promise<T>((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} timed out`)), STARTUP_TIMEOUT_MS);
      }),
    ]);
  } catch (error: unknown) {
    startupWarnings.push(label);
    console.warn(`startup ${label} failed`, error);
    return fallback;
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
};

const [strategies, boot] = await Promise.all([
  boundedStartup('strategy catalogue', loadBacktestStrategies(), []),
  boundedStartup('workspace', loadActiveWorkspace({ ...DEFAULTS, intervals: INTERVALS }), defaultBoot()),
]);

registerTier2(strategies.length > 0 ? strategies : undefined);

const tradingMode = getApiKey() === '' ? 'paper' : 'unknown';
let feedReady = false;
let replaying = false;
let accountPanel: AccountPanelController | null = null;
let depthUnsubscribe: (() => void) | null = null;

const orderControls = mountOrderControls(mount, {
  initialState: { mode: tradingMode, ready: false, replaying: false, armed: false, quantity: 1 },
  onStateChange: () => accountPanel?.syncControls(),
});

const setOrderStatus = (message: string, kind: 'info' | 'error' | 'success' = 'info'): void => {
  const node = document.getElementById('order-status');
  if (node !== null) {
    node.textContent = message;
    node.dataset.kind = kind;
    node.dataset.state = kind === 'error' ? 'blocked' : kind === 'success' ? 'ready' : 'info';
  }
};
const startupWarning = startupWarnings.length === 0
  ? null
  : `Startup degraded: ${startupWarnings.join(', ')} unavailable; continuing safely`;

const safetyState = (): OrderSafetyState => orderControls.state;
const setSafety = (patch: Partial<OrderSafetyState>): void => {
  const ready = !replaying && feedReady;
  orderControls.setState({
    ...patch,
    mode: replaying ? 'replay' : tradingMode,
    ready,
    replaying,
    armed: ready ? (patch.armed ?? safetyState().armed) : false,
  });
};
const canSubmit = (): boolean => {
  const state = safetyState();
  return state.armed
    && state.ready
    && !state.replaying
    && state.mode !== 'unknown'
    && state.mode !== 'replay';
};

const latestBarClose = (widget: Widget): number | null => {
  const bars = widget.series.getData();
  const last = bars[bars.length - 1];
  return last !== undefined && Number.isFinite(last.close) && last.close > 0 ? last.close : null;
};

const displayNumber = (value: number): string => String(value);

const placeOrder = (widget: Widget, order: OrderRequest): void => {
  const intent: OrderIntent = {
    side: order.side,
    type: order.type,
    price: order.price,
    triggerPrice: order.type === 'SL' ? order.price : undefined,
    paneIndex: order.paneIndex,
  };
  const state = safetyState();
  const validation = validateOrderIntent(intent, state, latestBarClose(widget));
  if (validation !== null) {
    setOrderStatus(validation, 'error');
    widget.context.toast(validation, 'error');
    return;
  }

  const quantity = state.quantity;
  const instrument = `${widget.exchange()}:${widget.symbol()}`;
  const priceText = intent.type === 'MARKET'
    ? 'MARKET'
    : `${intent.type} ${intent.type === 'SL' ? 'trigger' : 'price'} ${displayNumber(intent.triggerPrice ?? intent.price ?? 0)}`;
  const confirmation = `Confirm INTRADAY ${intent.side} ${intent.type} ${quantity} ${instrument} ${priceText}`;
  if (!window.confirm(confirmation)) {
    setOrderStatus('Order cancelled before submission');
    return;
  }

  const body: Record<string, unknown> = {
    exchange: widget.exchange(),
    symbol: widget.symbol(),
    side: order.side,
    quantity,
    order_type: order.type === 'SL' ? 'STOP' : order.type,
  };
  if (order.type === 'LIMIT') body.price = order.price;
  if (order.type === 'SL') body.trigger_price = order.price;

  void (async () => {
    const res = await fetch(`${API_BASE}/orders`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': crypto.randomUUID(),
        ...authHeaders(),
      },
      body: JSON.stringify(body),
    });
    const payload = (await res.json().catch(() => undefined)) as
      | { order_id?: string; status?: string; error?: { message?: string }; detail?: unknown }
      | undefined;
    if (res.ok) {
      orderControls.setState({ armed: false });
      setOrderStatus(`Order ${payload?.order_id ?? ''} ${payload?.status ?? 'submitted'}`.trim(), 'success');
      widget.context.toast(`Order ${payload?.order_id ?? ''} ${payload?.status ?? 'submitted'}`.trim(), 'success');
    } else {
      const detail = typeof payload?.detail === 'string'
        ? payload.detail
        : payload?.detail === undefined ? undefined : JSON.stringify(payload.detail);
      const message = `Order rejected: ${payload?.error?.message ?? detail ?? res.status}`;
      setOrderStatus(message, 'error');
      widget.context.toast(message, 'error');
    }
  })().catch((error: unknown) => {
    const message = `Order failed: ${error instanceof Error ? error.message : String(error)}`;
    setOrderStatus(message, 'error');
    widget.context.toast(message, 'error');
  });
};

let widget: Widget;
widget = createWidget(mount, {
  feed,
  symbol: boot.symbol,
  exchange: boot.exchange,
  exchanges: EXCHANGES,
  interval: boot.interval,
  theme: 'dark',
  lookbackBars: 3000,
  loading: {
    now: datalakeClock,
    timeoutMs: 10_000,
    pollIntervalMs: 30_000,
    refreshOnGap: true,
    refreshOnBarClose: { delayMs: 2_500, retries: 2, retryDelayMs: 5_000 },
  },
  intervals: INTERVALS,
  symbolSearch: async (q) => {
    const res = await fetch(
      `${API_BASE}/api/charts/symbols?source=datalake&q=${encodeURIComponent(q)}`,
    );
    if (!res.ok) return [];
    const body = (await res.json()) as { symbols?: Array<{ symbol: string; exchange: string }> };
    return Array.isArray(body.symbols) ? body.symbols : [];
  },
  onOrder: (order) => placeOrder(widget, order),
});
if (startupWarning !== null) {
  setOrderStatus(startupWarning, 'error');
  widget.context.toast(startupWarning, 'info');
}

let restoreRejected = false;
if (boot.state !== null) {
  const report = widget.restoreState(boot.state);
  if (!report.applied) {
    restoreRejected = true;
    console.warn('workspace restore rejected:', report.reason);
  }
}
const workspaceBoot = restoreRejected ? { ...boot, viewTrusted: false } : boot;

const volume = widget.chart.addSeries('histogram', {
  paneIndex: 0,
  priceScaleId: '',
  priceFormat: { type: 'volume' },
});
const syncVolume = (bars: readonly { time: number; volume?: number }[] = widget.series.getData()): void => {
  volume.setData(bars.map((bar) => ({ time: bar.time, value: bar.volume ?? 0 })));
};
widget.on('data', () => {
  syncVolume();
  if (orderControls.lastQuote === null) {
    const bars = widget.series.getData();
    const last = bars[bars.length - 1];
    if (last !== undefined) orderControls.setLastQuote(last.close);
  }
});
const dataController = widget.dataController;
const applyDataState = (snapshot: ReturnType<NonNullable<typeof dataController>['getState']>, meta?: { provisional?: boolean }): void => {
  feedReady = snapshot.status === 'ready' && snapshot.bars.length > 0 && meta?.provisional !== true;
  setSafety({});
  syncVolume(snapshot.bars);
  if (!feedReady && startupWarning !== null) setOrderStatus(startupWarning, 'error');
};
if (dataController !== null) {
  applyDataState(dataController.getState());
  dataController.subscribe(applyDataState);
}
// C: Feed-state gate — server-supervisor side delegated to FeedController (B7).
// Chart data controller (applyDataState) remains the authoritative setter for
// feedReady=true; FeedController only drives feedReady=false paths.
new FeedController(barSocket, ({ feedState, serverBlocked, message }) => {
  if (serverBlocked) feedReady = false;
  setSafety({ feedState });
  if (message !== undefined) setOrderStatus(message.text, message.kind);
});
const markFeedLoading = (message: string): void => {
  feedReady = false;
  setSafety({});
  setOrderStatus(message, 'error');
};
const bindQuote = (): void => {
  depthUnsubscribe?.();
  depthUnsubscribe = barSocket.subscribeDepth(`${widget.exchange()}:${widget.symbol()}`, (depth) => {
    const value = Number(depth.ltp);
    if (Number.isFinite(value) && value > 0) orderControls.setLastQuote(value);
  });
};
bindQuote();
widget.on('symbol', () => {
  markFeedLoading('Order entry disabled: feed loading');
  bindQuote();
});
widget.on('interval', () => {
  markFeedLoading('Order entry disabled: feed loading');
});
syncVolume();

const dock = createDock(widget);

mountBacktestMarkers(widget);
mountBacktestZones(widget);
mountBacktestRuns(widget);
mountReplayBar(widget, dock, (nextReplayState) => {
  replaying = nextReplayState;
  setSafety({});
});
mountRunsBar(widget, dock);
mountTradesPanel(widget, dock);
accountPanel = mountAccountPanel(widget, dock, {
  canSubmit,
  onError: (message) => setOrderStatus(message, 'error'),
});
mountScreener(widget, dock);
mountWorkspace(widget, workspaceBoot);

barSocket.onGiveUp(() => widget.context.toast('Live feed lost: reconnect gave up', 'error'));
