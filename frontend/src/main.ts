import { type IndicatorApi } from 'openalgo-charts';
import './theme.css';
import 'openalgo-charts/indicators';
import { createWidget } from 'openalgo-charts/widget';
import { mountBacktestMarkers, mountBacktestRuns, mountBacktestZones } from './backtest';
import { createDock } from './dock';
import { API_BASE, barSocket, datalakeClock, feed } from './feed';
import { mountAccountPanel } from './account-panel';
import { mountLadder } from './ladder';
import { onOrderEntry, mountOrders } from './orders';
import { mountReplayBar } from './replay';
import { mountRunsBar } from './runs-bar';
import { mountShellbar, updateTelemetry, incrementTick } from './shellbar';
import { mountTradeBar } from './trade-bar';
import { mountTradesPanel } from './trades-panel';
import { loadBacktestStrategies, registerTier2, TIER2_IDS } from './tier2';
import { mountScreener } from './screener';
import { loadActiveWorkspace, mountWorkspace } from './workspace';

const mount = document.getElementById('app');
if (mount === null) throw new Error('frontend: no #app mount');

const DEFAULTS = { symbol: 'RELIANCE', exchange: 'NSE', interval: '1m' };
const INTERVALS = ['1m', '5m', '15m', '30m', '1h', 'D'];

const [strategies, boot] = await Promise.all([
  loadBacktestStrategies(),
  loadActiveWorkspace({ ...DEFAULTS, intervals: INTERVALS }),
]);

registerTier2(strategies);

// Create widget with topbar: false so our reference dual-tier shellbar drives the controls
const widget = createWidget(mount, {
  feed,
  symbol: boot.symbol,
  exchange: boot.exchange,
  interval: boot.interval,
  theme: 'dark',
  topbar: false,
  lookbackBars: 3000,
  loading: { now: datalakeClock },
  intervals: INTERVALS,
  symbolSearch: async (q) =>
    (await fetch(`${API_BASE}/api/charts/symbols?q=${encodeURIComponent(q)}`)).json().then((r) => r.symbols),
  onOrder: (order) => onOrderEntry(widget, order),
});

// Intercept addIndicator to seed new indicators with the chart's current instrument
const addIndicator = widget.chart.addIndicator.bind(widget.chart);
widget.chart.addIndicator = (indicatorId, settings = {}, options = {}) => {
  if (!TIER2_IDS.has(indicatorId)) return addIndicator(indicatorId, settings, options);
  const seeded: Record<string, unknown> = {
    symbol: widget.symbol(),
    exchange: widget.exchange(),
    interval: widget.interval(),
    ...settings,
  };
  return addIndicator(indicatorId, seeded, options);
};

// Mount reference dual-tier header at the top of #app
mountShellbar(mount, widget, {
  symbol: boot.symbol,
  exchange: boot.exchange,
  interval: boot.interval,
  intervals: INTERVALS,
  broker: 'paper',
});

if (boot.state !== null) {
  const report = widget.restoreState(boot.state);
  if (!report.applied) console.warn('workspace restore rejected:', report.reason);
}

// Histogram on the hidden overlay scale mirrors the price series' bars after every load
const volume = widget.chart.addSeries('histogram', {
  paneIndex: 0,
  priceScaleId: '',
  priceFormat: { type: 'volume' },
});
const syncVolume = (): void => {
  const bars = widget.series.getData();
  volume.setData(bars.map((b) => ({ time: b.time, value: b.volume ?? 0 })));
  if (bars.length > 0) {
    const last = bars[bars.length - 1];
    if (last && 'close' in last) {
      updateTelemetry({
        ltp: (last as { close: number }).close,
        status: `${widget.symbol()} · ${widget.interval()} · ${widget.exchange()} (${bars.length} bars)`,
      });
    }
  }
};
widget.on('data', () => {
  syncVolume();
});

// Mount the tabbed dock below the chart
const dock = createDock(widget);

// Mount trade/data surfaces
mountBacktestMarkers(widget);
mountBacktestZones(widget);
mountBacktestRuns(widget);
mountLadder(widget, feed, { symbol: widget.symbol(), exchange: widget.exchange(), interval: widget.interval() });
mountReplayBar(widget, dock);
mountTradeBar(widget, dock);
mountRunsBar(widget, dock);
mountTradesPanel(widget, dock);
mountOrders(widget);
mountAccountPanel(widget, dock);
mountScreener(widget, dock);

// Wire live stream telemetry
barSocket.onOpen(() => updateTelemetry({ wsState: 'live' }));
barSocket.onClose(() => updateTelemetry({ wsState: 'closed' }));
barSocket.on('bar', (b) => {
  incrementTick();
  const data = b as { close?: number };
  if (data?.close) updateTelemetry({ ltp: data.close });
});
barSocket.on('tick', (t) => {
  incrementTick();
  const data = t as { ltp?: number; price?: number };
  const ltp = data?.ltp ?? data?.price;
  if (ltp) updateTelemetry({ ltp });
});

barSocket.onGiveUp(() => widget.context.toast('Live feed lost: reconnect gave up', 'error'));

// Persistence
mountWorkspace(widget, boot);
