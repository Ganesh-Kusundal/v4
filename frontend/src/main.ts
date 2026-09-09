import 'openalgo-charts/indicators';
import { createWidget } from 'openalgo-charts/widget';
import type { IndicatorApi } from 'openalgo-charts';
import { API_BASE, barSocket, feed } from './feed';
import { mountLadder } from './ladder';
import { onOrderEntry, mountOrders } from './orders';
import { mountReplayBar } from './replay';
import { mountTradeBar } from './trade-bar';
import { registerTier2, TIER2_IDS } from './tier2';
import { mountWorkspace } from './workspace';

const mount = document.getElementById('app');
if (mount === null) throw new Error('frontend: no #app mount');

// Backend Tier-2 descriptors must be in the registry before the widget (and
// its indicator picker) is built.
registerTier2();

const widget = createWidget(mount, {
  feed,
  symbol: 'RELIANCE',
  exchange: 'NSE',
  interval: '1m',
  theme: 'dark',
  persist: true,
  // 500 one-minute bars only span ~8h of wall clock, which misses the last
  // session across a night or weekend with no datalake update in between.
  lookbackBars: 3000,
  // Only intervals the backend history/WS layers actually serve; anything
  // else would render a pill that 404s the feed.
  intervals: ['1m', '5m', '15m', '30m', '1h', 'D'],
  symbolSearch: async (q) =>
    (await fetch(`${API_BASE}/api/charts/symbols?q=${encodeURIComponent(q)}`)).json().then((r) => r.symbols),
  // Right-click order entry (context-menu trade rows); closure over `widget`
  // is safe: it only runs on a user click, after the assignment lands.
  onOrder: (order) => onOrderEntry(widget, order),
});

// The widget owns the price series and never creates volume; a histogram on the
// hidden overlay scale mirrors the price series' bars after every load.
const volume = widget.chart.addSeries('histogram', {
  paneIndex: 0,
  priceScaleId: '',
  priceFormat: { type: 'volume' },
});
const syncVolume = (): void => {
  volume.setData(widget.series.getData().map((b) => ({ time: b.time, value: b.volume ?? 0 })));
};
widget.on('data', () => syncVolume());

// Tier-2 backend studies learn the instrument from their own settings inputs,
// so the host keeps them pointed at the chart's current one. The check runs on
// 'data', not 'symbol': at symbol-event time the series still holds the
// previous instrument's bars, so a fetch fired then would compute the old
// symbol's values. Comparing settings instead of blindly re-applying them also
// means a plain history reload matches and re-arms nothing: one fetch per
// symbol/interval change, no refetch storm, no blank flash.
const syncTier2Instrument = (inst: IndicatorApi): void => {
  if (!TIER2_IDS.has(inst.indicatorId)) return;
  const symbol = widget.symbol();
  const exchange = widget.exchange();
  const interval = widget.interval();
  const s = inst.settings();
  const mismatch = s.symbol !== symbol || s.exchange !== exchange || s.interval !== interval;
  // A restored Tier-2 attaches before the history load, so its fetch runs on an
  // empty window and the pane stays blank; the settings already match by then,
  // so also re-arm when the study has computed no values off the loaded bars.
  // setSettings re-runs attach, whose load() refetches while points are empty.
  // Descriptors with no plots (e.g. the seasonality table) never count as
  // empty: their values() is {} on purpose and must not re-arm on every event.
  const arrays = Object.values(inst.values());
  const empty = arrays.length > 0 && !arrays.some((arr) => arr.some((v) => v !== null && Number.isFinite(v)));
  if (!mismatch && !empty) return;
  try {
    inst.setSettings({ symbol, exchange, interval });
  } catch (err) {
    console.warn('tier2 instrument sync failed', inst.indicatorId, err);
  }
};
widget.on('data', () => {
  for (const inst of widget.chart.indicators()) syncTier2Instrument(inst);
});

// An indicator added through the picker carries the descriptor defaults
// (NSE/RELIANCE and the input's interval), which can already be behind the
// chart if the user switched first. There is no indicator-added event, so
// intercept addIndicator and seed the instrument settings at construction —
// not via setSettings afterwards, which would race the attach fetch that
// started with the defaults (external.ts keeps an in-flight fetch and its
// cache key). Restore is not intercepted on purpose: it carries its own saved
// settings, and the 'data' sync above corrects a stale one.
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

// M3 trade/data surface: DOM ladder, replay transport bar, qty control,
// order/position lines.
mountLadder(widget, feed, { symbol: widget.symbol(), exchange: widget.exchange(), interval: widget.interval() });
mountReplayBar(widget);
mountTradeBar(widget);
mountOrders(widget);

// The socket gives up on its own after 10 failed reconnects; surface it.
barSocket.onGiveUp(() => widget.context.toast('Live feed lost: reconnect gave up', 'error'));

// M4: chart-state persistence (debounced save on layout/symbol/interval,
// startup restore after the widget's own localStorage pass).
mountWorkspace(widget);
