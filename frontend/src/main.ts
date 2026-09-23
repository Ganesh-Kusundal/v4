import './theme.css';
import {
  registeredIndicators,
  type IndicatorDescriptor,
} from 'openalgo-charts';
import 'openalgo-charts/indicators';
import { authHeaders, getApiKey, setApiKey } from './apikey';
import { API_BASE, barSocket } from './feed';
import { mapInterval, mapWsInterval } from './chart-types';
import {
  chart, price, volume, ltpLine,
  rawBars, builder, liveAgg, lastLtp, ctxPrice, lastReq,
  isReplaying, replayPicking, replayPickIndex, replayShade, replayFullBars,
  replayTotalBars, replayCurrentIndex, replayIsPlaying, replaySpeed, REPLAY_SPEEDS,
  tickN, bookTimer, orderLines, posLine, position,
  unsubBar, unsubDepth, unsubQuote, unsubOrder, unsubFill, unsubPos, unsubOpen, unsubClose,
  setCtxPrice, setRawBars, setReplayCurrentIndex, setReplaySpeed, setIsReplaying, setReplayIsPlaying, setReplayTotalBars,
} from './chart-state';
import {
  chartType, buildChart, connect, disconnect,
  clearTrading, makeOrderLine, renderPosition, pollBook,
  cancelOrder, exitPosition, placeFromMenu,
  cancelPick, exitReplay, enterReplay, askExitReplay,
  syncReplayBarUI, startReplayAt,
  setPriceData, setLegend, setStatus,
  marketPrice, round2v,
} from './chart-lifecycle';

const el = <T extends HTMLElement = HTMLElement>(id: string): T => {
  const elem = document.getElementById(id);
  if (!elem) throw new Error(`Missing element #${id}`);
  return elem as T;
};

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
  setCtxPrice(round2v(p));
  const m = marketPrice();

  ctxMenu.querySelectorAll('button[data-type]').forEach((b) => {
    const side = b.getAttribute('data-side') || 'BUY';
    const v = side === 'BUY' ? 'Buy' : 'Sell';
    const t = b.getAttribute('data-type');
    const label =
      t === 'MARKET' ? `${v} Market` : t === 'LIMIT' ? `${v} Limit @ ${fmt(round2v(p))}` : `${v} Stop (SL) @ ${fmt(round2v(p))}`;
    const sp = b.querySelector('span');
    if (sp) sp.textContent = label;
    else b.textContent = label;
    let ok = true;
    if (m != null) {
      if (t === 'SL') ok = side === 'BUY' ? round2v(p) > m : round2v(p) < m;
      else if (t === 'LIMIT') ok = side === 'BUY' ? round2v(p) < m : round2v(p) > m;
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

// ── Symbol Autocomplete / Datalake Type-ahead ──
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
      e.preventDefault();
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

// ── Indicators & Studies Modal ──────────────────
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

// ── Chart Snapshot PNG ──────────────────────────
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

// ── Toolbar Select Changes ──────────────────────
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

// ── Market Replay ───────────────────────────────
el('chart').addEventListener('click', () => {
  if (replayPicking && replayPickIndex !== null) {
    startReplayAt(replayPickIndex);
  }
});

const replayBtn = el('replay-btn');
replayBtn.addEventListener('click', () => {
  if (isReplaying || replayPicking) {
    askExitReplay();
  } else {
    enterReplay();
  }
});

el('rp-pick-cancel')?.addEventListener('click', cancelPick);
el('rp-exit')?.addEventListener('click', askExitReplay);
el('rp-leave-stay')?.addEventListener('click', () => { el('replayleave').hidden = true; });
el('rp-leave-go')?.addEventListener('click', exitReplay);

el('rp-play')?.addEventListener('click', () => {
  if (!isReplaying) return;
  if (replayIsPlaying) {
    barSocket.send({ type: 'replay_pause' });
  } else {
    barSocket.send({ type: 'replay_resume' });
  }
});

el('rp-back')?.addEventListener('click', () => {
  if (!isReplaying || !replayFullBars || replayCurrentIndex <= 0) return;
  setReplayCurrentIndex(replayCurrentIndex - 1);
  setRawBars(replayFullBars.slice(0, replayCurrentIndex));
  setPriceData();
  syncReplayBarUI(replayCurrentIndex, replayTotalBars, rawBars[replayCurrentIndex] ?? null, false, replaySpeed);
  barSocket.send({ type: 'replay_pause' });
});

el('rp-fwd')?.addEventListener('click', () => {
  if (!isReplaying) return;
  barSocket.send({ type: 'replay_step' });
});

el('rp-scrub')?.addEventListener('input', () => {
  if (!isReplaying || !replayFullBars) return;
  const target = Math.max(0, Math.min(replayTotalBars - 1, Number((el('rp-scrub') as HTMLInputElement).value)));
  setReplayCurrentIndex(target);
  setRawBars(replayFullBars.slice(0, target + 1));
  setPriceData();
  syncReplayBarUI(target, replayTotalBars, rawBars[target] ?? null, false, replaySpeed);
  barSocket.send({ type: 'replay_pause' });
});

el('rp-speed')?.addEventListener('click', () => {
  const at = REPLAY_SPEEDS.indexOf(replaySpeed);
  const next = REPLAY_SPEEDS[(at + 1) % REPLAY_SPEEDS.length] ?? 1;
  setReplaySpeed(next);
  barSocket.send({ type: 'replay_speed', speed: next });
  syncReplayBarUI(replayCurrentIndex, replayTotalBars, rawBars[rawBars.length - 1] ?? null, replayIsPlaying, next);
});

window.addEventListener('keydown', (e: KeyboardEvent) => {
  if (e.key === 'Escape') {
    const leaveModal = document.getElementById('replayleave');
    if (leaveModal && !leaveModal.hidden) { leaveModal.hidden = true; return; }
    if (replayPicking) cancelPick();
  }
});

barSocket.on('replay_started', (msg: any) => {
  setIsReplaying(true);
  setReplayIsPlaying(true);
  if (msg?.total_bars) setReplayTotalBars(Number(msg.total_bars));
  syncReplayBarUI(replayCurrentIndex, replayTotalBars, rawBars[rawBars.length - 1] ?? null, true, replaySpeed);
  setStatus(`Replay active: ${msg?.instrument || ''}`, true);
});

barSocket.on('replay_paused', () => {
  setReplayIsPlaying(false);
  syncReplayBarUI(replayCurrentIndex, replayTotalBars, rawBars[rawBars.length - 1] ?? null, false, replaySpeed);
  setStatus('Replay paused');
});

barSocket.on('replay_resumed', () => {
  setReplayIsPlaying(true);
  syncReplayBarUI(replayCurrentIndex, replayTotalBars, rawBars[rawBars.length - 1] ?? null, true, replaySpeed);
  setStatus('Replay running', true);
});

barSocket.on('replay_stepped', () => {
  setReplayIsPlaying(false);
  setReplayCurrentIndex(rawBars.length - 1);
  syncReplayBarUI(replayCurrentIndex, replayTotalBars, rawBars[replayCurrentIndex] ?? null, false, replaySpeed);
  setStatus('Stepped 1 bar');
});

barSocket.on('replay_stopped', () => {
  exitReplay();
});

barSocket.on('replay_done', () => {
  setReplayIsPlaying(false);
  syncReplayBarUI(replayTotalBars - 1, replayTotalBars, rawBars[rawBars.length - 1] ?? null, false, replaySpeed);
  setStatus('Replay completed');
});

// Initial connection
connect();

// ── Utility ──────────────────────────────────────
const fmt = (n: number | null | undefined): string =>
  n == null || isNaN(n)
    ? '-'
    : Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const esc = (s: string): string =>
  String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] || c);
