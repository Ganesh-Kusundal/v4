import 'openalgo-charts/indicators';
import { createWidget } from 'openalgo-charts/widget';
import { API_BASE, feed } from './feed';
import { mountLadder } from './ladder';
import { onOrderEntry, mountOrders } from './orders';
import { mountReplayBar } from './replay';
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

// A Tier-2 restored from saved state attaches before the first history load,
// fetches an empty window and the engine never re-attaches on data alone.
// Reapplying settings is the engine's own re-attach trigger, so every history
// load re-arms the backend indicators (fetch is skipped while points exist).
widget.on('data', () => {
  for (const inst of widget.chart.indicators()) {
    if (TIER2_IDS.has(inst.indicatorId)) {
      try { inst.setSettings({}); } catch (err) { console.warn('tier2 re-arm failed', inst.indicatorId, err); }
    }
  }
});

// M3 trade/data surface: DOM ladder, replay transport bar, order/position lines.
mountLadder(widget, feed, { symbol: widget.symbol(), exchange: widget.exchange(), interval: widget.interval() });
mountReplayBar(widget);
mountOrders(widget);

// M4: chart-state persistence (debounced save on layout/symbol/interval,
// startup restore after the widget's own localStorage pass).
mountWorkspace(widget);
