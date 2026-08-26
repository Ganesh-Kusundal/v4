// Chrome primitives: watermark, series markers, price levels, pane legend,
// event markers, buy-sell buttons, and chart table. Pure chrome — the library
// draws each primitive, the shell feeds symbol/price context. No backend.
//
// Decisions (see the P4 task-3 report):
// - BuySellButtons clicks are NOT routed through chart.subscribeClick: that is
//   a single slot already owned by the TradingController (P3 task-2 caveat —
//   subscribeClick overwrites the chart's one _clickCb). They ride the chart's
//   unified multi-listener 'click' bus instead, which fires with the same
//   external id (`chrome:buy` / `chrome:sell`) on every clean click.
// - PriceLevels self-computes values from the bars in view every frame (draw →
//   computePriceLevels), so the `levels`/setLevel options only control the
//   line/label style. setContext switches the opt-in sessionHigh/sessionLow on
//   and feeds the rest of the chrome (buy-sell mark price, legend change %).
// - ChartTable is NOT mounted: an empty table draws nothing (draw early-returns
//   on zero rows), and seasonality already owns the table surface via its own
//   ChartTable inside SeasonalityPrimitive (seasonality.ts). Double-mounting a
//   second no-op table adds nothing.
import {
  LogoWatermark,
  PriceLevels,
  PaneLegend,
  EventMarkers,
  BuySellButtons,
  type IPrimitive,
  type SeriesApi,
} from "openalgo-charts";

export interface ChromeContext {
  symbol: string;
  exchange: string;
  lastPrice: number;
  prevClose: number;
  sessionHigh: number;
  sessionLow: number;
}

/** Order-placement hook; the shell routes BuySellButtons clicks here. */
export type ChromeOrderAction = (side: "BUY" | "SELL") => void;

/** The slice of the Chart instance Chrome touches (the Chart implements all three). */
export interface ChromeHost {
  on(event: string, cb: (payload: unknown) => void): () => void;
  addPrimitive(p: IPrimitive, where?: number): void;
  removePrimitive(p: IPrimitive): void;
}

// The library's LogoWatermark has no text-only mode — it needs an image. A tiny
// inline SVG glyph keeps the shell logo-free while giving the mark something to
// draw; the label unrolls on hover.
const TRADEX_MARK =
  "data:image/svg+xml," +
  encodeURIComponent(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>" +
      "<text x='32' y='46' font-family='system-ui, sans-serif' font-size='42' font-weight='800' text-anchor='middle'>T</text>" +
      "</svg>",
  );

/**
 * Owns the seven chrome primitives for one chart. Mounted via a shellbar toggle:
 * enable() builds + attaches, disable() detaches, setContext() feeds symbol and
 * price context after each history load / symbol change.
 */
export class Chrome {
  private readonly host: ChromeHost;
  private primitives: IPrimitive[] = [];
  private enabled = false;
  private priceLevels: PriceLevels | null = null;
  private legend: PaneLegend | null = null;
  private buySell: BuySellButtons | null = null;
  private unsubClick: (() => void) | null = null;
  private orderAction: ChromeOrderAction | null = null;

  constructor(chart: unknown) {
    this.host = chart as ChromeHost;
  }

  /** Wire BuySellButtons clicks to the shell's existing order action. */
  setOrderAction(fn: ChromeOrderAction): void {
    this.orderAction = fn;
  }

  setContext(ctx: ChromeContext): void {
    if (!this.enabled) return;
    this.applyContext(ctx);
  }

  enable(ctx: ChromeContext, series: SeriesApi | null = null): void {
    if (this.enabled) return;
    const host = this.host;

    const watermark = new LogoWatermark({
      src: TRADEX_MARK,
      position: "bottom-left",
      margin: 6,
      opacity: 0.7,
      label: "TradeX v4",
    });
    host.addPrimitive(watermark, 0);

    // SeriesMarkers binds to the price series id. createMarkers() resolves the
    // series' slot at call time and attaches the primitive to its pane itself.
    if (series) this.primitives.push(series.createMarkers());

    // Session high/low are opt-in in the library defaults (previousClose is on);
    // values come from the bars in view, so turning the styles on is all the
    // shell has to do.
    this.priceLevels = new PriceLevels({
      timezone: "Asia/Kolkata",
      levels: {
        previousClose: { line: true, label: true },
        sessionHigh: { line: true, label: true },
        sessionLow: { line: true, label: true },
      },
    });
    host.addPrimitive(this.priceLevels, 0);

    this.legend = new PaneLegend({
      id: "chrome-symbol",
      title: `${ctx.exchange}:${ctx.symbol}`,
      actions: [],
    });
    host.addPrimitive(this.legend, 0);

    // Empty until a corporate-actions feed exists — the renderer ships regardless.
    const events = new EventMarkers();
    host.addPrimitive(events, 0);

    this.buySell = new BuySellButtons({
      id: "chrome",
      position: "bottom-right",
      buyLabel: "BUY",
      sellLabel: "SELL",
      qty: 10,
    });
    host.addPrimitive(this.buySell, 0);

    // BuySellButtons hit-tests as chrome:buy / chrome:sell / chrome:qty. The
    // chart's single subscribeClick slot is owned by the TradingController, so
    // routing rides the unified 'click' bus (multi-listener, same external id).
    this.unsubClick = host.on("click", (payload) => {
      const id = (payload as { id?: unknown }).id;
      if (id === "chrome:buy") this.orderAction?.("BUY");
      else if (id === "chrome:sell") this.orderAction?.("SELL");
    });

    this.primitives.push(watermark, this.priceLevels, this.legend, events, this.buySell);
    this.enabled = true;
    this.applyContext(ctx);
  }

  disable(): void {
    if (!this.enabled) return;
    this.unsubClick?.();
    this.unsubClick = null;
    for (const p of this.primitives) this.host.removePrimitive(p);
    this.primitives = [];
    this.priceLevels = null;
    this.legend = null;
    this.buySell = null;
    this.enabled = false;
  }

  private applyContext(ctx: ChromeContext): void {
    this.priceLevels?.setLevel("previousClose", { line: true, label: true });
    this.priceLevels?.setLevel("sessionHigh", { line: true, label: true });
    this.priceLevels?.setLevel("sessionLow", { line: true, label: true });
    this.buySell?.setMark(ctx.lastPrice);
    this.legend?.setOptions({
      title: `${ctx.exchange}:${ctx.symbol}`,
      status: () => {
        const chg = Number.isFinite(ctx.lastPrice) && Number.isFinite(ctx.prevClose) && ctx.prevClose !== 0
          ? ((ctx.lastPrice - ctx.prevClose) / ctx.prevClose) * 100
          : null;
        return {
          ticker: `${ctx.exchange}:${ctx.symbol}`,
          lastDayChange: chg === null ? undefined : {
            label: "CHG",
            text: `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%`,
            color: chg >= 0 ? "#26a69a" : "#ef5350",
          },
        };
      },
    });
  }
}