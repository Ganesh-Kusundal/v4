import 'openalgo-charts/indicators';
import { createWidget } from 'openalgo-charts/widget';
import { API_BASE, feed } from './feed';
import { mountLadder } from './ladder';
import { onOrderEntry, mountOrders } from './orders';
import { mountReplayBar } from './replay';

const mount = document.getElementById('app');
if (mount === null) throw new Error('frontend: no #app mount');

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

// M3 trade/data surface: DOM ladder, replay transport bar, order/position lines.
mountLadder(widget, feed, { symbol: widget.symbol(), exchange: widget.exchange(), interval: widget.interval() });
mountReplayBar(widget);
mountOrders(widget);
