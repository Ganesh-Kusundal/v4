// Backend indicator wiring. Every formula lives in tradex_trading; this file
// only teaches the chart about them through the Tier-2 external-data
// contract, so a new backend registry entry reaches the UI with zero JS
// changes. Standardized against openalgo-charts-master/src/indicators/external.ts.
//
// Rendering metadata (plot series type, default colour, reference levels) is
// adopted from the SAME-version openalgo-charts built-in descriptors when an
// id matches — the reference library stays the source of visual truth, while
// the values themselves are always computed by the backend.
import {
  createTier2Indicator,
  BUILTIN_INDICATORS,
  type Tier2Context,
  type Tier2Point,
} from "openalgo-charts/indicators";
import {
  registerIndicator,
  type IndicatorInput,
  type IndicatorPlot,
} from "openalgo-charts";
import { expectJson } from "./http";

export interface CatalogueEntry {
  id: string;
  name: string;
  category: string;
  placement: "overlay" | "pane";
  params: { name: string; type: string; default: number | string | boolean }[];
  plots: { key: string; kind: string; title: string }[];
}

const BUILTIN_BY_ID = new Map(BUILTIN_INDICATORS.map((d) => [d.id, d]));

/** Poll cadence for live recomputation. The source library recalculates
 * Tier-1 indicators synchronously on every bar update; across the backend
 * boundary we approximate a per-closed-bar refresh with one compute POST per
 * instance per poll (only propagated when values actually moved). */
const REFRESH_MS = 15_000;


// Legacy module-state fallback until every caller passes symbol/exchange
// through IndicatorSettings (migration shim, not the source of truth).
let fallbackSymbol = "";
let fallbackExchange = "";
let fallbackInterval = "5m";

export function setIndicatorContext(exchange: string, symbol: string): void {
  fallbackExchange = exchange;
  fallbackSymbol = symbol;
}
export function setIndicatorInterval(interval: string): void {
  fallbackInterval = interval;
}

interface ComputeResponse {
  id: string;
  points: ({ time: number } & Record<string, unknown>)[];
}

async function computeOnBackend(
  id: string,
  params: Record<string, unknown>,
  ctx: Tier2Context,
): Promise<ComputeResponse> {
  // Settings are the source of truth (Tier-2 refetchOn); fallback covers
  // callers that still use setIndicatorContext() before first paint.
  const exchange = (params["exchange"] as string) || (ctx.settings["exchange"] as string) || fallbackExchange || "NSE";
  const symbol = (params["symbol"] as string) || (ctx.settings["symbol"] as string) || fallbackSymbol || "RELIANCE";
  const interval = (params["interval"] as string) || (ctx.settings["interval"] as string) || fallbackInterval;
  // Strip routing keys from indicator params so backend validation sees only
  // declared indicator params (backend rejects unknown keys loudly).
  const indicatorParams: Record<string, unknown> = { ...params };
  delete indicatorParams["exchange"];
  delete indicatorParams["symbol"];
  delete indicatorParams["interval"];

  const resp = await fetch("/api/charts/indicators/compute", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      exchange,
      symbol,
      interval,
      from: ctx.from,
      to: ctx.to,
      id,
      params: indicatorParams,
    }),
  });
  return expectJson<ComputeResponse>(resp);
}

function declaredParamsOnly(
  entry: CatalogueEntry,
  settings: Readonly<Record<string, unknown>>,
): Record<string, unknown> {
  const allowed = new Set(entry.params.map((p) => p.name));
  const filtered: Record<string, unknown> = {};
  for (const key of allowed) {
    if (key in settings) filtered[key] = settings[key];
  }
  return filtered;
}

function mapPoints(out: ComputeResponse, entry: CatalogueEntry): Tier2Point[] {
  return out.points.map((p) => {
    const values: Record<string, number | null> = {};
    for (const plot of entry.plots) {
      const v = (p as Record<string, unknown>)[plot.key];
      values[plot.key] =
        typeof v === "number" ? v : v === null || v === undefined ? null : Number(v);
    }
    return { time: p.time, values };
  });
}

/**
 * Plot definition for a backend plot key: adopt the reference descriptor's
 * series type / style / colour when its id+plot key match, otherwise fall
 * back to the catalogue's own declaration.
 */
function resolvePlots(entry: CatalogueEntry): IndicatorPlot[] {
  const refDescriptor = BUILTIN_BY_ID.get(entry.id);
  const refInputs = ((refDescriptor?.inputs ?? []) as unknown as {
    key: string; default?: unknown;
  }[]);
  const tsPlots = ((refDescriptor?.plots ?? []) as unknown as {
    key: string; type?: string; style?: Record<string, unknown>; colorKey?: string;
  }[]);
  const colorDefaultFor = (colorKey?: string): string | undefined => {
    if (!colorKey) return undefined;
    const hit = refInputs.find((i) => i.key === colorKey);
    const v = hit?.default;
    return typeof v === "string" && v.startsWith("#") ? v : undefined;
  };
  return entry.plots.map((p, index) => {
    // Match by key first; positional match keeps colours right for indicators
    // whose backend plot keys were normalised ('value' vs 'ma').
    const ref =
      tsPlots.find((q) => q.key === p.key) ??
      tsPlots[index] ??
      {};
    const style: Record<string, unknown> = { ...(ref.style ?? {}) };
    const color = colorDefaultFor(ref.colorKey);
    if (color !== undefined && style["color"] === undefined) style["color"] = color;
    return {
      key: p.key,
      // A histogram must render as a histogram — flattening every backend
      // plot to a line redraws MACD/Volume/WVF as trend lines.
      type: ((ref.type as IndicatorPlot["type"]) ?? p.kind ?? "line") as IndicatorPlot["type"],
      title: p.title,
      style: Object.keys(style).length > 0 ? (style as IndicatorPlot["style"]) : undefined,
    };
  });
}


/**
 * Fetch the catalogue and register every entry as a Tier-2 descriptor.
 * Returns the catalogue so the host can build its indicator menu from it —
 * the menu must never be hardcoded here.
 */
export async function registerBackendIndicators(): Promise<CatalogueEntry[]> {
  const resp = await fetch("/api/charts/indicators");
  const body = await expectJson<{ indicators: CatalogueEntry[] }>(resp);

  for (const entry of body.indicators) {
    const numberInputs: IndicatorInput[] = entry.params.map((p) => ({
      key: p.name,
      type: "number",
      label: p.name.replace(/_/g, " "),
      default: Number(p.default),
    }));
    const inputs: IndicatorInput[] = [
      // Routing inputs hidden from the cog but present in settings so
      // refetchOn can invalidate when symbol/interval changes.
      { key: "symbol", type: "text", label: "Symbol", default: fallbackSymbol || "RELIANCE" } as unknown as IndicatorInput,
      { key: "exchange", type: "text", label: "Exchange", default: fallbackExchange || "NSE" } as unknown as IndicatorInput,
      { key: "interval", type: "text", label: "Interval", default: fallbackInterval } as unknown as IndicatorInput,
      ...numberInputs,
    ];

    const plots = resolvePlots(entry);
    const refDescriptor = BUILTIN_BY_ID.get(entry.id);

    registerIndicator(
      createTier2Indicator({
        id: `backend:${entry.id}`,
        name: `${entry.name} (backend)`,
        category: entry.category,
        placement: entry.placement === "overlay" ? "onchart" : "pane",
        inputs,
        plots,
        // Reference levels (RSI 70/30, stochastic 80/20, …) come from the
        // matching built-in descriptor so panes look like the source UI.
        // Passed through untouched — the reference fn already takes settings
        // and tolerates an empty read; wrapping it here dereferenced
        // .levels on descriptors that have none and threw inside addIndicator.
        levels: refDescriptor?.levels ?? undefined,
        refetchOn: ["symbol", "exchange", "interval", ...entry.params.map((p) => p.name)],
        fetch: async (ctx: Tier2Context) => {
          // Forward only declared indicator params — the chart's
          // IndicatorSettings also carries style keys like `value:color` and
          // routing keys that the backend rightly rejects as unknown.
          const filtered = declaredParamsOnly(entry, ctx.settings as Record<string, unknown>);
          const out = await computeOnBackend(entry.id, filtered, ctx);
          return mapPoints(out, entry);
        },
        subscribe: (
          ctx: Tier2Context,
          push: (point: Tier2Point) => void,
        ) => {
          // Live-bars parity: the source recomputes on every new bar; one
          // debounced compute poll per instance plays that role across the
          // API boundary. Merging goes through the Tier-2 upsert.
          let stopped = false;
          let timer: ReturnType<typeof setTimeout> | null = null;
          let lastSerialized = "";
          const tick = async (): Promise<void> => {
            timer = null;
            if (stopped) return;
            try {
              const filtered = declaredParamsOnly(entry, ctx.settings as Record<string, unknown>);
              const out = await computeOnBackend(entry.id, filtered, ctx);
              const serialized = JSON.stringify(out.points);
              if (serialized !== lastSerialized) {
                lastSerialized = serialized;
                for (const pt of mapPoints(out, entry)) push(pt);
              }
            } catch {
              /* transient backend/network hiccup — next poll retries */
            }
            if (!stopped) timer = setTimeout(() => void tick(), REFRESH_MS);
          };
          timer = setTimeout(() => void tick(), REFRESH_MS);
          return () => {
            stopped = true;
            if (timer !== null) clearTimeout(timer);
          };
        },
      }),
    );
  }
  return body.indicators;
}

