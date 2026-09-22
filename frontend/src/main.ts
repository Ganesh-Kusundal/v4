import './theme.css';
import {
  createChart,
  CandleBuilder,
  intervalToSeconds,
  darkTheme,
  registeredIndicators,
  type Bar,
  type IndicatorDescriptor,
} from 'openalgo-charts';
import 'openalgo-charts/indicators';
import {
  runTransform,
  HeikinAshiTransform,
  RenkoTransform,
  RangeBarsTransform,
  LineBreakTransform,
  PointFigureTransform,
  KagiTransform,
} from 'openalgo-charts/transform';
import { authHeaders, getApiKey, setApiKey } from './apikey';
import { API_BASE, WS_BASE, barSocket } from './feed';

interface RawBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

interface BookOrder {
  id: string;
  symbol: string;
  exchange: string;
  side: string;
  type: string;
  qty: number;
  filled_qty?: number;
  filledQty?: number;
  price: number;
  trigger_price?: number | null;
  triggerPrice?: number | null;
  status: string;
}

interface BookPosition {
  symbol: string;
  exchange: string;
  net_qty?: number;
  netQty?: number;
  avg_price?: number;
  avgPrice?: number;
  unrealized_pnl?: number;
  product?: string;
}

const el = <T extends HTMLElement = HTMLElement>(id: string): T => {
  const elem = document.getElementById(id);
  if (!elem) throw new Error(`Missing element #${id}`);
  return elem as T;
};

const fmt = (n: number | null | undefined): string =>
  n == null || isNaN(n)
    ? '-'
    : Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const esc = (s: string): string =>
  String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] || c);

const nowSec = (): number => Math.floor(Date.now() / 1000);
const round2v = (n: number): number => Math.round(n * 100) / 100;

const LOOKBACK: Record<string, number> = {
  '1m': 10 * 86400,
  '5m': 45 * 86400,
  '15m': 90 * 86400,
  '1h': 250 * 86400,
  D: 800 * 86400,
};

let chart: any = null;
let price: any = null;
let volume: any = null;
let ltpLine: any = null;
let tickN = 0;
let rawBars: RawBar[] = [];
let builder: CandleBuilder | null = null;
let liveAgg = false;
let lastLtp: number | null = null;
let ctxPrice = 0;
let lastReq: { symbol: string; exchange: string; interval: string } | null = null;
let isReplaying = false;
let replayPicking = false;
let replayPickIndex: number | null = null;
let replayShade: ReplayShade | null = null;
let replayFullBars: RawBar[] | null = null;
let replayTotalBars = 0;
let replayCurrentIndex = 0;
let replayIsPlaying = false;
let replaySpeed = 1;
const REPLAY_SPEEDS = [0.5, 1, 2, 5, 10];

let bookTimer: any = null;
const orderLines = new Map<string, { line: any; order: BookOrder; dragFrom?: number | null }>();
let posLine: any = null;
let position: { net: number; avg: number; product?: string } | null = null;

let unsubBar: (() => void) | null = null;
let unsubDepth: (() => void) | null = null;
let unsubQuote: (() => void) | null = null;
let unsubOrder: (() => void) | null = null;
let unsubFill: (() => void) | null = null;
let unsubPos: (() => void) | null = null;
let unsubOpen: (() => void) | null = null;
let unsubClose: (() => void) | null = null;

// Chart types: base types render directly; Family-B run a transform first
const boxOf = (): number => {
  const lastBar = rawBars.length > 0 ? rawBars[rawBars.length - 1] : undefined;
  const c = lastBar ? lastBar.close : 100;
  return Math.max(0.05, Math.round((c * 0.0015) / 0.05) * 0.05);
};

const CHART_TYPES: Record<string, { series: string; style?: () => any; tf?: () => any }> = {
  candlestick: { series: 'candlestick' },
  'hollow-candle': { series: 'hollow-candle' },
  bar: { series: 'bar' },
  'high-low': { series: 'high-low' },
  'volume-candle': { series: 'volume-candle' },
  line: { series: 'line' },
  'line-markers': { series: 'line-markers' },
  step: { series: 'step' },
  area: { series: 'area' },
  'hlc-area': { series: 'hlc-area' },
  baseline: {
    series: 'baseline',
    style: () => ({ baseValue: rawBars.reduce((s, b) => s + b.close, 0) / (rawBars.length || 1) }),
  },
  'heikin-ashi': { series: 'candlestick', tf: () => new HeikinAshiTransform() },
  renko: { series: 'candlestick', tf: () => new RenkoTransform({ boxSize: boxOf() }) },
  range: { series: 'candlestick', tf: () => new RangeBarsTransform({ range: boxOf() }) },
  'line-break': { series: 'candlestick', tf: () => new LineBreakTransform({ lines: 3 }) },
  'point-figure': {
    series: 'point-figure',
    style: () => ({ boxSize: boxOf() }),
    tf: () => new PointFigureTransform({ boxSize: boxOf(), reversal: 3 }),
  },
  kagi: { series: 'kagi', tf: () => new KagiTransform({ reversal: boxOf() * 2 }) },
};

const chartType = () => CHART_TYPES[el<HTMLSelectElement>('ctype').value] || CHART_TYPES.candlestick;

function bucketVolume(tbars: any[]): any[] {
  const out: any[] = [];
  let ri = 0;
  for (const tb of tbars) {
    let v = 0;
    while (ri < rawBars.length && (rawBars[ri]?.time ?? 0) <= tb.time) {
      v += rawBars[ri]?.volume || 0;
      ri++;
    }
    out.push({ time: tb.time, open: 0, high: v, low: 0, close: v });
  }
  let rest = 0;
  while (ri < rawBars.length) {
    rest += rawBars[ri]?.volume || 0;
    ri++;
  }
  if (out.length && rest) {
    const last = out[out.length - 1];
    if (last) {
      last.high += rest;
      last.close += rest;
    }
  }
  return out;
}

function setPriceData(): void {
  if (!price || !rawBars.length) return;
  const cfg = chartType() || CHART_TYPES.candlestick;
  if (cfg && cfg.tf) {
    const t = runTransform(cfg.tf(), rawBars as any);
    price.setData(t);
    volume.setData(bucketVolume(t));
  } else {
    price.setData(rawBars);
    volume.setData(
      rawBars.map((b) => ({ time: b.time, open: 0, high: b.volume || 0, low: 0, close: b.volume || 0 })),
    );
  }
}

function setLegend(sym: string, ex: string, intv: string, bar?: RawBar, ltp?: number | null): void {
  const col = bar && bar.close >= bar.open ? 'up' : 'dn';
  el('legend').innerHTML =
    `<b>${esc(sym)}</b> <span style="opacity:.5">· ${esc(intv)} · ${esc(ex)}</span>` +
    (bar
      ? ` <span class="${col}">O ${fmt(bar.open)} H ${fmt(bar.high)} L ${fmt(bar.low)} C ${fmt(bar.close)}</span>`
      : '') +
    (ltp != null ? ` <span class="ltp">LTP ${fmt(ltp)}</span>` : '');
}

function setStatus(msg: string, active = false): void {
  const s = el('status');
  s.textContent = msg;
  const dot = document.querySelector('.status-dot');
  if (dot) dot.classList.toggle('active', active);
}

function marketPrice(): number | null {
  const lastBar = rawBars.length > 0 ? rawBars[rawBars.length - 1] : undefined;
  return lastLtp != null ? lastLtp : lastBar ? lastBar.close : null;
}

function stopSideOk(side: string, type: string, px: number): boolean {
  if (type !== 'SL' && type !== 'SL-M') return true;
  const m = marketPrice();
  if (m == null) return true;
  return side === 'BUY' ? px > m : px < m;
}

function posLabel(): string {
  if (!position) return '';
  const mark = lastLtp != null ? lastLtp : position.avg;
  const pnl = (mark - position.avg) * position.net;
  return `@ ${fmt(position.avg)}  ${pnl >= 0 ? '+' : '-'}Rs ${Math.round(Math.abs(pnl)).toLocaleString('en-IN')}`;
}

function makeOrderLine(o: BookOrder): any {
  const px = o.trigger_price ?? o.triggerPrice ?? o.price;
  const side = (o.side || 'BUY').toUpperCase();
  const typ = (o.type || 'LIMIT').toUpperCase();
  return chart.addPriceLine(
    {
      price: px,
      color: side === 'BUY' ? '#26a69a' : '#ef5350',
      lineWidth: 1,
      dashed: true,
      id: `order:${o.id}`,
      cursor: 'ns-resize',
      extentFromRight: 0.3,
      closeButton: true,
      badge: `${side} ${o.qty} ${typ}`,
      qty: o.qty,
      leftLabel: typ,
    },
    0,
  );
}

function renderPosition(pos: BookPosition | null): void {
  if (posLine && chart) {
    chart.removePrimitive(posLine);
    posLine = null;
  }
  const net = pos ? Number(pos.net_qty ?? pos.netQty ?? 0) : 0;
  const avg = pos ? Number(pos.avg_price ?? pos.avgPrice ?? 0) : 0;
  position = net !== 0 ? { net, avg, product: pos?.product } : null;
  if (!position || !chart) return;
  posLine = chart.addPriceLine(
    {
      price: position.avg,
      color: position.net > 0 ? '#2e7d6b' : '#a14a52',
      lineWidth: 2,
      dashed: false,
      id: 'position',
      extentFromRight: 0.3,
      closeButton: true,
      badge: position.net > 0 ? 'LONG' : 'SHORT',
      qty: Math.abs(position.net),
      leftLabel: posLabel(),
    },
    0,
  );
}

async function pollBook(): Promise<void> {
  if (!lastReq) return;
  try {
    const res = await fetch(`${API_BASE}/api/charts/book`);
    if (!res.ok) return;
    const data = await res.json();
    const orders: BookOrder[] = data.orders || [];
    const positions: BookPosition[] = data.positions || [];

    const seen = new Set<string>();
    for (const o of orders) {
      if (o.status !== 'working' || o.symbol !== lastReq.symbol) continue;
      seen.add(o.id);
      const px = o.trigger_price ?? o.triggerPrice ?? o.price;
      const rec = orderLines.get(o.id);
      if (rec) {
        rec.order = o;
        rec.line.setPrice(px);
      } else if (chart) {
        orderLines.set(o.id, { line: makeOrderLine(o), order: o });
      }
    }
    for (const [id, rec] of orderLines) {
      if (!seen.has(id) && chart) {
        chart.removePrimitive(rec.line);
        orderLines.delete(id);
      }
    }

    const myPos = positions.find(
      (p) => p.symbol === lastReq?.symbol && Number(p.net_qty ?? p.netQty ?? 0) !== 0,
    );
    renderPosition(myPos || null);
  } catch (_) {
    // transient
  }
}

async function cancelOrder(orderId: string): Promise<void> {
  setStatus(`cancelling order ${orderId}...`);
  try {
    const res = await fetch(`${API_BASE}/orders/${encodeURIComponent(orderId)}`, {
      method: 'DELETE',
      headers: {
        'Idempotency-Key': crypto.randomUUID(),
        ...authHeaders(),
      },
    });
    if (res.ok) {
      setStatus(`cancelled order ${orderId}`);
      pollBook();
    } else {
      setStatus(`cancel error for order ${orderId}`);
    }
  } catch (e: any) {
    setStatus('cancel error: ' + (e?.message || 'failed'));
  }
}

async function exitPosition(): Promise<void> {
  if (!position || !lastReq) return;
  const arm = el<HTMLInputElement>('arm');
  if (!arm.checked) {
    setStatus('Tick "Arm trading" to exit the position');
    arm.focus();
    return;
  }
  const qty = Math.abs(position.net);
  const side = position.net > 0 ? 'SELL' : 'BUY';
  const summary = `close ${position.net > 0 ? 'LONG' : 'SHORT'} ${qty} ${lastReq.symbol} @ market`;
  if (!window.confirm(`${summary}\n\nProceed?`)) return;
  setStatus(summary + ' ...');
  try {
    const res = await fetch(`${API_BASE}/orders`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': crypto.randomUUID(),
        ...authHeaders(),
      },
      body: JSON.stringify({
        symbol: lastReq.symbol,
        exchange: lastReq.exchange,
        side,
        order_type: 'MARKET',
        quantity: qty,
        product: position.product || el<HTMLSelectElement>('product').value,
      }),
    });
    if (res.ok) {
      setStatus('position closed');
      pollBook();
    } else {
      const err = await res.json().catch(() => ({ detail: 'failed' }));
      setStatus('exit error: ' + (err.detail || 'failed'));
    }
  } catch (e: any) {
    setStatus('exit error: ' + (e?.message || 'failed'));
  }
}

async function placeFromMenu(side: string, type: string): Promise<void> {
  const arm = el<HTMLInputElement>('arm');
  if (!arm.checked) {
    setStatus('Tick "Arm trading" in the toolbar to place orders');
    arm.focus();
    return;
  }
  const sym = lastReq?.symbol || el<HTMLInputElement>('symbol').value.trim().toUpperCase();
  const ex = lastReq?.exchange || el<HTMLSelectElement>('exchange').value;
  const qty = Math.max(1, Number(el<HTMLInputElement>('qty').value) || 1);
  const product = el<HTMLSelectElement>('product').value;
  const priceVal = type === 'MARKET' ? 0 : round2v(ctxPrice);

  if (!stopSideOk(side, type, priceVal)) {
    const m = marketPrice();
    setStatus(`${side} Stop must be ${side === 'BUY' ? 'ABOVE' : 'BELOW'} the price (LTP ${fmt(m)})`);
    return;
  }
  const apiType = type === 'SL' ? 'STOP' : type === 'SL-M' ? 'STOP_LIMIT' : type;
  const summary = `${side} ${type} ${qty} ${sym}` + (type === 'MARKET' ? '' : ` @ ${fmt(priceVal)}`) + ` (${product})`;
  setStatus('placing: ' + summary + ' ...');
  try {
    const orderPayload: Record<string, unknown> = {
      symbol: sym,
      exchange: ex,
      side: side.toUpperCase(),
      order_type: apiType,
      quantity: qty,
    };
    if (apiType === 'LIMIT' || apiType === 'STOP_LIMIT') {
      orderPayload.price = priceVal;
    }
    if (apiType === 'STOP' || apiType === 'STOP_LIMIT') {
      orderPayload.trigger_price = priceVal;
    }

    const res = await fetch(`${API_BASE}/orders`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': crypto.randomUUID(),
        ...authHeaders(),
      },
      body: JSON.stringify(orderPayload),
    });
    const j = await res.json().catch(() => ({}));
    if (res.ok) {
      const orderId = j.order_id || j.id || (typeof j === 'string' ? j : 'ok');
      setStatus(`placed ${summary} (id ${orderId})`, true);
      pollBook();
    } else {
      setStatus('order error: ' + (j.detail || j.message || 'failed'));
    }
  } catch (e: any) {
    setStatus('order error: ' + (e?.message || 'failed'));
  }
}

function clearTrading(): void {
  if (bookTimer) {
    clearInterval(bookTimer);
    bookTimer = null;
  }
  for (const [, rec] of orderLines) {
    if (chart) chart.removePrimitive(rec.line);
  }
  orderLines.clear();
  if (posLine && chart) {
    chart.removePrimitive(posLine);
    posLine = null;
  }
  position = null;
}

function wireChart(): void {
  chart.subscribeDrag(
    (id: string, p: number) => {
      if (!id.startsWith('order:') || id.endsWith('::close')) return;
      const rec = orderLines.get(id.slice(6));
      if (!rec) return;
      if (rec.dragFrom == null) {
        rec.dragFrom = rec.line.price;
        rec.line.setDragGhost(rec.dragFrom);
      }
      rec.line.setPrice(round2v(p));
    },
    async (id: string, p: number) => {
      if (!id.startsWith('order:') || id.endsWith('::close')) return;
      const oid = id.slice(6);
      const rec = orderLines.get(oid);
      if (!rec) return;
      rec.line.setDragGhost(null);
      rec.dragFrom = null;
      const px = round2v(p);
      setStatus(`modifying ${rec.order.side} ${rec.order.type} to ${fmt(px)} ...`);
      try {
        const res = await fetch(`${API_BASE}/orders/${encodeURIComponent(oid)}`, {
          method: 'PUT',
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': crypto.randomUUID(),
            ...authHeaders(),
          },
          body: JSON.stringify({ price: px }),
        });
        if (res.ok) {
          setStatus(`modified ${rec.order.side} ${rec.order.type} to ${fmt(px)}`);
          pollBook();
        } else {
          const err = await res.json().catch(() => ({ detail: 'modify failed' }));
          setStatus(`modify error: ${err.detail || 'failed'}`);
          pollBook();
        }
      } catch (e: any) {
        setStatus('modify error: ' + (e?.message || 'failed'));
        pollBook();
      }
    },
  );

  chart.subscribeClick((id: string) => {
    if (id === 'position::close') {
      exitPosition();
      return;
    }
    if (id.startsWith('order:') && id.endsWith('::close')) {
      cancelOrder(id.slice(6, -7));
    }
  });

  chart.subscribeCrosshairMove((e: any) => {
    if (replayPicking) {
      movePick(e.index);
    }
  });
}

function buildChart(): void {
  const savedIndicators: { indicatorId: string; settings: any }[] = [];
  if (chart) {
    try {
      for (const i of chart.indicators()) {
        savedIndicators.push({ indicatorId: i.indicatorId, settings: i.settings?.() || {} });
      }
    } catch (_) {}
    chart.destroy();
  }
  el('chart').innerHTML = '';
  chart = createChart(el('chart'), { theme: darkTheme, priceAxisWidth: 72 });
  const cfg = chartType() ?? { series: 'candlestick' };
  const seriesStyle = cfg.style ? cfg.style() : {};
  price = chart.addSeries(cfg.series, { style: seriesStyle });
  volume = chart.addSeries('histogram', { paneIndex: 1, style: { color: '#33415e' } });
  setPriceData();

  if (chart.timeScale && chart.timeScale.barSpacing > 6) {
    chart.timeScale.setBarSpacing(6);
  }

  // Re-attach active indicators
  for (const item of savedIndicators) {
    try {
      chart.addIndicator(item.indicatorId, item.settings);
    } catch (_) {}
  }
  renderActiveChips();

  // Re-attach replay shade if picking
  if (replayPicking && replayPickIndex !== null) {
    replayShade = new ReplayShade({ index: replayPickIndex });
    chart.addPrimitive(replayShade, 0);
  } else {
    replayShade = null;
  }

  const lastBar = rawBars.length > 0 ? rawBars[rawBars.length - 1] : undefined;
  const lp = lastLtp != null ? lastLtp : lastBar ? lastBar.close : null;
  ltpLine = lp != null ? chart.addPriceLine({ price: lp, color: '#e0b020', lineWidth: 1, dashed: true, id: 'ltp' }, 0) : null;
  wireChart();
  orderLines.clear();
  posLine = null;
  pollBook();
}

function disconnect(): void {
  if (unsubBar) {
    unsubBar();
    unsubBar = null;
  }
  if (unsubDepth) {
    unsubDepth();
    unsubDepth = null;
  }
  if (unsubQuote) {
    unsubQuote();
    unsubQuote = null;
  }
  if (unsubOrder) {
    unsubOrder();
    unsubOrder = null;
  }
  if (unsubFill) {
    unsubFill();
    unsubFill = null;
  }
  if (unsubPos) {
    unsubPos();
    unsubPos = null;
  }
  if (unsubOpen) {
    unsubOpen();
    unsubOpen = null;
  }
  if (unsubClose) {
    unsubClose();
    unsubClose = null;
  }
  clearTrading();
  el('wsstate').textContent = 'WS idle';
}

async function connect(): Promise<void> {
  disconnect();

  const apiKeyInput = document.getElementById('apikey') as HTMLInputElement | null;
  const userKey = apiKeyInput ? apiKeyInput.value.trim() : '';
  if (userKey) {
    setApiKey(userKey);
  }

  const symbol = el<HTMLInputElement>('symbol').value.trim().toUpperCase();
  const exchange = el<HTMLSelectElement>('exchange').value;
  const interval = el<HTMLSelectElement>('interval').value;
  const instrument = `${exchange}:${symbol}`;
  lastReq = { symbol, exchange, interval };
  lastLtp = null;
  rawBars = [];

  setStatus(`connecting to TradeX backend (${symbol} ${interval})...`);

  // History load via TradeX datalake
  try {
    const to = nowSec();
    const url = `${API_BASE}/api/charts/history/${encodeURIComponent(instrument)}?interval=${encodeURIComponent(
      interval,
    )}&to=${to}`;

    const res = await fetch(url, { headers: { ...authHeaders() } });
    if (!res.ok) {
      setStatus(`history error: HTTP ${res.status}`);
      return;
    }
    const data = await res.json();
    rawBars = (data.bars || []).map((b: any) => ({
      time: b.time,
      open: Number(b.open),
      high: Number(b.high),
      low: Number(b.low),
      close: Number(b.close),
      volume: Number(b.volume || 0),
    }));

    if (!rawBars.length) {
      setStatus(`no history for ${symbol} ${exchange} ${interval}`);
      return;
    }

    buildChart();
    const lastBar = rawBars[rawBars.length - 1];
    if (lastBar) {
      lastLtp = lastBar.close;
      el('ltp').textContent = `LTP ${fmt(lastLtp)}`;
      setLegend(symbol, exchange, interval, lastBar, lastBar.close);
    }
    setStatus(`${symbol} · ${rawBars.length} bars loaded · connecting live feed...`, true);

    // Setup live websocket stream
    liveAgg = /^\d+(m|h)$/i.test(interval);
    builder = new CandleBuilder({ intervalSec: intervalToSeconds(interval), volumeMode: 'ltq-sum' });
    if (liveAgg && rawBars.length > 0 && rawBars[rawBars.length - 1]) {
      builder.seed(rawBars[rawBars.length - 1] as Bar);
    }
    tickN = 0;

    const onTickUpdate = (ltp: number) => {
      if (ltp == null || isNaN(ltp) || ltp <= 0) return;
      // Sanity check: prevent out-of-range ticks (e.g. 100 on a 1250 stock) from blowing up the price scale
      const lastBar = rawBars.length > 0 ? rawBars[rawBars.length - 1] : undefined;
      if (lastBar && lastBar.close > 0) {
        const ratio = ltp / lastBar.close;
        if (ratio < 0.4 || ratio > 2.5) {
          console.warn(`Ignoring out-of-range tick ${ltp} (last close ${lastBar.close})`);
          return;
        }
      }
      tickN += 1;
      el('wsstate').textContent = 'WS live';
      el('ticks').textContent = `${tickN} ticks`;
      lastLtp = ltp;
      el('ltp').textContent = `LTP ${fmt(ltp)}`;
      if (ltpLine) ltpLine.setPrice(ltp);
      if (position && posLine) posLine.setLeftLabel(posLabel());

      if (liveAgg && builder) {
        const u = builder.onTick({ time: nowSec(), price: ltp });
        if (u && u.bar) {
          const curBar = u.bar as RawBar;
          const i = rawBars.findIndex((b) => b.time === curBar.time);
          if (i >= 0) rawBars[i] = curBar;
          else rawBars.push(curBar);
          setPriceData();
          const curLast = rawBars[rawBars.length - 1];
          if (curLast && lastReq) {
            setLegend(lastReq.symbol, lastReq.exchange, lastReq.interval, curLast, ltp);
          }
        }
      } else {
        const curLast = rawBars[rawBars.length - 1];
        if (curLast && lastReq) {
          setLegend(lastReq.symbol, lastReq.exchange, lastReq.interval, curLast, ltp);
        }
      }
    };

    const onBarUpdate = (b: Bar) => {
      if (!b || b.close == null || isNaN(b.close) || b.close <= 0) return;
      if (!isReplaying) {
        const lastBar = rawBars.length > 0 ? rawBars[rawBars.length - 1] : undefined;
        if (lastBar && lastBar.close > 0) {
          const ratio = b.close / lastBar.close;
          if (ratio < 0.4 || ratio > 2.5) {
            console.warn(`Ignoring out-of-range bar ${b.close} (last close ${lastBar.close})`);
            return;
          }
        }
      }
      tickN += 1;
      el('wsstate').textContent = isReplaying ? 'Replay live' : 'WS live';
      el('ticks').textContent = `${tickN} ticks`;
      lastLtp = b.close;
      el('ltp').textContent = `LTP ${fmt(b.close)}`;
      if (ltpLine) ltpLine.setPrice(b.close);
      if (position && posLine) posLine.setLeftLabel(posLabel());

      if (rawBars.length === 0) {
        rawBars.push(b as RawBar);
        setPriceData();
      } else {
        const last = rawBars[rawBars.length - 1];
        if (last && b.time === last.time) {
          // Intra-bar forming update: stretch high/low and update close & volume
          rawBars[rawBars.length - 1] = {
            time: last.time,
            open: b.open != null ? b.open : last.open,
            high: Math.max(last.high, b.high != null ? b.high : b.close),
            low: Math.min(last.low, b.low != null ? b.low : b.close),
            close: b.close,
            volume: b.volume != null ? b.volume : last.volume,
          };
          setPriceData();
          if (isReplaying) {
            syncReplayBarUI(rawBars.length - 1, replayTotalBars, rawBars[rawBars.length - 1] ?? null, replayIsPlaying, replaySpeed);
          }
        } else if (last && b.time > last.time) {
          // New candle started
          rawBars.push(b as RawBar);
          setPriceData();
          if (isReplaying) {
            replayCurrentIndex = rawBars.length - 1;
            syncReplayBarUI(replayCurrentIndex, replayTotalBars, rawBars[replayCurrentIndex] ?? null, replayIsPlaying, replaySpeed);
          }
        } else if (isReplaying) {
          const idx = rawBars.findIndex((r) => r.time === b.time);
          const target = idx >= 0 ? rawBars[idx] : undefined;
          if (target) {
            rawBars[idx] = {
              time: b.time,
              open: b.open != null ? b.open : target.open,
              high: Math.max(target.high, b.high != null ? b.high : b.close),
              low: Math.min(target.low, b.low != null ? b.low : b.close),
              close: b.close,
              volume: b.volume != null ? b.volume : target.volume,
            };
          } else {
            rawBars.push(b as RawBar);
            rawBars.sort((x, y) => x.time - y.time);
          }
          setPriceData();
        }
      }
      const curLast = rawBars[rawBars.length - 1];
      if (curLast && lastReq) {
        setLegend(lastReq.symbol, lastReq.exchange, lastReq.interval, curLast, lastLtp);
      }
    };

    unsubOpen = barSocket.onOpen(() => {
      el('wsstate').textContent = 'WS live';
      barSocket.send({ type: 'subscribe_orders' });
    });
    unsubClose = barSocket.onClose(() => {
      el('wsstate').textContent = 'WS closed';
    });

    // Real-time TradeX depth subscription (handles depth and ltp snapshots & ticks)
    unsubDepth = barSocket.subscribeDepth(instrument, (d: any) => {
      if (d && d.ltp != null) {
        onTickUpdate(Number(d.ltp));
      }
    });

    // Real-time TradeX bar stream subscription
    unsubBar = barSocket.subscribe(instrument, interval, onBarUpdate);

    // Real-time TradeX quote stream subscription
    unsubQuote = barSocket.on('quote', (q: any) => {
      if (q && q.instrument === instrument && q.ltp != null) {
        onTickUpdate(Number(q.ltp));
      }
    });

    // Real-time order/position update subscriptions from TradeX backend
    unsubOrder = barSocket.on('order', () => pollBook());
    unsubFill = barSocket.on('fill', () => pollBook());
    unsubPos = barSocket.on('position', () => pollBook());

    bookTimer = setInterval(pollBook, 4000);
    pollBook();
    setStatus(`${symbol} · TradeX connected`, true);
  } catch (e: any) {
    setStatus('connect error: ' + (e?.message || 'failed'));
  }
}

// Event Listeners
el('connect').addEventListener('click', connect);
el('disconnect').addEventListener('click', () => {
  disconnect();
  setStatus('disconnected');
});
el('fit').addEventListener('click', () => {
  if (chart) chart.resetScale();
});

// Right click context menu
const ctxMenu = el('ctxmenu');
el('chart').addEventListener('contextmenu', (e: MouseEvent) => {
  if (!chart) return;
  e.preventDefault();
  const rect = el('chart').getBoundingClientRect();
  const p = chart.coordinateToPrice(e.clientY - rect.top, 0);
  if (p == null) return;
  ctxPrice = round2v(p);
  const m = marketPrice();

  ctxMenu.querySelectorAll('button[data-type]').forEach((b) => {
    const side = b.getAttribute('data-side') || 'BUY';
    const v = side === 'BUY' ? 'Buy' : 'Sell';
    const t = b.getAttribute('data-type');
    const label =
      t === 'MARKET' ? `${v} Market` : t === 'LIMIT' ? `${v} Limit @ ${fmt(ctxPrice)}` : `${v} Stop (SL) @ ${fmt(ctxPrice)}`;
    const sp = b.querySelector('span');
    if (sp) sp.textContent = label;
    else b.textContent = label;
    let ok = true;
    if (m != null) {
      if (t === 'SL') ok = side === 'BUY' ? ctxPrice > m : ctxPrice < m;
      else if (t === 'LIMIT') ok = side === 'BUY' ? ctxPrice < m : ctxPrice > m;
    }
    (b as HTMLButtonElement).disabled = !ok;
  });

  ctxMenu.style.left = `${Math.min(e.clientX, window.innerWidth - 230)}px`;
  ctxMenu.style.top = `${Math.min(e.clientY, window.innerHeight - 300)}px`;
  ctxMenu.hidden = false;
});

window.addEventListener('click', () => {
  ctxMenu.hidden = true;
});

ctxMenu.addEventListener('click', (e: MouseEvent) => {
  const b = (e.target as HTMLElement).closest('button');
  if (!b) return;
  e.stopPropagation();
  ctxMenu.hidden = true;
  placeFromMenu(b.getAttribute('data-side') || 'BUY', b.getAttribute('data-type') || 'MARKET');
});

// ── Symbol Autocomplete / Datalake Type-ahead ────────────────
const symInput = el<HTMLInputElement>('symbol');
const symDropdown = el('sym-dropdown');
let activeSymIndex = -1;
let datalakeSymbols: { symbol: string; exchange: string }[] = [];
let searchTimer: any = null;

async function fetchDatalakeSymbols(q = ''): Promise<void> {
  try {
    const res = await fetch(`${API_BASE}/api/charts/symbols?source=datalake&q=${encodeURIComponent(q.trim())}`);
    if (!res.ok) return;
    const data = await res.json();
    datalakeSymbols = data.symbols || [];
    renderSymDropdown();
  } catch (_) {
    // transient
  }
}

function renderSymDropdown(): void {
  activeSymIndex = -1;
  if (!datalakeSymbols.length) {
    symDropdown.innerHTML = `<div class="sym-empty">No datalake symbols found</div>`;
    symDropdown.hidden = false;
    return;
  }
  symDropdown.innerHTML = datalakeSymbols
    .slice(0, 100)
    .map(
      (s, i) =>
        `<div class="sym-item" data-index="${i}" data-sym="${esc(s.symbol)}" data-ex="${esc(s.exchange)}">
          <b>${esc(s.symbol)}</b>
          <span class="sym-meta">
            <span class="sym-tag">${esc(s.exchange)}</span>
            <span class="sym-tag" style="color:var(--acc);border-color:#1a423a">datalake</span>
          </span>
        </div>`,
    )
    .join('');
  symDropdown.hidden = false;

  symDropdown.querySelectorAll('.sym-item').forEach((item) => {
    item.addEventListener('mousedown', (e) => {
      e.preventDefault(); // prevent blur before click
      const sym = item.getAttribute('data-sym');
      const ex = item.getAttribute('data-ex');
      if (sym) {
        selectSymbol(sym, ex || 'NSE');
      }
    });
  });
}

function selectSymbol(sym: string, ex: string): void {
  symInput.value = sym;
  const exSelect = el<HTMLSelectElement>('exchange');
  if (exSelect && ex) {
    const opt = Array.from(exSelect.options).find((o) => o.value === ex);
    if (opt) exSelect.value = ex;
  }
  symDropdown.hidden = true;
  connect();
}

function highlightSymItem(index: number): void {
  const items = symDropdown.querySelectorAll('.sym-item');
  items.forEach((it, i) => {
    it.classList.toggle('active', i === index);
  });
  if (index >= 0 && items[index]) {
    (items[index] as HTMLElement).scrollIntoView({ block: 'nearest' });
  }
}

symInput.addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    fetchDatalakeSymbols(symInput.value);
  }, 120);
});

symInput.addEventListener('focus', () => {
  fetchDatalakeSymbols(symInput.value);
});

symInput.addEventListener('keydown', (e: KeyboardEvent) => {
  if (symDropdown.hidden) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      fetchDatalakeSymbols(symInput.value);
    } else if (e.key === 'Enter') {
      connect();
    }
    return;
  }

  const items = symDropdown.querySelectorAll('.sym-item');
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (!items.length) return;
    activeSymIndex = (activeSymIndex + 1) % items.length;
    highlightSymItem(activeSymIndex);
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (!items.length) return;
    activeSymIndex = (activeSymIndex - 1 + items.length) % items.length;
    highlightSymItem(activeSymIndex);
  } else if (e.key === 'Enter') {
    e.preventDefault();
    if (activeSymIndex >= 0) {
      const activeItem = items[activeSymIndex];
      if (activeItem) {
        const sym = activeItem.getAttribute('data-sym');
        const ex = activeItem.getAttribute('data-ex');
        if (sym) {
          selectSymbol(sym, ex || 'NSE');
          return;
        }
      }
    }
    symDropdown.hidden = true;
    connect();
  } else if (e.key === 'Escape') {
    symDropdown.hidden = true;
  }
});

document.addEventListener('click', (e) => {
  if (!symInput.contains(e.target as Node) && !symDropdown.contains(e.target as Node)) {
    symDropdown.hidden = true;
  }
});

// ── Indicators & Studies Modal ──────────────────────────────
const indBtn = el('ind-btn');
const indBackdrop = el('ind-backdrop');
const indClose = el('ind-close');
const indSearch = el<HTMLInputElement>('ind-search');
const indTabs = el('ind-tabs');
const indList = el('ind-list');
const indCount = document.getElementById('ind-count');
let currentCategory = 'all';

function renderActiveChips(): void {
  const bar = document.getElementById('ind-active-bar');
  const chips = document.getElementById('ind-active-chips');
  if (!bar || !chips) return;
  if (!chart) {
    bar.hidden = true;
    return;
  }
  const current = chart.indicators();
  if (!current.length) {
    bar.hidden = true;
    chips.innerHTML = '';
    return;
  }
  bar.hidden = false;
  chips.innerHTML = current
    .map(
      (inst: any) =>
        `<span class="ind-active-chip">${esc(inst.name)} <button class="remove-btn" data-id="${esc(
          inst.id,
        )}" title="Remove ${esc(inst.name)}">✕</button></span>`,
    )
    .join('');
  chips.querySelectorAll('.remove-btn').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const id = (btn as HTMLElement).getAttribute('data-id');
      if (id && chart) {
        chart.removeIndicator(id);
        renderActiveChips();
      }
    });
  });
}

function openIndicatorDialog(): void {
  indBackdrop.hidden = false;
  indSearch.value = '';
  renderActiveChips();
  renderIndicatorCatalog();
  setTimeout(() => indSearch.focus(), 50);
}

function closeIndicatorDialog(): void {
  indBackdrop.hidden = true;
}

function renderIndicatorCatalog(): void {
  const all = registeredIndicators();
  if (indCount) indCount.textContent = `${all.length}`;
  const needle = indSearch.value.trim().toLowerCase();

  const filtered = all.filter((d) => {
    if (currentCategory !== 'all') {
      const cat = (d.category || 'General').toLowerCase();
      if (cat !== currentCategory.toLowerCase()) return false;
    }
    if (!needle) return true;
    return (
      d.name.toLowerCase().includes(needle) ||
      d.id.toLowerCase().includes(needle) ||
      (d.category || '').toLowerCase().includes(needle)
    );
  });

  if (!filtered.length) {
    indList.innerHTML = `<div class="sym-empty" style="padding:32px">No indicators matching "${esc(needle)}"</div>`;
    return;
  }

  indList.innerHTML = filtered
    .map(
      (d) =>
        `<div class="ind-row" data-id="${esc(d.id)}">
          <div class="ind-info">
            <span class="ind-name">${esc(d.name)}</span>
            <div class="ind-meta">
              <span class="ind-cat">${esc(d.category || 'General')}</span>
              <span class="ind-placement">${d.placement === 'onchart' ? 'Overlay' : 'Sub-Pane'}</span>
            </div>
          </div>
          <button class="ind-add-btn">+ Add</button>
        </div>`,
    )
    .join('');

  indList.querySelectorAll('.ind-row').forEach((row) => {
    row.addEventListener('click', () => {
      const id = row.getAttribute('data-id');
      if (!id || !chart) return;
      try {
        chart.addIndicator(id);
        renderActiveChips();
        setStatus(`Added ${id}`, true);
      } catch (err: any) {
        setStatus(`Indicator error: ${err?.message || 'failed'}`);
      }
    });
  });
}

indBtn.addEventListener('click', openIndicatorDialog);
indClose.addEventListener('click', closeIndicatorDialog);
indBackdrop.addEventListener('click', (e) => {
  if (e.target === indBackdrop) closeIndicatorDialog();
});

indSearch.addEventListener('input', () => {
  renderIndicatorCatalog();
});

indTabs.querySelectorAll('.modal-tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    indTabs.querySelectorAll('.modal-tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    currentCategory = tab.getAttribute('data-cat') || 'all';
    renderIndicatorCatalog();
  });
});

window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !indBackdrop.hidden) {
    closeIndicatorDialog();
  }
});

// ── Chart Snapshot PNG ──────────────────────────────────────
const snapBtn = el('snap-btn');
snapBtn.addEventListener('click', () => {
  if (!chart) return;
  try {
    const canvas = chart.takeScreenshot();
    const sym = lastReq?.symbol || 'chart';
    const intv = lastReq?.interval || '';
    const filename = `tradex_${sym}_${intv}_${Date.now()}.png`;
    const a = document.createElement('a');
    a.download = filename;
    a.href = canvas.toDataURL('image/png');
    a.click();
    setStatus(`Saved snapshot ${filename}`, true);
  } catch (err: any) {
    setStatus(`Snapshot error: ${err?.message || 'failed'}`);
  }
});

// ── Toolbar Select Changes ──────────────────────────────────
el('interval').addEventListener('change', () => {
  connect();
});

el('exchange').addEventListener('change', () => {
  connect();
});

el('ctype').addEventListener('change', () => {
  if (!rawBars.length) return;
  buildChart();
  const lb = rawBars[rawBars.length - 1];
  if (lastReq && lb) {
    setLegend(lastReq.symbol, lastReq.exchange, lastReq.interval, lb, lastLtp != null ? lastLtp : lb.close);
  }
});

// Seed API key if present in meta tag
const seedKey = getApiKey();
if (seedKey) {
  const keyInput = document.getElementById('apikey') as HTMLInputElement | null;
  if (keyInput) keyInput.value = seedKey;
}

// Set initial mode
const modeEl = el('mode');
if (modeEl) {
  modeEl.textContent = 'ANALYZE (sandbox)';
  modeEl.className = 'chip analyze';
}

// ── Market Replay (openalgo-charts reference implementation) ──────────────────
interface ReplayShadeOptions {
  index: number | null;
  color?: string;
  lineColor?: string;
  lineWidth?: number;
  lineVisible?: boolean;
}

class ReplayShade {
  private _opts: Required<Omit<ReplayShadeOptions, 'index'>> & { index: number | null };
  private _host?: any;

  constructor(opts: ReplayShadeOptions) {
    this._opts = {
      index: opts.index,
      color: opts.color ?? 'rgba(12, 14, 20, 0.62)',
      lineColor: opts.lineColor ?? '#3b82f6',
      lineWidth: opts.lineWidth ?? 1,
      lineVisible: opts.lineVisible ?? true,
    };
  }

  zOrder() { return 'top' as const; }
  autoscaleInfo() { return null; }
  hitTest() { return null; }
  attached(host: any) { this._host = host; }
  detached() { this._host = undefined; }

  setOptions(patch: Partial<ReplayShadeOptions>): void {
    this._opts = { ...this._opts, ...patch };
    this._host?.requestUpdate?.();
  }

  draw(ctx: CanvasRenderingContext2D, rc: any): void {
    const o = this._opts;
    if (o.index === null) return;
    const dpr = rc.dpr || 1;
    const w = rc.plotWidth * dpr;
    const h = rc.plotHeight * dpr;
    if (w <= 0 || h <= 0) return;

    const barSpacing = rc.timeScale?.barSpacing ?? 6;
    const half = (barSpacing * dpr) / 2;
    const barX = rc.timeScale?.indexToX ? rc.timeScale.indexToX(o.index) : null;
    if (barX == null || isNaN(barX)) return;
    const x = Math.round(barX * dpr + half);
    if (x >= w) return;

    const from = Math.max(0, x);
    ctx.save();
    ctx.fillStyle = o.color;
    ctx.fillRect(from, 0, w - from, h);
    if (o.lineVisible && x >= 0) {
      ctx.strokeStyle = o.lineColor;
      ctx.lineWidth = Math.max(1, Math.round(o.lineWidth * dpr));
      ctx.beginPath();
      ctx.moveTo(x + 0.5, 0);
      ctx.lineTo(x + 0.5, h);
      ctx.stroke();
    }
    ctx.restore();
  }
}

function formatBarTime(ts: number): string {
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const replayBtn = el('replay-btn');
const replayBar = el('replaybar');
const replayPickPrompt = el('replaypick');
const replayLeaveModal = el('replayleave');
const rpExit = el('rp-exit');
const rpBack = el('rp-back');
const rpPlay = el('rp-play');
const rpFwd = el('rp-fwd');
const rpScrub = el<HTMLInputElement>('rp-scrub');
const rpSpeed = el('rp-speed');
const rpCount = el('rp-count');
const rpClock = el('rp-clock');
const rpPickCancel = el('rp-pick-cancel');
const rpPicked = el('rp-picked');
const rpLeaveStay = el('rp-leave-stay');
const rpLeaveGo = el('rp-leave-go');

function enterReplay(): void {
  if (isReplaying || replayPicking || !chart) return;
  if (rawBars.length < 2) {
    setStatus('Replay needs bars (load chart first)');
    return;
  }
  replayPicking = true;
  replayBtn.classList.add('active');
  el('chart').classList.add('is-picking');

  let from = 0;
  try {
    const range = chart.timeScale?.getVisibleLogicalRange?.();
    if (range) from = Math.round(range.from);
  } catch (_) {
    from = 0;
  }
  const floor = Math.min(20, rawBars.length - 1);
  replayPickIndex = Math.max(floor, Math.min(rawBars.length - 1, from));

  if (!replayShade) {
    replayShade = new ReplayShade({ index: replayPickIndex });
    chart.addPrimitive(replayShade, 0);
  } else {
    replayShade.setOptions({ index: replayPickIndex });
  }

  replayPickPrompt.hidden = false;
  syncPickHint();
  setStatus('Replay: click a bar to start from (Esc to cancel)');
}

function cancelPick(): void {
  if (!replayPicking) return;
  replayPicking = false;
  replayPickIndex = null;
  if (replayShade) {
    replayShade.setOptions({ index: null });
  }
  el('chart').classList.remove('is-picking');
  replayPickPrompt.hidden = true;
  replayBtn.classList.remove('active');
  setStatus('Replay cancelled');
}

function movePick(index: number | null | undefined): void {
  if (!replayPicking || index === null || index === undefined || !chart) return;
  const total = rawBars.length;
  if (total === 0) return;
  const clamped = Math.max(0, Math.min(total - 1, Math.round(index)));
  if (clamped === replayPickIndex) return;
  replayPickIndex = clamped;
  if (replayShade) {
    replayShade.setOptions({ index: clamped });
  }
  syncPickHint();
}

function syncPickHint(): void {
  const b = rawBars[replayPickIndex ?? 0];
  if (rpPicked) {
    rpPicked.textContent = b ? formatBarTime(b.time) : '';
  }
}

function startReplayAt(index: number): void {
  if (!chart || isReplaying) return;
  if (rawBars.length < 2) return;

  replayPicking = false;
  el('chart').classList.remove('is-picking');
  replayPickPrompt.hidden = true;
  if (replayShade) {
    replayShade.setOptions({ index: null });
  }

  if (!replayFullBars || replayFullBars.length === 0) {
    replayFullBars = [...rawBars];
  }

  replayCurrentIndex = index;
  replayTotalBars = replayFullBars.length;
  const chosenBar = replayFullBars[index] ?? null;
  const firstBar = rawBars[0];
  if (!chosenBar || !firstBar) return;
  const startTime = chosenBar.time;

  // Truncate chart bars to the selected start bar
  rawBars = replayFullBars.slice(0, index + 1);
  setPriceData();
  chart.fitContent();

  // Show replay transport bar
  isReplaying = true;
  replayIsPlaying = true;
  replayBar.hidden = false;
  replayBtn.classList.add('active');

  syncReplayBarUI(index, replayTotalBars, chosenBar, true, replaySpeed);

  const symbol = el<HTMLInputElement>('symbol').value.trim().toUpperCase();
  const exchange = el<HTMLSelectElement>('exchange').value;
  const interval = el<HTMLSelectElement>('interval').value;
  const instrument = `${exchange}:${symbol}`;

  setStatus(`Replaying ${instrument} from ${formatBarTime(startTime)} (${replaySpeed}x)...`, true);

  barSocket.send({
    type: 'replay_start',
    instrument,
    interval: interval === 'D' ? '1d' : interval,
    speed: replaySpeed,
    ticks_per_bar: 10,
    start_time: startTime,
  });
}

function syncReplayBarUI(index: number, total: number, bar: RawBar | null, playing: boolean, speed: number): void {
  if (rpScrub) {
    rpScrub.max = String(Math.max(0, total - 1));
    if (Number(rpScrub.value) !== index) rpScrub.value = String(index);
  }
  if (rpCount) rpCount.textContent = `${index + 1} / ${total}`;
  if (rpClock && bar) rpClock.textContent = formatBarTime(bar.time);
  if (rpPlay) {
    rpPlay.innerHTML = playing ? '⏸' : '▶';
    rpPlay.title = playing ? 'Pause' : 'Play';
    rpPlay.classList.toggle('is-on', playing);
  }
  if (rpSpeed) rpSpeed.textContent = `${speed}x`;
}

function askExitReplay(): void {
  if (replayPicking) {
    cancelPick();
    return;
  }
  if (!isReplaying) return;
  replayLeaveModal.hidden = false;
}

function exitReplay(): void {
  cancelPick();
  replayLeaveModal.hidden = true;
  if (isReplaying) {
    barSocket.send({ type: 'replay_stop' });
  }
  isReplaying = false;
  replayIsPlaying = false;
  replayBar.hidden = true;
  replayBtn.classList.remove('active');

  if (replayFullBars && replayFullBars.length > 0) {
    rawBars = [...replayFullBars];
    replayFullBars = null;
    setPriceData();
    chart.fitContent();
  }
  setStatus('Exited replay — live chart restored');
}

// Chart canvas click -> start replay at hovered bar when in picking mode
el('chart').addEventListener('click', () => {
  if (replayPicking && replayPickIndex !== null) {
    startReplayAt(replayPickIndex);
  }
});

// Replay button toggle
replayBtn.addEventListener('click', () => {
  if (isReplaying || replayPicking) {
    askExitReplay();
  } else {
    enterReplay();
  }
});

rpPickCancel.addEventListener('click', cancelPick);
rpExit.addEventListener('click', askExitReplay);
rpLeaveStay.addEventListener('click', () => { replayLeaveModal.hidden = true; });
rpLeaveGo.addEventListener('click', exitReplay);

rpPlay.addEventListener('click', () => {
  if (!isReplaying) return;
  if (replayIsPlaying) {
    barSocket.send({ type: 'replay_pause' });
  } else {
    barSocket.send({ type: 'replay_resume' });
  }
});

rpBack.addEventListener('click', () => {
  if (!isReplaying || !replayFullBars || replayCurrentIndex <= 0) return;
  replayCurrentIndex--;
  rawBars = replayFullBars.slice(0, replayCurrentIndex + 1);
  setPriceData();
  syncReplayBarUI(replayCurrentIndex, replayTotalBars, rawBars[replayCurrentIndex] ?? null, false, replaySpeed);
  barSocket.send({ type: 'replay_pause' });
});

rpFwd.addEventListener('click', () => {
  if (!isReplaying) return;
  barSocket.send({ type: 'replay_step' });
});

rpScrub.addEventListener('input', () => {
  if (!isReplaying || !replayFullBars) return;
  const target = Math.max(0, Math.min(replayTotalBars - 1, Number(rpScrub.value)));
  replayCurrentIndex = target;
  rawBars = replayFullBars.slice(0, target + 1);
  setPriceData();
  syncReplayBarUI(target, replayTotalBars, rawBars[target] ?? null, false, replaySpeed);
  barSocket.send({ type: 'replay_pause' });
});

rpSpeed.addEventListener('click', () => {
  const at = REPLAY_SPEEDS.indexOf(replaySpeed);
  replaySpeed = REPLAY_SPEEDS[(at + 1) % REPLAY_SPEEDS.length] ?? 1;
  barSocket.send({ type: 'replay_speed', speed: replaySpeed });
  syncReplayBarUI(replayCurrentIndex, replayTotalBars, rawBars[rawBars.length - 1] ?? null, replayIsPlaying, replaySpeed);
});

window.addEventListener('keydown', (e: KeyboardEvent) => {
  if (e.key === 'Escape') {
    if (!replayLeaveModal.hidden) { replayLeaveModal.hidden = true; return; }
    if (replayPicking) cancelPick();
  }
});

barSocket.on('replay_started', (msg: any) => {
  isReplaying = true;
  replayIsPlaying = true;
  if (msg?.total_bars) replayTotalBars = Number(msg.total_bars);
  syncReplayBarUI(replayCurrentIndex, replayTotalBars, rawBars[rawBars.length - 1] ?? null, true, replaySpeed);
  setStatus(`Replay active: ${msg?.instrument || ''}`, true);
});

barSocket.on('replay_paused', () => {
  replayIsPlaying = false;
  syncReplayBarUI(replayCurrentIndex, replayTotalBars, rawBars[rawBars.length - 1] ?? null, false, replaySpeed);
  setStatus('Replay paused');
});

barSocket.on('replay_resumed', () => {
  replayIsPlaying = true;
  syncReplayBarUI(replayCurrentIndex, replayTotalBars, rawBars[rawBars.length - 1] ?? null, true, replaySpeed);
  setStatus('Replay running', true);
});

barSocket.on('replay_stepped', () => {
  replayIsPlaying = false;
  replayCurrentIndex = rawBars.length - 1;
  syncReplayBarUI(replayCurrentIndex, replayTotalBars, rawBars[replayCurrentIndex] ?? null, false, replaySpeed);
  setStatus('Stepped 1 bar');
});

barSocket.on('replay_stopped', () => {
  exitReplay();
});

barSocket.on('replay_done', () => {
  replayIsPlaying = false;
  syncReplayBarUI(replayTotalBars - 1, replayTotalBars, rawBars[rawBars.length - 1] ?? null, false, replaySpeed);
  setStatus('Replay completed');
});

// Initial connection
connect();
