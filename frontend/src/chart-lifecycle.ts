import {
  createChart, CandleBuilder, intervalToSeconds, darkTheme,
  registeredIndicators, type Bar, type IndicatorDescriptor,
} from 'openalgo-charts';
import 'openalgo-charts/indicators';
import {
  runTransform, HeikinAshiTransform, RenkoTransform,
  RangeBarsTransform, LineBreakTransform, PointFigureTransform, KagiTransform,
} from 'openalgo-charts/transform';
import { authHeaders, getApiKey, setApiKey } from './apikey';
import { API_BASE, WS_BASE, barSocket } from './feed';
import { mapInterval, mapWsInterval } from './chart-types';
import { RawBar, type BookOrder, type BookPosition } from './chart-state';
import {
  chart, price, volume, ltpLine,
  rawBars, builder, liveAgg, lastLtp, ctxPrice, lastReq,
  isReplaying, replayPicking, replayPickIndex, replayShade, replayFullBars,
  replayTotalBars, replayCurrentIndex, replayIsPlaying, replaySpeed, REPLAY_SPEEDS,
  tickN, bookTimer, orderLines, posLine, position, setTickN,
  unsubBar, unsubDepth, unsubQuote, unsubOrder, unsubFill, unsubPos, unsubOpen, unsubClose,
  setChart, setPrice, setVolume, setLtpLine,
  setRawBars, setBuilder, setLiveAgg, setLastLtp, setCtxPrice, setLastReq,
  setIsReplaying, setReplayPicking, setReplayPickIndex, setReplayShade, setReplayFullBars,
  setReplayTotalBars, setReplayCurrentIndex, setReplayIsPlaying, setReplaySpeed,
  setBookTimer, setPosLine, setPosition,
  setUnsubBar, setUnsubDepth, setUnsubQuote, setUnsubOrder, setUnsubFill, setUnsubPos, setUnsubOpen, setUnsubClose,
} from './chart-state';
// ── Chart types ────────────────────────────────
const boxOf = (): number => {
  const lastBar = rawBars.length > 0 ? rawBars[rawBars.length - 1] : undefined;
  const c = lastBar ? lastBar.close : 100;
  return Math.max(0.05, Math.round((c * 0.0015) / 0.05) * 0.05);
};

const CHART_TYPES: Record<string, { series: string; style?: () => any; tf?: () => any }> = {
  candlestick: { series: 'candlestick' },
  'hollow-candle': { series: 'hollow-candle' }, bar: { series: 'bar' },
  'high-low': { series: 'high-low' }, 'volume-candle': { series: 'volume-candle' },
  line: { series: 'line' }, 'line-markers': { series: 'line-markers' },
  step: { series: 'step' }, area: { series: 'area' }, 'hlc-area': { series: 'hlc-area' },
  baseline: { series: 'baseline', style: () => ({ baseValue: rawBars.reduce((s, b) => s + b.close, 0) / (rawBars.length || 1) }) },
  'heikin-ashi': { series: 'candlestick', tf: () => new HeikinAshiTransform() },
  renko: { series: 'candlestick', tf: () => new RenkoTransform({ boxSize: boxOf() }) },
  range: { series: 'candlestick', tf: () => new RangeBarsTransform({ range: boxOf() }) },
  'line-break': { series: 'candlestick', tf: () => new LineBreakTransform({ lines: 3 }) },
  'point-figure': { series: 'point-figure', style: () => ({ boxSize: boxOf() }), tf: () => new PointFigureTransform({ boxSize: boxOf(), reversal: 3 }) },
  kagi: { series: 'kagi', tf: () => new KagiTransform({ reversal: boxOf() * 2 }) },
};

const ctypeEl = document.getElementById('ctype') as HTMLSelectElement | null;
export const chartType = () => CHART_TYPES[ctypeEl?.value || 'candlestick'] || CHART_TYPES.candlestick;

// ── Helpers ────────────────────────────────────
export function setStatus(msg: string, active = false): void {
  const s = document.getElementById('status');
  if (s) s.textContent = msg;
  const dot = document.querySelector('.status-dot');
  if (dot) dot.classList.toggle('active', active);
}

export function marketPrice(): number | null {
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

const fmt = (n: number | null | undefined): string =>
  n == null || isNaN(n) ? '-' : Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export const round2v = (n: number): number => Math.round(n * 100) / 100;
const nowSec = (): number => Math.floor(Date.now() / 1000);

// ── Volume bucketing ───────────────────────────
function bucketVolume(tbars: any[]): any[] {
  const out: any[] = [];
  let ri = 0;
  for (const tb of tbars) {
    let v = 0;
    while (ri < rawBars.length && (rawBars[ri]?.time ?? 0) <= tb.time) { v += rawBars[ri]?.volume || 0; ri++; }
    out.push({ time: tb.time, open: 0, high: v, low: 0, close: v });
  }
  let rest = 0;
  while (ri < rawBars.length) { rest += rawBars[ri]?.volume || 0; ri++; }
  if (out.length && rest) { const last = out[out.length - 1]; if (last) { last.high += rest; last.close += rest; } }
  return out;
}

export function setPriceData(): void {
  if (!price || !rawBars.length) return;
  const cfg = chartType() || CHART_TYPES.candlestick;
  if (cfg && cfg.tf) {
    const t = runTransform(cfg.tf(), rawBars as any);
    price.setData(t);
    volume.setData(bucketVolume(t));
  } else {
    price.setData(rawBars);
    volume.setData(rawBars.map((b) => ({ time: b.time, open: 0, high: b.volume || 0, low: 0, close: b.volume || 0 })));
  }
}

export function setLegend(sym: string, ex: string, intv: string, bar?: RawBar, ltp?: number | null): void {
  const col = bar && bar.close >= bar.open ? 'up' : 'dn';
  const legend = document.getElementById('legend');
  if (legend) {
    legend.innerHTML = `<b>${esc(sym)}</b> <span style="opacity:.5">· ${esc(intv)} · ${esc(ex)}</span>` +
      (bar ? ` <span class="${col}">O ${fmt(bar.open)} H ${fmt(bar.high)} L ${fmt(bar.low)} C ${fmt(bar.close)}</span>` : '') +
      (ltp != null ? ` <span class="ltp">LTP ${fmt(ltp)}</span>` : '');
  }
}

const esc = (s: string): string =>
  String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] || c);

// ── Order/position drawing ────────────────
export function makeOrderLine(o: BookOrder): any {
  const px = o.trigger_price ?? o.triggerPrice ?? o.price;
  const side = (o.side || 'BUY').toUpperCase();
  const typ = (o.type || 'LIMIT').toUpperCase();
  return chart.addPriceLine({ price: px, color: side === 'BUY' ? '#26a69a' : '#ef5350', lineWidth: 1, dashed: true, id: `order:${o.id}`, cursor: 'ns-resize', extentFromRight: 0.3, closeButton: true, badge: `${side} ${o.qty} ${typ}`, qty: o.qty, leftLabel: typ }, 0);
}

export function renderPosition(pos: BookPosition | null): void {
  if (posLine && chart) { chart.removePrimitive(posLine); setPosLine(null); }
  const net = pos ? Number(pos.net_qty ?? pos.netQty ?? 0) : 0;
  const avg = pos ? Number(pos.avg_price ?? pos.avgPrice ?? 0) : 0;
  setPosition(net !== 0 ? { net, avg, product: pos?.product } : null);
  if (!position || !chart) return;
  setPosLine(chart.addPriceLine({ price: position.avg, color: position.net > 0 ? '#2e7d6b' : '#a14a52', lineWidth: 2, dashed: false, id: 'position', extentFromRight: 0.3, closeButton: true, badge: position.net > 0 ? 'LONG' : 'SHORT', qty: Math.abs(position.net), leftLabel: posLabel() }, 0));
}

// ── Book polling ──────────────────────────
export async function pollBook(): Promise<void> {
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
      if (rec) { rec.order = o; rec.line.setPrice(px); }
      else if (chart) { orderLines.set(o.id, { line: makeOrderLine(o), order: o }); }
    }
    for (const [id, rec] of orderLines) {
      if (!seen.has(id) && chart) { chart.removePrimitive(rec.line); orderLines.delete(id); }
    }
    const myPos = positions.find((p) => p.symbol === lastReq?.symbol && Number(p.net_qty ?? p.netQty ?? 0) !== 0);
    renderPosition(myPos || null);
  } catch (_) { /* transient */ }
}

// ── Order actions ─────────────────────────
export async function cancelOrder(orderId: string): Promise<void> {
  setStatus(`cancelling order ${orderId}...`);
  try {
    const res = await fetch(`${API_BASE}/orders/${encodeURIComponent(orderId)}`, { method: 'DELETE', headers: { 'Idempotency-Key': crypto.randomUUID(), ...authHeaders() } });
    if (res.ok) { setStatus(`cancelled order ${orderId}`); pollBook(); } else setStatus(`cancel error for order ${orderId}`);
  } catch (e: any) { setStatus('cancel error: ' + (e?.message || 'failed')); }
}

export async function exitPosition(): Promise<void> {
  if (!position || !lastReq) return;
  const arm = document.getElementById('arm') as HTMLInputElement | null;
  if (!arm?.checked) { setStatus('Tick "Arm trading" to exit the position'); arm?.focus(); return; }
  const qty = Math.abs(position.net);
  const side = position.net > 0 ? 'SELL' : 'BUY';
  const summary = `close ${position.net > 0 ? 'LONG' : 'SHORT'} ${qty} ${lastReq.symbol} @ market`;
  if (!window.confirm(`${summary}\n\nProceed?`)) return;
  setStatus(summary + ' ...');
  try {
    const res = await fetch(`${API_BASE}/orders`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID(), ...authHeaders() }, body: JSON.stringify({ symbol: lastReq.symbol, exchange: lastReq.exchange, side, order_type: 'MARKET', quantity: qty, product: position.product || (document.getElementById('product') as HTMLSelectElement)?.value }) });
    if (res.ok) { setStatus('position closed'); pollBook(); }
    else { const err = await res.json().catch(() => ({ detail: 'failed' })); setStatus('exit error: ' + (err.detail || 'failed')); }
  } catch (e: any) { setStatus('exit error: ' + (e?.message || 'failed')); }
}

export async function placeFromMenu(side: string, type: string): Promise<void> {
  const arm = document.getElementById('arm') as HTMLInputElement | null;
  if (!arm?.checked) { setStatus('Tick "Arm trading" in the toolbar to place orders'); arm?.focus(); return; }
  const sym = lastReq?.symbol || (document.getElementById('symbol') as HTMLInputElement)?.value.trim().toUpperCase();
  const ex = lastReq?.exchange || (document.getElementById('exchange') as HTMLSelectElement)?.value;
  const qty = Math.max(1, Number((document.getElementById('qty') as HTMLInputElement)?.value) || 1);
  const product = (document.getElementById('product') as HTMLSelectElement)?.value;
  const priceVal = type === 'MARKET' ? 0 : round2v(ctxPrice);
  if (!stopSideOk(side, type, priceVal)) { const m = marketPrice(); setStatus(`${side} Stop must be ${side === 'BUY' ? 'ABOVE' : 'BELOW'} the price (LTP ${fmt(m)})`); return; }
  const apiType = type === 'SL' ? 'STOP' : type === 'SL-M' ? 'STOP_LIMIT' : type;
  const summary = `${side} ${type} ${qty} ${sym}` + (type === 'MARKET' ? '' : ` @ ${fmt(priceVal)}`) + ` (${product})`;
  setStatus('placing: ' + summary + ' ...');
  try {
    const orderPayload: Record<string, unknown> = { symbol: sym, exchange: ex, side: side.toUpperCase(), order_type: apiType, quantity: qty };
    if (apiType === 'LIMIT' || apiType === 'STOP_LIMIT') orderPayload.price = priceVal;
    if (apiType === 'STOP' || apiType === 'STOP_LIMIT') orderPayload.trigger_price = priceVal;
    const res = await fetch(`${API_BASE}/orders`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID(), ...authHeaders() }, body: JSON.stringify(orderPayload) });
    const j = await res.json().catch(() => ({}));
    if (res.ok) { const orderId = j.order_id || j.id || (typeof j === 'string' ? j : 'ok'); setStatus(`placed ${summary} (id ${orderId})`, true); pollBook(); }
    else setStatus('order error: ' + (j.detail || j.message || 'failed'));
  } catch (e: any) { setStatus('order error: ' + (e?.message || 'failed')); }
}

export function clearTrading(): void {
  if (bookTimer) { clearInterval(bookTimer); setBookTimer(null); }
  for (const [, rec] of orderLines) { if (chart) chart.removePrimitive(rec.line); }
  orderLines.clear();
  if (posLine && chart) { chart.removePrimitive(posLine); setPosLine(null); }
  setPosition(null);
}

// ── Chart wiring ──────────────────────
function wireChart(): void {
  chart.subscribeDrag(
    (id: string, p: number) => {
      if (!id.startsWith('order:') || id.endsWith('::close')) return;
      const rec = orderLines.get(id.slice(6));
      if (!rec) return;
      if (rec.dragFrom == null) { rec.dragFrom = rec.line.price; rec.line.setDragGhost(rec.dragFrom); }
      rec.line.setPrice(round2v(p));
    },
    async (id: string, p: number) => {
      if (!id.startsWith('order:') || id.endsWith('::close')) return;
      const oid = id.slice(6); const rec = orderLines.get(oid);
      if (!rec) return;
      rec.line.setDragGhost(null); rec.dragFrom = null;
      const px = round2v(p); setStatus(`modifying ${rec.order.side} ${rec.order.type} to ${fmt(px)} ...`);
      try {
        const res = await fetch(`${API_BASE}/orders/${encodeURIComponent(oid)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID(), ...authHeaders() }, body: JSON.stringify({ price: px }) });
        if (res.ok) { setStatus(`modified ${rec.order.side} ${rec.order.type} to ${fmt(px)}`); pollBook(); }
        else { const err = await res.json().catch(() => ({ detail: 'modify failed' })); setStatus(`modify error: ${err.detail || 'failed'}`); pollBook(); }
      } catch (e: any) { setStatus('modify error: ' + (e?.message || 'failed')); pollBook(); }
    },
  );
  chart.subscribeClick((id: string) => {
    if (id === 'position::close') { exitPosition(); return; }
    if (id.startsWith('order:') && id.endsWith('::close')) cancelOrder(id.slice(6, -7));
  });
  chart.subscribeCrosshairMove((e: any) => { if (replayPicking) movePick(e.index); });
}

export function buildChart(): void {
  const savedIndicators: { indicatorId: string; settings: any }[] = [];
  if (chart) { try { for (const i of chart.indicators()) savedIndicators.push({ indicatorId: i.indicatorId, settings: i.settings?.() || {} }); } catch (_) {} chart.destroy(); }
  const chartEl = document.getElementById('chart');
  if (!chartEl) return;
  chartEl.innerHTML = '';
  setChart(createChart(chartEl, { theme: darkTheme, priceAxisWidth: 72 }));
  const cfg = chartType() ?? { series: 'candlestick' };
  const seriesStyle = cfg.style ? cfg.style() : {};
  setPrice(chart.addSeries(cfg.series, { style: seriesStyle }));
  setVolume(chart.addSeries('histogram', { paneIndex: 1, style: { color: '#33415e' } }));
  setPriceData();
  if (chart.timeScale && chart.timeScale.barSpacing > 6) chart.timeScale.setBarSpacing(6);
  for (const item of savedIndicators) { try { chart.addIndicator(item.indicatorId, item.settings); } catch (_) {} }
  function renderActiveChips(): void {
  const bar = document.getElementById('ind-active-bar');
  const chips = document.getElementById('ind-active-chips');
  if (!bar || !chips) return;
  if (!chart) { bar.hidden = true; return; }
  const current = chart.indicators();
  if (!current.length) { bar.hidden = true; chips.innerHTML = ''; return; }
  bar.hidden = false;
  chips.innerHTML = current.map((inst: any) => `<span class="ind-active-chip">${esc(inst.name)} <button class="remove-btn" data-id="${esc(inst.id)}" title="Remove ${esc(inst.name)}">✕</button></span>`).join('');
  chips.querySelectorAll('.remove-btn').forEach((btn) => {
    btn.addEventListener('click', (e) => { e.stopPropagation(); const id = (btn as HTMLElement).getAttribute('data-id'); if (id && chart) { chart.removeIndicator(id); renderActiveChips(); } });
  });
}
  if (replayPicking && replayPickIndex !== null) { setReplayShade(new ReplayShade({ index: replayPickIndex })); chart.addPrimitive(replayShade, 0); } else { setReplayShade(null); }
  const lastBar = rawBars.length > 0 ? rawBars[rawBars.length - 1] : undefined;
  const lp = lastLtp != null ? lastLtp : lastBar ? lastBar.close : null;
  setLtpLine(lp != null ? chart.addPriceLine({ price: lp, color: '#e0b020', lineWidth: 1, dashed: true, id: 'ltp' }, 0) : null);
  wireChart(); orderLines.clear(); setPosLine(null); pollBook();
}

// ── WS lifecycle ──────────────────────
export function disconnect(): void {
  if (unsubBar) { unsubBar(); setUnsubBar(null); }
  if (unsubDepth) { unsubDepth(); setUnsubDepth(null); }
  if (unsubQuote) { unsubQuote(); setUnsubQuote(null); }
  if (unsubOrder) { unsubOrder(); setUnsubOrder(null); }
  if (unsubFill) { unsubFill(); setUnsubFill(null); }
  if (unsubPos) { unsubPos(); setUnsubPos(null); }
  if (unsubOpen) { unsubOpen(); setUnsubOpen(null); }
  if (unsubClose) { unsubClose(); setUnsubClose(null); }
  clearTrading();
  const wsState = document.getElementById('wsstate');
  if (wsState) wsState.textContent = 'WS idle';
}

export async function connect(): Promise<void> {
  disconnect();
  const apiKeyInput = document.getElementById('apikey') as HTMLInputElement | null;
  const userKey = apiKeyInput ? apiKeyInput.value.trim() : '';
  if (userKey) setApiKey(userKey);
  const symbol = (document.getElementById('symbol') as HTMLInputElement)?.value.trim().toUpperCase();
  const exchange = (document.getElementById('exchange') as HTMLSelectElement)?.value;
  const interval = (document.getElementById('interval') as HTMLSelectElement)?.value;
  if (!symbol || !exchange || !interval) return;
  const instrument = `${exchange}:${symbol}`;
  setLastReq({ symbol, exchange, interval });
  setLastLtp(null); setRawBars([]);
  setStatus(`connecting to TradeX backend (${symbol} ${interval})...`);
  try {
    const to = nowSec();
    const url = `${API_BASE}/api/charts/history/${encodeURIComponent(instrument)}?interval=${encodeURIComponent(interval)}&to=${to}`;
    const res = await fetch(url, { headers: { ...authHeaders() } });
    if (!res.ok) { setStatus(`history error: HTTP ${res.status}`); return; }
    const data = await res.json();
    setRawBars((data.bars || []).map((b: any) => ({ time: b.time, open: Number(b.open), high: Number(b.high), low: Number(b.low), close: Number(b.close), volume: Number(b.volume || 0) })));
    if (!rawBars.length) { setStatus(`no history for ${symbol} ${exchange} ${interval}`); return; }
    buildChart();
    const lastBar = rawBars[rawBars.length - 1];
    if (lastBar) { setLastLtp(lastBar.close); const ltpEl = document.getElementById('ltp'); if (ltpEl) ltpEl.textContent = `LTP ${fmt(lastLtp)}`; setLegend(symbol, exchange, interval, lastBar, lastBar.close); }
    setStatus(`${symbol} · ${rawBars.length} bars loaded · connecting live feed...`, true);
    setLiveAgg(/^\d+(m|h)$/i.test(interval));
    setBuilder(new CandleBuilder({ intervalSec: intervalToSeconds(interval), volumeMode: 'ltq-sum' }));
    if (liveAgg && rawBars.length > 0 && rawBars[rawBars.length - 1]) builder.seed(rawBars[rawBars.length - 1] as Bar);
    setTickN(0);

    const onTickUpdate = (ltp: number) => {
      if (ltp == null || isNaN(ltp) || ltp <= 0) return;
      const lastBar = rawBars.length > 0 ? rawBars[rawBars.length - 1] : undefined;
      if (lastBar && lastBar.close > 0) { const ratio = ltp / lastBar.close; if (ratio < 0.4 || ratio > 2.5) { console.warn(`Ignoring out-of-range tick ${ltp}`); return; } }
      setTickN(tickN + 1);
      const wsState = document.getElementById('wsstate'); if (wsState) wsState.textContent = 'WS live';
      const ticksEl = document.getElementById('ticks'); if (ticksEl) ticksEl.textContent = `${tickN + 1} ticks`;
      setLastLtp(ltp);
      const ltpEl = document.getElementById('ltp'); if (ltpEl) ltpEl.textContent = `LTP ${fmt(ltp)}`;
      if (position && posLine) posLine.setLeftLabel(posLabel());
      if (liveAgg && builder) { const u = builder.onTick({ time: nowSec(), price: ltp }); if (u && u.bar) { const curBar = u.bar as RawBar; const i = rawBars.findIndex((b) => b.time === curBar.time); if (i >= 0) rawBars[i] = curBar; else rawBars.push(curBar); setPriceData(); const curLast = rawBars[rawBars.length - 1]; if (curLast && lastReq) setLegend(lastReq.symbol, lastReq.exchange, lastReq.interval, curLast, ltp); } }
      else { const curLast = rawBars[rawBars.length - 1]; if (curLast && lastReq) setLegend(lastReq.symbol, lastReq.exchange, lastReq.interval, curLast, ltp); }
    };

    const onBarUpdate = (b: Bar) => {
      if (!b || b.close == null || isNaN(b.close) || b.close <= 0) return;
      if (!isReplaying) { const lastBar = rawBars.length > 0 ? rawBars[rawBars.length - 1] : undefined; if (lastBar && lastBar.close > 0) { const ratio = b.close / lastBar.close; if (ratio < 0.4 || ratio > 2.5) { console.warn(`Ignoring out-of-range bar ${b.close}`); return; } } }
      setTickN(tickN + 1);
      const wsState = document.getElementById('wsstate'); if (wsState) wsState.textContent = isReplaying ? 'Replay live' : 'WS live';
      const ticksEl = document.getElementById('ticks'); if (ticksEl) ticksEl.textContent = `${tickN + 1} ticks`;
      setLastLtp(b.close);
      const ltpEl = document.getElementById('ltp'); if (ltpEl) ltpEl.textContent = `LTP ${fmt(b.close)}`;
      if (position && posLine) posLine.setLeftLabel(posLabel());
      if (rawBars.length === 0) { rawBars.push(b as RawBar); setPriceData(); }
      else { const last = rawBars[rawBars.length - 1]; if (last && b.time === last.time) { rawBars[rawBars.length - 1] = { time: last.time, open: b.open != null ? b.open : last.open, high: Math.max(last.high, b.high != null ? b.high : b.close), low: Math.min(last.low, b.low != null ? b.low : b.close), close: b.close, volume: b.volume != null ? b.volume : last.volume }; setPriceData(); if (isReplaying) syncReplayBarUI(rawBars.length - 1, replayTotalBars, rawBars[rawBars.length - 1] ?? null, replayIsPlaying, replaySpeed); }
      else if (last && b.time > last.time) { rawBars.push(b as RawBar); setPriceData(); if (isReplaying) { setReplayCurrentIndex(rawBars.length - 1); syncReplayBarUI(rawBars.length - 1, replayTotalBars, rawBars[rawBars.length - 1] ?? null, replayIsPlaying, replaySpeed); } }
      else if (isReplaying) { const idx = rawBars.findIndex((r) => r.time === b.time); const target = idx >= 0 ? rawBars[idx] : undefined; if (target) { rawBars[idx] = { time: b.time, open: b.open != null ? b.open : target.open, high: Math.max(target.high, b.high != null ? b.high : b.close), low: Math.min(target.low, b.low != null ? b.low : b.close), close: b.close, volume: b.volume != null ? b.volume : target.volume }; } else { rawBars.push(b as RawBar); rawBars.sort((x, y) => x.time - y.time); } setPriceData(); } }
      const curLast = rawBars[rawBars.length - 1]; if (curLast && lastReq) setLegend(lastReq.symbol, lastReq.exchange, lastReq.interval, curLast, lastLtp);
    };

    setUnsubOpen(barSocket.onOpen(() => { const wsState = document.getElementById('wsstate'); if (wsState) wsState.textContent = 'WS live'; barSocket.send({ type: 'subscribe_orders' }); }));
    setUnsubClose(barSocket.onClose(() => { const wsState = document.getElementById('wsstate'); if (wsState) wsState.textContent = 'WS closed'; }));
    setUnsubDepth(barSocket.subscribeDepth(instrument, (d: any) => { if (d && d.ltp != null) onTickUpdate(Number(d.ltp)); }));
    setUnsubBar(barSocket.subscribe(instrument, interval, onBarUpdate));
    setUnsubQuote(barSocket.on('quote', (q: any) => { if (q && q.instrument === instrument && q.ltp != null) onTickUpdate(Number(q.ltp)); }));
    setUnsubOrder(barSocket.on('order', () => pollBook()));
    setUnsubFill(barSocket.on('fill', () => pollBook()));
    setUnsubPos(barSocket.on('position', () => pollBook()));
    setBookTimer(setInterval(pollBook, 4000));
    pollBook(); setStatus(`${symbol} · TradeX connected`, true);
  } catch (e: any) { setStatus('connect error: ' + (e?.message || 'failed')); }
}

// ── Replay helpers ──────────────────
function formatBarTime(ts: number): string {
  const d = new Date(ts * 1000); const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

class ReplayShade {
  private _opts: Required<Omit<ReplayShadeOptions, 'index'>> & { index: number | null };
  private _host?: any;
  constructor(opts: ReplayShadeOptions) { this._opts = { index: opts.index, color: opts.color ?? 'rgba(12, 14, 20, 0.62)', lineColor: opts.lineColor ?? '#3b82f6', lineWidth: opts.lineWidth ?? 1, lineVisible: opts.lineVisible ?? true }; }
  zOrder() { return 'top' as const; } autoscaleInfo() { return null; } hitTest() { return null; }
  attached(host: any) { this._host = host; } detached() { this._host = undefined; }
  setOptions(patch: Partial<ReplayShadeOptions>): void { this._opts = { ...this._opts, ...patch }; this._host?.requestUpdate?.(); }
  draw(ctx: CanvasRenderingContext2D, rc: any): void {
    const o = this._opts; if (o.index === null) return; const dpr = rc.dpr || 1;
    const w = rc.plotWidth * dpr; const h = rc.plotHeight * dpr; if (w <= 0 || h <= 0) return;
    const barSpacing = rc.timeScale?.barSpacing ?? 6; const half = (barSpacing * dpr) / 2;
    const barX = rc.timeScale?.indexToX ? rc.timeScale.indexToX(o.index) : null; if (barX == null || isNaN(barX)) return;
    const x = Math.round(barX * dpr + half); if (x >= w) return; const from = Math.max(0, x);
    ctx.save(); ctx.fillStyle = o.color; ctx.fillRect(from, 0, w - from, h);
    if (o.lineVisible && x >= 0) { ctx.strokeStyle = o.lineColor; ctx.lineWidth = Math.max(1, Math.round(o.lineWidth * dpr)); ctx.beginPath(); ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, h); ctx.stroke(); }
    ctx.restore();
  }
}

interface ReplayShadeOptions { index: number | null; color?: string; lineColor?: string; lineWidth?: number; lineVisible?: boolean; }

export function enterReplay(): void {
  if (isReplaying || replayPicking || !chart) return;
  if (rawBars.length < 2) { setStatus('Replay needs bars (load chart first)'); return; }
  setReplayPicking(true);
  const replayBtn = document.getElementById('replay-btn'); if (replayBtn) replayBtn.classList.add('active');
  const chartEl = document.getElementById('chart'); if (chartEl) chartEl.classList.add('is-picking');
  let from = 0; try { const range = chart.timeScale?.getVisibleLogicalRange?.(); if (range) from = Math.round(range.from); } catch (_) { from = 0; }
  const floor = Math.min(20, rawBars.length - 1); setReplayPickIndex(Math.max(floor, Math.min(rawBars.length - 1, from)));
  if (!replayShade) { setReplayShade(new ReplayShade({ index: replayPickIndex })); chart.addPrimitive(replayShade, 0); } else { replayShade.setOptions({ index: replayPickIndex }); }
  const pickPrompt = document.getElementById('replaypick'); if (pickPrompt) pickPrompt.hidden = false; syncPickHint(); setStatus('Replay: click a bar to start from (Esc to cancel)');
}

export function cancelPick(): void {
  if (!replayPicking) return; setReplayPicking(false); setReplayPickIndex(null);
  if (replayShade) replayShade.setOptions({ index: null });
  const chartEl = document.getElementById('chart'); if (chartEl) chartEl.classList.remove('is-picking');
  const pickPrompt = document.getElementById('replaypick'); if (pickPrompt) pickPrompt.hidden = true;
  const replayBtn = document.getElementById('replay-btn'); if (replayBtn) replayBtn.classList.remove('active'); setStatus('Replay cancelled');
}

function movePick(index: number | null | undefined): void {
  if (!replayPicking || index === null || index === undefined || !chart) return;
  const total = rawBars.length; if (total === 0) return;
  const clamped = Math.max(0, Math.min(total - 1, Math.round(index)));
  if (clamped === replayPickIndex) return; setReplayPickIndex(clamped); if (replayShade) replayShade.setOptions({ index: clamped }); syncPickHint();
}

function syncPickHint(): void { const b = rawBars[replayPickIndex ?? 0]; const rpPicked = document.getElementById('rp-picked'); if (rpPicked) rpPicked.textContent = b ? formatBarTime(b.time) : ''; }

export function startReplayAt(index: number): void {
  if (!chart || isReplaying) return; if (rawBars.length < 2) return;
  setReplayPicking(false);
  const chartEl = document.getElementById('chart'); if (chartEl) chartEl.classList.remove('is-picking');
  const pickPrompt = document.getElementById('replaypick'); if (pickPrompt) pickPrompt.hidden = true;
  if (replayShade) replayShade.setOptions({ index: null });
  if (!replayFullBars || replayFullBars.length === 0) setReplayFullBars([...rawBars]);
  setReplayCurrentIndex(index); setReplayTotalBars(replayFullBars!.length);
  const chosenBar = replayFullBars![index] ?? null; const firstBar = rawBars[0]; if (!chosenBar || !firstBar) return;
  const startTime = chosenBar.time; setRawBars(replayFullBars!.slice(0, index + 1)); setPriceData(); chart.fitContent();
  setIsReplaying(true); setReplayIsPlaying(true);
  const replayBar = document.getElementById('replaybar'); if (replayBar) replayBar.hidden = false;
  const replayBtn = document.getElementById('replay-btn'); if (replayBtn) replayBtn.classList.add('active');
  syncReplayBarUI(index, replayTotalBars, chosenBar, true, replaySpeed);
  const sym = (document.getElementById('symbol') as HTMLInputElement)?.value.trim().toUpperCase();
  const ex = (document.getElementById('exchange') as HTMLSelectElement)?.value;
  const intv = (document.getElementById('interval') as HTMLSelectElement)?.value;
  setStatus(`Replaying ${ex}:${sym} from ${formatBarTime(startTime)} (${replaySpeed}x)...`, true);
  barSocket.send({ type: 'replay_start', instrument: `${ex}:${sym}`, interval: intv === 'D' ? '1d' : intv, speed: replaySpeed, ticks_per_bar: 10, start_time: startTime });
}

export function syncReplayBarUI(index: number, total: number, bar: RawBar | null, playing: boolean, speed: number): void {
  const rpScrub = document.getElementById('rp-scrub') as HTMLInputElement | null; const rpCount = document.getElementById('rp-count'); const rpClock = document.getElementById('rp-clock'); const rpPlay = document.getElementById('rp-play'); const rpSpeed = document.getElementById('rp-speed');
  if (rpScrub) { rpScrub.max = String(Math.max(0, total - 1)); if (Number(rpScrub.value) !== index) rpScrub.value = String(index); }
  if (rpCount) rpCount.textContent = `${index + 1} / ${total}`; if (rpClock && bar) rpClock.textContent = formatBarTime(bar.time);
  if (rpPlay) { rpPlay.innerHTML = playing ? '⏸' : '▶'; rpPlay.title = playing ? 'Pause' : 'Play'; rpPlay.classList.toggle('is-on', playing); }
  if (rpSpeed) rpSpeed.textContent = `${speed}x`;
}

export function askExitReplay(): void { if (replayPicking) { cancelPick(); return; } if (!isReplaying) return; const modal = document.getElementById('replayleave'); if (modal) modal.hidden = false; }

export function exitReplay(): void {
  cancelPick(); const modal = document.getElementById('replayleave'); if (modal) modal.hidden = true;
  if (isReplaying) barSocket.send({ type: 'replay_stop' });
  setIsReplaying(false); setReplayIsPlaying(false);
  const replayBar = document.getElementById('replaybar'); if (replayBar) replayBar.hidden = true;
  const replayBtn = document.getElementById('replay-btn'); if (replayBtn) replayBtn.classList.remove('active');
  if (replayFullBars && replayFullBars.length > 0) { setRawBars([...replayFullBars]); setReplayFullBars(null); setPriceData(); chart.fitContent(); }
  setStatus('Exited replay — live chart restored');
}
