import type { Widget } from 'openalgo-charts/widget';

const STYLE_ID = 'tradex-shellbar-css';

const CSS = `
.tradex-topbar {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 6px 14px; background: linear-gradient(180deg, var(--panel), #0c0f16);
  border-bottom: 1px solid var(--bd-soft); min-height: 40px; flex: none; z-index: 10;
}
.tradex-brand {
  display: inline-flex; align-items: center; gap: 8px; font-weight: 700;
  letter-spacing: .2px; padding-right: 4px; color: var(--tx-strong); font-size: 14px;
}
.tradex-brand em {
  color: var(--live); font-style: normal; font-weight: 700; font-size: 10px;
  letter-spacing: 1px; border: 1px solid #40232a; background: #1c1216;
  padding: 1px 6px; border-radius: 4px; text-transform: uppercase;
}
.tradex-brand-dot {
  width: 8px; height: 8px; border-radius: 50%; background: var(--acc);
  box-shadow: 0 0 8px var(--acc);
}
.tradex-group { display: inline-flex; align-items: center; gap: 6px; }
.tradex-divider { width: 1px; height: 20px; background: var(--bd-soft); margin: 0 2px; }

.tradex-field {
  height: var(--h); background: var(--elev); color: var(--tx);
  border: 1px solid var(--bd); border-radius: 6px; padding: 0 9px;
  font: inherit; font-size: 12px; outline: none;
  transition: border-color .15s, box-shadow .15s, background .15s;
}
.tradex-field::placeholder { color: var(--faint); }
.tradex-field:hover { border-color: #33405a; }
.tradex-field:focus {
  border-color: var(--acc); box-shadow: 0 0 0 2px rgba(34,193,164,.15);
  background: var(--elev-2);
}
select.tradex-field { cursor: pointer; padding-right: 6px; }
.tradex-field--sym { width: 110px; font-weight: 600; text-transform: uppercase; letter-spacing: .3px; }
.tradex-field--qty { width: 64px; text-align: center; }

.tradex-btn {
  height: var(--h); display: inline-flex; align-items: center; gap: 6px; padding: 0 11px;
  background: var(--elev); color: var(--tx); border: 1px solid var(--bd); border-radius: 6px;
  cursor: pointer; font: inherit; font-size: 12px; font-weight: 500; transition: all .15s;
  user-select: none;
}
.tradex-btn:hover { border-color: #33405a; background: var(--elev-2); color: var(--tx-strong); }
.tradex-btn:active { transform: translateY(1px); }
.tradex-btn--active, .tradex-btn[aria-checked="true"] {
  background: var(--elev-2); border-color: var(--acc); color: var(--acc); font-weight: 600;
}
.tradex-pill {
  height: var(--h); padding: 0 7px; font-size: 11px; font-weight: 500;
}
.tradex-btn--primary {
  background: linear-gradient(180deg, var(--acc-2), var(--acc)); color: #04231d;
  border-color: transparent; font-weight: 600;
}
.tradex-btn--primary:hover { filter: brightness(1.08); }

.tradex-status {
  margin-left: auto; display: inline-flex; align-items: center; gap: 8px;
  color: var(--mut); font-size: 12px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
}
.tradex-status-dot {
  width: 7px; height: 7px; border-radius: 50%; background: var(--acc); flex: none;
  box-shadow: 0 0 6px var(--acc);
}

/* ── subbar ─────────────────────────────────────────────────────────── */
.tradex-subbar {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 4px 14px; background: #0c0f16; border-bottom: 1px solid var(--bd-soft);
  color: var(--mut); min-height: 32px; flex: none; font-size: 12px; z-index: 9;
}
.tradex-chip {
  display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px; font-size: 11px;
  background: var(--elev); border: 1px solid var(--bd-soft); border-radius: 999px; color: var(--mut);
}
.tradex-chip code { color: var(--tx); font-size: 11px; }
.tradex-chip--ltp { color: var(--amber); border-color: #3a3116; font-weight: 600; }
.tradex-chip--ws { color: var(--acc); }
.tradex-chip--analyze { color: var(--analyze); border-color: #1d3a4a; background: #0e1a22; font-weight: 600; }
.tradex-chip--live { color: var(--live); border-color: #4a2226; background: #1e1013; font-weight: 700; }

.tradex-lbl {
  display: inline-flex; align-items: center; gap: 6px; color: var(--faint); font-size: 11px;
  text-transform: uppercase; letter-spacing: .6px;
}
.tradex-lbl .tradex-field { text-transform: none; letter-spacing: normal; }
.tradex-chk {
  display: inline-flex; align-items: center; gap: 6px; cursor: pointer; user-select: none;
  padding: 2px 8px; border-radius: 6px; border: 1px solid var(--bd-soft);
  background: var(--elev); color: var(--mut); font-size: 11px;
}
.tradex-chk:hover { border-color: #33405a; color: var(--tx); }
.tradex-chk input { accent-color: var(--acc); width: 13px; height: 13px; cursor: pointer; }
.tradex-spacer { flex: 1 1 auto; }
.tradex-hint { color: var(--faint); font-size: 11px; font-style: italic; }
`;

function injectStyle(doc: Document): void {
  if (doc.getElementById(STYLE_ID) !== null) return;
  const s = doc.createElement('style');
  s.id = STYLE_ID;
  s.textContent = CSS;
  doc.head.appendChild(s);
}

export interface ShellbarOptions {
  symbol: string;
  exchange: string;
  interval: string;
  intervals: string[];
  broker?: string;
  onSymbolChange?: (symbol: string, exchange: string) => void;
  onIntervalChange?: (interval: string) => void;
  onChartTypeChange?: (type: string) => void;
}

let qtyInput: HTMLInputElement | null = null;
let productSelect: HTMLSelectElement | null = null;
let armCheckbox: HTMLInputElement | null = null;
let ltpChip: HTMLElement | null = null;
let ticksChip: HTMLElement | null = null;
let wsStateChip: HTMLElement | null = null;
let statusSpan: HTMLElement | null = null;
let statusDot: HTMLElement | null = null;
let tickCounter = 0;

export function mountShellbar(parent: HTMLElement, widget: Widget, opts: ShellbarOptions): void {
  const doc = parent.ownerDocument;
  injectStyle(doc);

  // 1. Topbar
  const topbar = doc.createElement('header');
  topbar.className = 'tradex-topbar';

  // Brand
  const brand = doc.createElement('div');
  brand.className = 'tradex-brand';
  const brandDot = doc.createElement('span');
  brandDot.className = 'tradex-brand-dot';
  const brandText = doc.createTextNode(' TradeX ');
  const badge = doc.createElement('em');
  badge.textContent = (opts.broker ?? 'paper').toUpperCase() === 'PAPER' ? 'PAPER' : 'LIVE';
  brand.append(brandDot, brandText, badge);

  const div1 = doc.createElement('div');
  div1.className = 'tradex-divider';

  // Instrument group
  const instGroup = doc.createElement('div');
  instGroup.className = 'tradex-group';

  const symInput = doc.createElement('input');
  symInput.type = 'text';
  symInput.className = 'tradex-field tradex-field--sym';
  symInput.value = opts.symbol;
  symInput.title = 'Instrument symbol';
  symInput.placeholder = 'SYMBOL';

  const exSelect = doc.createElement('select');
  exSelect.className = 'tradex-field';
  exSelect.title = 'Exchange';
  for (const ex of ['NSE', 'NFO', 'BSE', 'BFO', 'MCX']) {
    const opt = doc.createElement('option');
    opt.value = ex;
    opt.textContent = ex;
    if (ex === opts.exchange) opt.selected = true;
    exSelect.appendChild(opt);
  }

  instGroup.append(symInput, exSelect);

  const ivGroup = doc.createElement('div');
  ivGroup.className = 'tradex-group';
  ivGroup.setAttribute('role', 'radiogroup');
  ivGroup.setAttribute('aria-label', 'Interval');

  const intervalBtns: HTMLButtonElement[] = [];
  const setIntervalActive = (code: string) => {
    for (const b of intervalBtns) {
      const active = b.dataset.interval === code;
      b.setAttribute('aria-pressed', active ? 'true' : 'false');
      b.setAttribute('aria-checked', active ? 'true' : 'false');
      b.classList.toggle('tradex-btn--active', active);
    }
  };

  for (const iv of opts.intervals) {
    const btn = doc.createElement('button');
    btn.type = 'button';
    btn.className = 'tradex-btn tradex-pill';
    btn.setAttribute('role', 'radio');
    btn.setAttribute('aria-label', `Interval ${iv}`);
    btn.dataset.interval = iv;
    btn.textContent = iv;
    const isCur = iv === opts.interval;
    btn.setAttribute('aria-pressed', isCur ? 'true' : 'false');
    btn.setAttribute('aria-checked', isCur ? 'true' : 'false');
    if (isCur) btn.classList.add('tradex-btn--active');
    btn.onclick = () => {
      setIntervalActive(iv);
      widget.setInterval(iv);
      if (opts.onIntervalChange) opts.onIntervalChange(iv);
      if (statusSpan) statusSpan.textContent = `${symInput.value} · ${iv} · ${exSelect.value}`;
    };
    intervalBtns.push(btn);
    ivGroup.appendChild(btn);
  }

  const ctypeSelect = doc.createElement('select');
  ctypeSelect.className = 'tradex-field';
  ctypeSelect.title = 'Chart type';
  const ctypes = [
    { label: 'Candles', value: 'candlestick' },
    { label: 'Hollow Candles', value: 'hollow-candle' },
    { label: 'Bars (OHLC)', value: 'bar' },
    { label: 'High-Low', value: 'high-low' },
    { label: 'Line', value: 'line' },
    { label: 'Area', value: 'area' },
    { label: 'Baseline', value: 'baseline' },
    { label: 'Heikin Ashi', value: 'heikin-ashi' },
    { label: 'Renko', value: 'renko' },
  ];
  for (const ct of ctypes) {
    const opt = doc.createElement('option');
    opt.value = ct.value;
    opt.textContent = ct.label;
    ctypeSelect.appendChild(opt);
  }

  const div2 = doc.createElement('div');
  div2.className = 'tradex-divider';

  // Action group
  const actionGroup = doc.createElement('div');
  actionGroup.className = 'tradex-group';

  const indBtn = doc.createElement('button');
  indBtn.className = 'tradex-btn';
  indBtn.textContent = 'Indicators';
  indBtn.title = 'Indicators & backend studies';
  indBtn.onclick = () => widget.openIndicatorPicker();

  const objBtn = doc.createElement('button');
  objBtn.className = 'tradex-btn';
  objBtn.textContent = 'Objects';
  objBtn.title = 'Indicator pane settings & drawings tree';
  objBtn.onclick = () => widget.openObjects();

  const fitBtn = doc.createElement('button');
  fitBtn.className = 'tradex-btn';
  fitBtn.textContent = 'Fit';
  fitBtn.title = 'Fit bars and auto-scale view';
  fitBtn.onclick = () => {
    try {
      (widget.chart as unknown as { resetView?: () => void }).resetView?.();
    } catch (_) {}
  };

  const snapBtn = doc.createElement('button');
  snapBtn.className = 'tradex-btn';
  snapBtn.textContent = '📷 PNG';
  snapBtn.title = 'Save snapshot image';
  snapBtn.onclick = () => {
    widget.chart.downloadScreenshot(`${symInput.value}_${widget.interval()}_chart.png`);
  };

  actionGroup.append(indBtn, objBtn, fitBtn, snapBtn);

  // Status
  const statusContainer = doc.createElement('div');
  statusContainer.className = 'tradex-status';
  statusDot = doc.createElement('span');
  statusDot.className = 'tradex-status-dot';
  statusSpan = doc.createElement('span');
  statusSpan.textContent = `${opts.symbol} · ${opts.interval} · ${opts.exchange}`;
  statusContainer.append(statusDot, statusSpan);

  topbar.append(brand, div1, instGroup, ivGroup, ctypeSelect, div2, actionGroup, statusContainer);

  // 2. Subbar
  const subbar = doc.createElement('div');
  subbar.className = 'tradex-subbar';

  const restChip = doc.createElement('span');
  restChip.className = 'tradex-chip';
  restChip.innerHTML = 'REST <code>/api/charts/history</code>';

  wsStateChip = doc.createElement('span');
  wsStateChip.className = 'tradex-chip tradex-chip--ws';
  wsStateChip.textContent = '● WS live';

  ticksChip = doc.createElement('span');
  ticksChip.className = 'tradex-chip';
  ticksChip.textContent = '0 ticks';

  ltpChip = doc.createElement('span');
  ltpChip.className = 'tradex-chip tradex-chip--ltp';
  ltpChip.textContent = 'LTP -';

  const spacer = doc.createElement('span');
  spacer.className = 'tradex-spacer';

  const qtyLabel = doc.createElement('label');
  qtyLabel.className = 'tradex-lbl';
  qtyLabel.textContent = 'Qty ';
  qtyInput = doc.createElement('input');
  qtyInput.type = 'number';
  qtyInput.className = 'tradex-field tradex-field--qty';
  qtyInput.value = '1';
  qtyInput.min = '1';
  qtyInput.title = 'Order quantity';
  qtyLabel.appendChild(qtyInput);

  productSelect = doc.createElement('select');
  productSelect.className = 'tradex-field';
  productSelect.title = 'Product type';
  for (const prod of ['MIS', 'CNC', 'NRML']) {
    const opt = doc.createElement('option');
    opt.value = prod;
    opt.textContent = prod;
    productSelect.appendChild(opt);
  }

  const armLabel = doc.createElement('label');
  armLabel.className = 'tradex-chk';
  armLabel.title = 'Arm trading: enable right-click placement and 1-click square off';
  armCheckbox = doc.createElement('input');
  armCheckbox.type = 'checkbox';
  const armText = doc.createElement('span');
  armText.textContent = 'Arm trading';
  armLabel.append(armCheckbox, armText);

  const modeChip = doc.createElement('span');
  modeChip.className = 'tradex-chip tradex-chip--analyze';
  modeChip.textContent = 'ANALYZE (sandbox)';

  const hint = doc.createElement('span');
  hint.className = 'tradex-hint';
  hint.textContent = 'right-click chart to order';

  subbar.append(restChip, wsStateChip, ticksChip, ltpChip, spacer, qtyLabel, productSelect, armLabel, modeChip, hint);

  // Wire symbol / interval / chart type listeners
  const commitSymbol = () => {
    const s = symInput.value.trim().toUpperCase();
    const ex = exSelect.value;
    if (!s) return;
    symInput.value = s;
    widget.setSymbol(s, ex);
    if (opts.onSymbolChange) opts.onSymbolChange(s, ex);
    if (statusSpan) statusSpan.textContent = `${s} · ${widget.interval()} · ${ex}`;
  };

  symInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') commitSymbol();
  });
  symInput.addEventListener('blur', commitSymbol);
  exSelect.addEventListener('change', commitSymbol);

  ctypeSelect.addEventListener('change', () => {
    const val = ctypeSelect.value;
    widget.setChartType(val);
    if (opts.onChartTypeChange) opts.onChartTypeChange(val);
  });

  // Keep topbar in sync with widget events
  widget.on('symbol', (ev) => {
    symInput.value = ev.symbol;
    if (statusSpan) statusSpan.textContent = `${ev.symbol} · ${widget.interval()} · ${exSelect.value}`;
  });
  widget.on('interval', (ev) => {
    setIntervalActive(ev.interval);
    if (statusSpan) statusSpan.textContent = `${symInput.value} · ${ev.interval} · ${exSelect.value}`;
  });

  parent.prepend(subbar);
  parent.prepend(topbar);
}

export function getOrderQty(): number {
  const n = Math.floor(Number(qtyInput?.value));
  return Number.isFinite(n) && n >= 1 ? n : 1;
}

export function getOrderProduct(): string {
  return productSelect?.value ?? 'MIS';
}

export function isTradingArmed(): boolean {
  return armCheckbox?.checked ?? false;
}

export function updateTelemetry(info: {
  ltp?: number | null;
  ticks?: number;
  status?: string;
  wsState?: 'live' | 'connecting' | 'idle' | 'closed';
}): void {
  if (info.ltp !== undefined && ltpChip !== null) {
    if (info.ltp !== null && Number.isFinite(info.ltp)) {
      ltpChip.textContent = `LTP ${Number(info.ltp).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    } else {
      ltpChip.textContent = 'LTP -';
    }
  }
  if (info.ticks !== undefined && ticksChip !== null) {
    tickCounter = info.ticks;
    ticksChip.textContent = `${tickCounter} ticks`;
  }
  if (info.wsState !== undefined && wsStateChip !== null) {
    if (info.wsState === 'live') {
      wsStateChip.textContent = '● WS live';
      wsStateChip.style.color = 'var(--acc)';
    } else if (info.wsState === 'connecting') {
      wsStateChip.textContent = '○ WS connecting';
      wsStateChip.style.color = 'var(--amber)';
    } else {
      wsStateChip.textContent = '× WS offline';
      wsStateChip.style.color = 'var(--sell)';
    }
  }
  if (info.status !== undefined && statusSpan !== null) {
    statusSpan.textContent = info.status;
  }
}

export function incrementTick(): void {
  tickCounter++;
  if (ticksChip) ticksChip.textContent = `${tickCounter} ticks`;
}
