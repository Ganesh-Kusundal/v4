import 'openalgo-charts/indicators';
import { createWidget } from 'openalgo-charts/widget';
import { API_BASE, feed } from './feed';

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
