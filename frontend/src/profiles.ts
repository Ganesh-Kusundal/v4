// Profile tier: volume-profile / TPO / market-profile / footprint cells are
// computed by tradex_trading (POST /api/charts/profiles/{id}); this module
// draws what the backend returns. No profile math in JS.
//
// Renderers mirror the reference openalgo-charts-master profile primitives for
// geometry — volume-profile-primitive.ts, market-profile-primitive.ts (also the
// TPO letter grid) and footprint-primitive.ts — but every computation is
// replaced by a backend POST. Scope is the default-options path only.
import {
  compactVolume,
  type Bar,
  type IPrimitive,
  type PrimitiveHost,
  type PrimitiveRenderContext,
  type ZOrder,
} from "openalgo-charts";
import { expectJson } from "./http";

export interface ProfilePayload {
  id: string;
  result: unknown;
}

/** A footprint trade (the backend's compute_footprint consumes trades, not bars). */
export interface FootprintTrade {
  price: number;
  qty: number;
  side: "bid" | "ask";
}

export async function computeProfile(
  id: string,
  params: Record<string, unknown>,
  bars: Bar[] | readonly FootprintTrade[],
): Promise<unknown> {
  const resp = await fetch(`/api/charts/profiles/${encodeURIComponent(id)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, params, bars }),
  });
  const body = await expectJson<ProfilePayload>(resp);
  return body.result;
}

// ---------------------------------------------------------------------------
// Backend result shapes (mirror tradex_trading.analytics.profiles)
// ---------------------------------------------------------------------------

export interface VolumeBucket {
  price: number;
  volume: number;
}
export interface VolumeProfileResult {
  buckets: VolumeBucket[];
  poc: number;
  vah: number;
  val: number;
  totalVolume: number;
}

export interface TpoBucket {
  price: number;
  count: number;
}
export interface TpoResult {
  buckets: TpoBucket[];
  poc: number;
  vah: number;
  val: number;
  ib: { high: number; low: number };
}

export interface MarketProfileLevel {
  price: number;
  count: number;
  periods: number[];
  volume: number;
  letters: string;
}
export interface MarketProfilePeriod {
  index: number;
  letter: string;
  startTime: number;
  endTime: number;
  high: number;
  low: number;
  volume: number;
}
export interface MarketProfileSession {
  startTime: number;
  endTime: number;
  levels: MarketProfileLevel[];
  poc: number;
  vah: number;
  val: number;
  high: number;
  low: number;
  open: number;
  close: number;
  periods: number;
  periodDetail: MarketProfilePeriod[];
  initialBalance: { high: number; low: number };
  rangeExtension: { up: number; down: number };
  singlePrints: number[];
  buyingTail: { high: number; low: number } | null;
  sellingTail: { high: number; low: number } | null;
  poorHigh: boolean;
  poorLow: boolean;
  developing: { periodIndex: number; time: number; poc: number; vah: number; val: number }[];
  dayType: string;
  openType: string;
  volumePoc: number;
  totalVolume: number;
  label?: string;
}
export interface MarketProfileResult {
  sessions: MarketProfileSession[];
  options: {
    tickSize: number;
    rowTicks: number;
    session: string;
    blockMinutes: number;
    valueAreaPercent: number;
    initialBalancePeriods: number;
    compositeSessions: number;
    tailEdges: number;
    timezone: string;
  };
}

export interface FootprintCell {
  price: number;
  bidVol: number;
  askVol: number;
}
export interface FootprintResult {
  time: number;
  cells: FootprintCell[];
  delta: number;
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

/** Smallest positive gap between adjacent price rows (infers the tick). */
function priceTick(prices: readonly number[]): number {
  let tick = 0;
  for (let i = 1; i < prices.length; i++) {
    const gap = Math.abs(prices[i - 1] - prices[i]);
    if (gap > 0 && (tick === 0 || gap < tick)) tick = gap;
  }
  return tick;
}

/**
 * Footprint consumes classified trades (the backend's compute_footprint), so
 * the window's OHLCV bars are shaped into deterministic bid/ask prints — the
 * same rule the golden generator uses: one ask print at high for volume/2, one
 * bid print at low for volume/2. This is data shaping, not profile math.
 */
export function barsToTrades(bars: readonly Bar[]): FootprintTrade[] {
  const trades: FootprintTrade[] = [];
  for (const b of bars) {
    const half = (b.volume ?? 0) / 2;
    trades.push({ price: b.high, qty: half, side: "ask" });
    trades.push({ price: b.low, qty: half, side: "bid" });
  }
  return trades;
}

/** A mounted renderer: the primitive plus a uniform way to feed it data. */
export interface ProfileRenderer {
  primitive: IPrimitive;
  setData(result: unknown): void;
}

export interface ProfileSpec {
  id: string;
  name: string;
  create(): ProfileRenderer;
}

// ---------------------------------------------------------------------------
// Volume Profile — reference: volume-profile-primitive.ts (total mode, single
// profile over the visible window, right-anchored histogram)
// ---------------------------------------------------------------------------

export interface VolumeProfileOptions {
  side: "left" | "right";
  width: number;
  opacity: number;
  barColor: string;
  pocColor: string;
  vahColor: string;
  valColor: string;
  highlightValueArea: boolean;
  valueAreaFillColor: string;
  valueAreaFillOpacity: number;
  valueAreaOpacityDim: number;
  zOrder: ZOrder;
}

export const DEFAULT_VOLUME_PROFILE_OPTIONS: VolumeProfileOptions = {
  side: "right",
  width: 90,
  opacity: 0.85,
  barColor: "#3b5168",
  pocColor: "#f0a020",
  vahColor: "#8892a6",
  valColor: "#8892a6",
  highlightValueArea: true,
  valueAreaFillColor: "#4a6fa5",
  valueAreaFillOpacity: 0.08,
  valueAreaOpacityDim: 0.4,
  zOrder: "bottom",
};

export class VolumeProfilePrimitive implements IPrimitive {
  private _result: VolumeProfileResult | null = null;
  private readonly _opts: VolumeProfileOptions;
  private _host: PrimitiveHost | null = null;

  public constructor(opts: Partial<VolumeProfileOptions> = {}) {
    this._opts = { ...DEFAULT_VOLUME_PROFILE_OPTIONS, ...opts };
  }

  public attached(host: PrimitiveHost): void { this._host = host; }
  public detached(): void { this._host = null; }
  public zOrder(): ZOrder { return this._opts.zOrder; }

  public autoscaleInfo(): { min: number; max: number } | null {
    const r = this._result;
    if (r === null || r.buckets.length === 0) return null;
    return { min: r.buckets[r.buckets.length - 1].price, max: r.buckets[0].price };
  }

  public setData(result: VolumeProfileResult): void {
    this._result = result;
    this._host?.requestUpdate();
  }

  public draw(ctx: CanvasRenderingContext2D, rc: PrimitiveRenderContext): void {
    const o = this._opts;
    const r = this._result;
    if (r === null || r.buckets.length === 0) return;
    const dpr = rc.dpr;
    const yOf = (p: number): number => rc.priceScale.priceToY(p) * dpr;
    const tick = priceTick(r.buckets.map((b) => b.price));
    if (tick <= 0) return;
    const rowH = Math.max(1, Math.abs(rc.priceScale.priceToY(r.poc) - rc.priceScale.priceToY(r.poc + tick)) * dpr);
    const widthDev = o.width * dpr;
    const x1 = rc.plotWidth * dpr;
    const anchor = o.side === "right" ? x1 : 0;

    let maxVol = 1;
    for (const l of r.buckets) maxVol = Math.max(maxVol, l.volume);

    ctx.save();

    // Value-area highlight fill (the reference draws it behind the bars).
    if (o.highlightValueArea) {
      const yTop = yOf(r.vah) - rowH / 2;
      const yBot = yOf(r.val) + rowH / 2;
      const fx = o.side === "right" ? x1 - widthDev : 0;
      ctx.globalAlpha = o.valueAreaFillOpacity;
      ctx.fillStyle = o.valueAreaFillColor;
      ctx.fillRect(fx, yTop, widthDev, yBot - yTop);
      ctx.globalAlpha = 1;
    }

    // Histogram bars, right-anchored (side: right).
    for (const l of r.buckets) {
      const inVa = l.price <= r.vah && l.price >= r.val;
      const alpha = o.opacity * (inVa ? 1 : o.valueAreaOpacityDim);
      const len = (l.volume / maxVol) * widthDev;
      if (len <= 0) continue;
      const y = yOf(l.price);
      const top = y - rowH / 2;
      const h = Math.max(1, rowH - 1);
      ctx.globalAlpha = alpha;
      ctx.fillStyle = o.barColor;
      ctx.fillRect(o.side === "right" ? anchor - len : anchor, top, len, h);
    }
    ctx.globalAlpha = 1;

    // POC / VAH / VAL lines + labels across the whole pane.
    const hline = (price: number, color: string, label: string): void => {
      const y = Math.round(yOf(price)) + 0.5;
      ctx.strokeStyle = color;
      ctx.lineWidth = Math.max(1, Math.round(dpr));
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(x1, y);
      ctx.stroke();
      if (label !== "") {
        ctx.font = `${9 * dpr}px ui-sans-serif, system-ui, sans-serif`;
        ctx.textBaseline = "middle";
        ctx.fillStyle = color;
        ctx.textAlign = "right";
        ctx.fillText(label, x1 - 3 * dpr, y);
      }
    };
    hline(r.vah, o.vahColor, `VAH ${rc.priceScale.format(r.vah)}`);
    hline(r.val, o.valColor, `VAL ${rc.priceScale.format(r.val)}`);
    hline(r.poc, o.pocColor, `POC ${rc.priceScale.format(r.poc)}`);

    ctx.restore();
  }
}

export function createVolumeProfilePrimitive(): ProfileRenderer {
  const primitive = new VolumeProfilePrimitive();
  return {
    primitive,
    setData: (result: unknown) => primitive.setData(result as VolumeProfileResult),
  };
}

// ---------------------------------------------------------------------------
// TPO — reference: market-profile-primitive.ts (the TPO letter grid), one
// column of letter cells per bucket anchored at the right edge
// ---------------------------------------------------------------------------

export interface TpoOptions {
  letterWidth: number;
  font: number;
  color: string;
  opacity: number;
  outsideVaOpacity: number;
  zOrder: ZOrder;
  showPoc: boolean;
  pocColor: string;
  showPocLabel: boolean;
  showValueArea: boolean;
  vahColor: string;
  valColor: string;
  showValueAreaLabels: boolean;
  fillValueArea: boolean;
  valueAreaFillColor: string;
  valueAreaFillOpacity: number;
  showInitialBalance: boolean;
  ibColor: string;
}

export const DEFAULT_TPO_OPTIONS: TpoOptions = {
  letterWidth: 8,
  font: 10,
  color: "#5a6b8c",
  opacity: 0.92,
  outsideVaOpacity: 0.45,
  zOrder: "top",
  showPoc: true,
  pocColor: "#f0a020",
  showPocLabel: true,
  showValueArea: true,
  vahColor: "#8892a6",
  valColor: "#8892a6",
  showValueAreaLabels: true,
  fillValueArea: true,
  valueAreaFillColor: "#4a8f7a",
  valueAreaFillOpacity: 0.07,
  showInitialBalance: true,
  ibColor: "#c8853a",
};

export class TpoPrimitive implements IPrimitive {
  private _result: TpoResult | null = null;
  private readonly _opts: TpoOptions;
  private _host: PrimitiveHost | null = null;

  public constructor(opts: Partial<TpoOptions> = {}) {
    this._opts = { ...DEFAULT_TPO_OPTIONS, ...opts };
  }

  public attached(host: PrimitiveHost): void { this._host = host; }
  public detached(): void { this._host = null; }
  public zOrder(): ZOrder { return this._opts.zOrder; }

  public autoscaleInfo(): { min: number; max: number } | null {
    const r = this._result;
    if (r === null || r.buckets.length === 0) return null;
    return { min: r.buckets[r.buckets.length - 1].price, max: r.buckets[0].price };
  }

  public setData(result: TpoResult): void {
    this._result = result;
    this._host?.requestUpdate();
  }

  public draw(ctx: CanvasRenderingContext2D, rc: PrimitiveRenderContext): void {
    const o = this._opts;
    const r = this._result;
    if (r === null || r.buckets.length === 0) return;
    const dpr = rc.dpr;
    const yOf = (p: number): number => rc.priceScale.priceToY(p) * dpr;
    const tick = priceTick(r.buckets.map((b) => b.price));
    if (tick <= 0) return;
    const rowH = Math.max(1, Math.abs(rc.priceScale.priceToY(r.poc) - rc.priceScale.priceToY(r.poc + tick)) * dpr);
    const lw = o.letterWidth * dpr;
    const blockH = Math.max(1, rowH - Math.min(1.5 * dpr, rowH * 0.15));
    const x1 = rc.plotWidth * dpr;
    const fontPx = Math.min(o.font * dpr, rowH * 0.95);

    ctx.save();

    // Value-area highlight fill behind the letter grid.
    if (o.fillValueArea) {
      const yTop = yOf(r.vah) - rowH / 2;
      const yBot = yOf(r.val) + rowH / 2;
      ctx.globalAlpha = o.valueAreaFillOpacity;
      ctx.fillStyle = o.valueAreaFillColor;
      ctx.fillRect(0, yTop, x1, yBot - yTop);
      ctx.globalAlpha = 1;
    }

    // Letter cells: count → that many letter columns, packed left from the right edge.
    ctx.font = `${fontPx}px ui-monospace, SFMono-Regular, Menlo, monospace`;
    ctx.textBaseline = "middle";
    ctx.textAlign = "center";
    for (const l of r.buckets) {
      const y = yOf(l.price);
      const inVa = l.price <= r.vah && l.price >= r.val;
      const alpha = o.opacity * (inVa ? 1 : o.outsideVaOpacity);
      for (let j = 0; j < l.count; j++) {
        const bx = x1 - (j + 1) * lw;
        if (bx < 0) break;
        ctx.globalAlpha = alpha;
        ctx.fillStyle = o.color;
        ctx.fillRect(bx, y - blockH / 2, Math.max(1, lw - Math.min(1, lw * 0.12)), blockH);
        ctx.fillStyle = "#e6ecf5";
        ctx.fillText(String.fromCharCode(65 + (j % 26)), bx + lw / 2, y);
      }
    }
    ctx.globalAlpha = 1;

    // Initial-balance box, bracketed at the left edge.
    if (o.showInitialBalance && Number.isFinite(r.ib.high)) {
      const yH = yOf(r.ib.high);
      const yL = yOf(r.ib.low);
      const xb = 2 * dpr;
      ctx.strokeStyle = o.ibColor;
      ctx.lineWidth = Math.max(1, Math.round(1.5 * dpr));
      ctx.beginPath();
      ctx.moveTo(xb, yH); ctx.lineTo(xb, yL);
      ctx.moveTo(xb, yH); ctx.lineTo(xb + 5 * dpr, yH);
      ctx.moveTo(xb, yL); ctx.lineTo(xb + 5 * dpr, yL);
      ctx.stroke();
    }

    // POC / VAH / VAL lines + labels across the whole pane.
    const hline = (price: number, color: string, label: string): void => {
      const y = Math.round(yOf(price)) + 0.5;
      ctx.strokeStyle = color;
      ctx.lineWidth = Math.max(1, Math.round(dpr));
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(x1, y);
      ctx.stroke();
      if (label !== "") {
        ctx.font = `${(o.font - 1) * dpr}px ui-sans-serif, system-ui, sans-serif`;
        ctx.textBaseline = "middle";
        ctx.fillStyle = color;
        ctx.textAlign = "right";
        ctx.fillText(label, x1 - 3 * dpr, y);
      }
    };
    if (o.showValueArea) {
      hline(r.vah, o.vahColor, o.showValueAreaLabels ? `VAH ${rc.priceScale.format(r.vah)}` : "");
      hline(r.val, o.valColor, o.showValueAreaLabels ? `VAL ${rc.priceScale.format(r.val)}` : "");
    }
    if (o.showPoc) hline(r.poc, o.pocColor, o.showPocLabel ? `POC ${rc.priceScale.format(r.poc)}` : "");

    ctx.restore();
  }
}

export function createTpoPrimitive(): ProfileRenderer {
  const primitive = new TpoPrimitive();
  return {
    primitive,
    setData: (result: unknown) => primitive.setData(result as TpoResult),
  };
}

// ---------------------------------------------------------------------------
// Market Profile — reference: market-profile-primitive.ts (per-session TPO
// columns, POC/VA, initial balance, single prints). Backend result maps
// field-for-field onto the reference's MarketProfileResult, so the drawing
// path is a near-verbatim port of _drawSession (default options).
// ---------------------------------------------------------------------------

export const TPO_PERIOD_COLORS: readonly string[] = [
  "#e05555", "#e08a3c", "#d9c341", "#8cc44a", "#3fb96b", "#38b2a3",
  "#3b9fd1", "#5a7fe0", "#8a68d9", "#c05fc4", "#d1508f", "#9b7b5a",
];

export interface MarketProfileOptions {
  minLetterHeight: number;
  letterFade: number;
  letterWidth: number;
  font: number;
  colorMode: "period" | "valueArea" | "uniform";
  periodColors: readonly string[];
  color: string;
  vaColor: string;
  opacity: number;
  outsideVaOpacity: number;
  zOrder: ZOrder;
  split: boolean;
  profileSpacing: number;
  showPoc: boolean;
  pocColor: string;
  pocThickness: number;
  showPocLabel: boolean;
  showValueArea: boolean;
  vahColor: string;
  valColor: string;
  showValueAreaLabels: boolean;
  fillValueArea: boolean;
  valueAreaFillColor: string;
  valueAreaFillOpacity: number;
  showInitialBalance: boolean;
  ibColor: string;
  showSinglePrints: boolean;
  singlePrintColor: string;
  showSessionLabel: boolean;
  labelColor: string;
}

export const DEFAULT_MARKET_PROFILE_OPTIONS: MarketProfileOptions = {
  minLetterHeight: 7,
  letterFade: 4,
  letterWidth: 8,
  font: 10,
  colorMode: "period",
  periodColors: TPO_PERIOD_COLORS,
  color: "#5a6b8c",
  vaColor: "#4a8f7a",
  opacity: 0.92,
  outsideVaOpacity: 0.45,
  zOrder: "top",
  split: false,
  profileSpacing: 4,
  showPoc: true,
  pocColor: "#f0a020",
  pocThickness: 2,
  showPocLabel: true,
  showValueArea: true,
  vahColor: "#8892a6",
  valColor: "#8892a6",
  showValueAreaLabels: true,
  fillValueArea: true,
  valueAreaFillColor: "#4a8f7a",
  valueAreaFillOpacity: 0.07,
  showInitialBalance: true,
  ibColor: "#c8853a",
  showSinglePrints: true,
  singlePrintColor: "#e0556b",
  showSessionLabel: true,
  labelColor: "#cccccc",
};

export class MarketProfilePrimitive implements IPrimitive {
  private _result: MarketProfileResult | null = null;
  private readonly _opts: MarketProfileOptions;
  private _host: PrimitiveHost | null = null;

  public constructor(opts: Partial<MarketProfileOptions> = {}) {
    this._opts = { ...DEFAULT_MARKET_PROFILE_OPTIONS, ...opts };
  }

  public attached(host: PrimitiveHost): void { this._host = host; }
  public detached(): void { this._host = null; }
  public zOrder(): ZOrder { return this._opts.zOrder; }

  public autoscaleInfo(): { min: number; max: number } | null {
    const r = this._result;
    if (r === null) return null;
    let min = Infinity;
    let max = -Infinity;
    for (const s of r.sessions) {
      if (s.levels.length === 0) continue;
      max = Math.max(max, s.levels[0].price);
      min = Math.min(min, s.levels[s.levels.length - 1].price);
    }
    return Number.isFinite(min) ? { min, max } : null;
  }

  public setData(result: MarketProfileResult): void {
    this._result = result;
    this._host?.requestUpdate();
  }

  /** How opaque the letter is at this row height: 0 below the threshold, 1 well above. */
  private _letterAlpha(rowHpx: number): number {
    const o = this._opts;
    const fade = Math.max(1, o.letterFade);
    return Math.max(0, Math.min(1, (rowHpx - o.minLetterHeight) / fade));
  }

  private _blockColor(l: MarketProfileLevel, periodIdx: number, s: MarketProfileSession): string {
    const o = this._opts;
    if (o.colorMode === "period") return o.periodColors[periodIdx % o.periodColors.length];
    if (o.colorMode === "valueArea") return l.price <= s.vah && l.price >= s.val ? o.vaColor : o.color;
    return o.color;
  }

  public draw(ctx: CanvasRenderingContext2D, rc: PrimitiveRenderContext): void {
    const r = this._result;
    if (r === null || r.sessions.length === 0) return;
    const row = r.options.tickSize * Math.max(1, Math.floor(r.options.rowTicks));
    for (const s of r.sessions) this._drawSession(ctx, rc, s, row);
  }

  private _drawSession(
    ctx: CanvasRenderingContext2D,
    rc: PrimitiveRenderContext,
    s: MarketProfileSession,
    row: number,
  ): void {
    const o = this._opts;
    const dpr = rc.dpr;
    const i0 = rc.dataLayer.timeToIndex(s.startTime);
    const i1 = rc.dataLayer.timeToIndex(s.endTime);
    if (i0 === undefined || i1 === undefined) return;
    const spacing = o.profileSpacing * dpr;
    const x0 = Math.round(rc.timeScale.indexToX(i0) * dpr) + spacing / 2;
    const x1 = Math.max(x0 + 1, Math.round(rc.timeScale.indexToX(i1) * dpr) - spacing / 2);

    const yOf = (p: number): number => rc.priceScale.priceToY(p) * dpr;
    const rowH = Math.max(1, Math.abs(rc.priceScale.priceToY(s.poc) - rc.priceScale.priceToY(s.poc + row)) * dpr);
    const lw = o.letterWidth * dpr;
    // 'auto' block display: the block is always drawn; the letter fades in over
    // `letterFade` px above `minLetterHeight`.
    const alpha = this._letterAlpha(rowH / dpr);
    const fontPx = Math.min(o.font * dpr, rowH * 0.95);
    const blockH = Math.max(1, rowH - Math.min(1.5 * dpr, rowH * 0.15));

    ctx.save();

    if (o.fillValueArea) {
      const yTop = yOf(s.vah) - rowH / 2;
      const yBot = yOf(s.val) + rowH / 2;
      ctx.globalAlpha = o.valueAreaFillOpacity;
      ctx.fillStyle = o.valueAreaFillColor;
      ctx.fillRect(x0, yTop, x1 - x0, yBot - yTop);
      ctx.globalAlpha = 1;
    }

    // ── TPO blocks + letters ──────────────────────────────────────────────
    if (alpha > 0) {
      ctx.font = `${fontPx}px ui-monospace, SFMono-Regular, Menlo, monospace`;
      ctx.textBaseline = "middle";
      ctx.textAlign = "center";
    }
    for (const l of s.levels) {
      const y = yOf(l.price);
      const inVa = l.price <= s.vah && l.price >= s.val;
      const baseAlpha = o.opacity * (inVa ? 1 : o.outsideVaOpacity);
      for (let j = 0; j < l.periods.length; j++) {
        // `split` gives each period its own column slot; packed mode closes gaps.
        const slot = o.split ? l.periods[j] : j;
        const bx = x0 + slot * lw;
        if (bx > x1) break;
        const color = this._blockColor(l, l.periods[j], s);
        ctx.globalAlpha = baseAlpha;
        ctx.fillStyle = color;
        ctx.fillRect(bx, y - blockH / 2, Math.max(1, lw - Math.min(1, lw * 0.12)), blockH);
        if (alpha > 0) {
          ctx.globalAlpha = alpha * 0.95;
          ctx.fillStyle = "#12151c";
          ctx.fillText(l.letters[j] ?? "", bx + lw / 2, y);
        }
      }
    }
    ctx.globalAlpha = 1;

    if (o.showSinglePrints && s.singlePrints.length > 0) {
      ctx.strokeStyle = o.singlePrintColor;
      ctx.lineWidth = Math.max(1, Math.round(dpr));
      for (const price of s.singlePrints) {
        const y = Math.round(yOf(price)) + 0.5;
        ctx.beginPath();
        ctx.moveTo(x0, y);
        ctx.lineTo(x0 + 6 * dpr, y);
        ctx.stroke();
      }
    }

    if (o.showInitialBalance && Number.isFinite(s.initialBalance.high)) {
      const yH = yOf(s.initialBalance.high);
      const yL = yOf(s.initialBalance.low);
      const xb = x0 - 2 * dpr;
      ctx.strokeStyle = o.ibColor;
      ctx.lineWidth = Math.max(1, Math.round(1.5 * dpr));
      ctx.beginPath();
      ctx.moveTo(xb, yH); ctx.lineTo(xb, yL);
      ctx.moveTo(xb, yH); ctx.lineTo(xb + 5 * dpr, yH);
      ctx.moveTo(xb, yL); ctx.lineTo(xb + 5 * dpr, yL);
      ctx.stroke();
    }

    const hline = (price: number, color: string, label: string, width = 1): void => {
      const y = Math.round(yOf(price)) + 0.5;
      ctx.strokeStyle = color;
      ctx.lineWidth = Math.max(1, Math.round(width * dpr));
      ctx.beginPath();
      ctx.moveTo(x0, y);
      ctx.lineTo(x1, y);
      ctx.stroke();
      if (label !== "") {
        ctx.font = `${(o.font - 1) * dpr}px ui-sans-serif, system-ui, sans-serif`;
        ctx.textBaseline = "middle";
        ctx.textAlign = "left";
        ctx.fillStyle = color;
        ctx.fillText(label, x1 + 3 * dpr, y);
      }
    };
    if (o.showValueArea) {
      hline(s.vah, o.vahColor, o.showValueAreaLabels ? `VAH ${rc.priceScale.format(s.vah)}` : "");
      hline(s.val, o.valColor, o.showValueAreaLabels ? `VAL ${rc.priceScale.format(s.val)}` : "");
    }
    if (o.showPoc) hline(s.poc, o.pocColor, o.showPocLabel ? `POC ${rc.priceScale.format(s.poc)}` : "", o.pocThickness);

    // Session header (only when the backend tagged the session with a label).
    if (o.showSessionLabel && s.label !== undefined) {
      ctx.font = `${(o.font - 1) * dpr}px ui-sans-serif, system-ui, sans-serif`;
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      ctx.fillStyle = o.labelColor;
      ctx.fillText(s.label, x0, yOf(s.high) - rowH);
    }

    ctx.restore();
  }
}

export function createMarketProfilePrimitive(): ProfileRenderer {
  const primitive = new MarketProfilePrimitive();
  return {
    primitive,
    setData: (result: unknown) => primitive.setData(result as MarketProfileResult),
  };
}

// ---------------------------------------------------------------------------
// Footprint — reference: footprint-primitive.ts. The backend returns ONE bar's
// cells (the whole visible window aggregated), so a single column is drawn:
// bid/ask cells at each price plus a compact volume/delta strip.
// ---------------------------------------------------------------------------

export interface FootprintOptions {
  widthFactor: number;
  font: number;
  minTextHeight: number;
  textFade: number;
  buyColor?: string;
  sellColor?: string;
  pocColor: string;
  showPoc: boolean;
  showCandle: boolean;
  zOrder: ZOrder;
}

export const DEFAULT_FOOTPRINT_OPTIONS: FootprintOptions = {
  widthFactor: 0.9,
  font: 10,
  minTextHeight: 11,
  textFade: 4,
  pocColor: "#f0a020",
  showPoc: true,
  showCandle: true,
  zOrder: "normal",
};

export class FootprintPrimitive implements IPrimitive {
  private _result: FootprintResult | null = null;
  private readonly _opts: FootprintOptions;
  private _host: PrimitiveHost | null = null;

  public constructor(opts: Partial<FootprintOptions> = {}) {
    this._opts = { ...DEFAULT_FOOTPRINT_OPTIONS, ...opts };
  }

  public attached(host: PrimitiveHost): void { this._host = host; }
  public detached(): void { this._host = null; }
  public zOrder(): ZOrder { return this._opts.zOrder; }

  public autoscaleInfo(): { min: number; max: number } | null {
    const r = this._result;
    if (r === null || r.cells.length === 0) return null;
    return { min: r.cells[r.cells.length - 1].price, max: r.cells[0].price };
  }

  public setData(result: FootprintResult): void {
    this._result = result;
    this._host?.requestUpdate();
  }

  public draw(ctx: CanvasRenderingContext2D, rc: PrimitiveRenderContext): void {
    const o = this._opts;
    const r = this._result;
    if (r === null || r.cells.length === 0) return;
    const dpr = rc.dpr;
    const buy = o.buyColor ?? rc.theme.upColor;
    const sell = o.sellColor ?? rc.theme.downColor;
    const tick = priceTick(r.cells.map((c) => c.price));
    const p0 = r.cells[0].price;
    const rowH = tick > 0
      ? Math.max(Math.abs(rc.priceScale.priceToY(p0) - rc.priceScale.priceToY(p0 + tick)) * dpr, 6 * dpr)
      : 16 * dpr;
    const width = Math.max(48 * dpr, rc.timeScale.barSpacing * o.widthFactor * dpr);
    const half = width / 2;
    const index = rc.dataLayer.timeToIndex(r.time);
    const x = index === undefined
      ? Math.round(rc.plotWidth * dpr - half)
      : Math.round(rc.timeScale.indexToX(index) * dpr);
    const x0 = x - half;
    const plotH = rc.plotHeight * dpr;
    const statsH = 2 * 16 * dpr;
    const cellBottom = plotH - statsH;

    let peak = 1;
    let pocPrice = r.cells[0].price;
    let maxCell = -1;
    for (const c of r.cells) {
      peak = Math.max(peak, c.bidVol, c.askVol);
      const total = c.bidVol + c.askVol;
      if (total > maxCell) { maxCell = total; pocPrice = c.price; }
    }

    // Fade numbers in as rows get tall enough to hold them.
    const textAlpha = Math.max(0, Math.min(1, (rowH / dpr - o.minTextHeight) / Math.max(1, o.textFade) + 1));

    ctx.save();
    ctx.textBaseline = "middle";
    if (textAlpha > 0) {
      ctx.font = `${o.font * dpr}px ui-monospace, SFMono-Regular, Menlo, monospace`;
      ctx.textAlign = "center";
    }

    // Range line + body behind the cells: the bar is still a bar.
    if (o.showCandle) {
      const yHi = rc.priceScale.priceToY(r.cells[0].price) * dpr - rowH / 2;
      const yLo = rc.priceScale.priceToY(r.cells[r.cells.length - 1].price) * dpr + rowH / 2;
      ctx.globalAlpha = 0.5;
      ctx.fillStyle = r.delta >= 0 ? buy : sell;
      ctx.fillRect(x0 - 5 * dpr, yHi, 3 * dpr, yLo - yHi);
      ctx.globalAlpha = 1;
    }

    for (const c of r.cells) {
      const y = rc.priceScale.priceToY(c.price) * dpr;
      const top = Math.round(y - rowH / 2);
      const h = Math.max(1, Math.round(rowH) - 1);
      if (top + h < 0 || top > cellBottom) continue;
      this._cell(ctx, x0, top, half - dpr, h, c.bidVol, peak, sell, textAlpha, dpr);
      this._cell(ctx, x + dpr, top, half - dpr, h, c.askVol, peak, buy, textAlpha, dpr);
      if (o.showPoc && c.price === pocPrice) {
        ctx.fillStyle = o.pocColor;
        ctx.fillRect(x0 - 2 * dpr, top, 2 * dpr, h);
      }
    }

    // Compact stats strip: volume + delta for the aggregated bar.
    let volume = 0;
    for (const c of r.cells) volume += c.bidVol + c.askVol;
    ctx.globalAlpha = 0.92;
    ctx.fillStyle = rc.theme.background;
    ctx.fillRect(0, cellBottom, rc.plotWidth * dpr, statsH);
    ctx.globalAlpha = 1;
    ctx.font = `${(o.font - 0.5) * dpr}px ui-monospace, SFMono-Regular, Menlo, monospace`;
    ctx.fillStyle = "rgba(255,255,255,0.92)";
    ctx.fillText(`Vol ${compactVolume(volume)}`, x, cellBottom + 16 * dpr / 2);
    ctx.fillText(`Δ ${r.delta >= 0 ? "+" : ""}${compactVolume(r.delta)}`, x, cellBottom + 16 * dpr + 16 * dpr / 2);

    ctx.restore();
  }

  /** One filled, intensity-graded cell with its number (heat = share of peak). */
  private _cell(
    ctx: CanvasRenderingContext2D,
    x: number,
    y: number,
    w: number,
    h: number,
    value: number,
    peak: number,
    color: string,
    textAlpha: number,
    dpr: number,
  ): void {
    if (w <= 0) return;
    const t = peak > 0 ? value / peak : 0;
    ctx.globalAlpha = 0.08 + 0.62 * Math.sqrt(t);
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.roundRect(x, y, w, h, Math.min(2 * dpr, h / 2, w / 2));
    ctx.fill();
    ctx.globalAlpha = 1;
    if (textAlpha <= 0) return;
    ctx.fillStyle = `rgba(255,255,255,${0.9 * textAlpha})`;
    ctx.fillText(compactVolume(value), x + w / 2, y + h / 2);
  }
}

export function createFootprintPrimitive(): ProfileRenderer {
  const primitive = new FootprintPrimitive();
  return {
    primitive,
    setData: (result: unknown) => primitive.setData(result as FootprintResult),
  };
}

// ---------------------------------------------------------------------------
// Catalogue
// ---------------------------------------------------------------------------

export const PROFILES: ProfileSpec[] = [
  { id: "volume-profile", name: "Volume Profile", create: createVolumeProfilePrimitive },
  { id: "tpo", name: "TPO", create: createTpoPrimitive },
  { id: "market-profile", name: "Market Profile", create: createMarketProfilePrimitive },
  { id: "footprint", name: "Footprint", create: createFootprintPrimitive },
];