// Backend indicator wiring. Every formula lives in tradex_trading; this file
// only teaches the chart about them through the Tier-2 external-data
// contract, so a new backend registry entry reaches the UI with zero JS
// changes. Standardized against openalgo-charts-master/src/indicators/external.ts.
import { createTier2Indicator, type Tier2Context } from "openalgo-charts/indicators";
import {
  registerIndicator,
  type IndicatorInput,
  type IndicatorPlot,
} from "openalgo-charts";

export interface CatalogueEntry {
  id: string;
  name: string;
  category: string;
  placement: "overlay" | "pane";
  params: { name: string; type: string; default: number }[];
  plots: { key: string; kind: string; title: string }[];
}

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
  if (!resp.ok) throw new Error(`indicator ${id} compute failed: ${await resp.text()}`);
  return resp.json() as Promise<ComputeResponse>;
}

/**
 * Fetch the catalogue and register every entry as a Tier-2 descriptor.
 * Returns the catalogue so the host can build its indicator menu from it —
 * the menu must never be hardcoded here.
 */
export async function registerBackendIndicators(): Promise<CatalogueEntry[]> {
  const resp = await fetch("/api/charts/indicators");
  if (!resp.ok) throw new Error(`catalogue failed (${resp.status})`);
  const body = (await resp.json()) as { indicators: CatalogueEntry[] };

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

    const plots: IndicatorPlot[] = entry.plots.map((p) => ({
      key: p.key,
      type: "line",
      title: p.title,
    }));

    registerIndicator(
      createTier2Indicator({
        id: `backend:${entry.id}`,
        name: `${entry.name} (backend)`,
        category: entry.category,
        placement: entry.placement === "overlay" ? "onchart" : "pane",
        inputs,
        plots,
        refetchOn: ["symbol", "exchange", "interval", ...entry.params.map((p) => p.name)],
        fetch: async (ctx: Tier2Context) => {
          // Forward only declared indicator params — the chart's IndicatorSettings
          // also carries style keys like `value:color` and routing keys that the
          // backend rightly rejects as unknown. Filter to the catalogue's param
          // list so Compute stays strict.
          const allowed = new Set(entry.params.map((p) => p.name));
          const filtered: Record<string, unknown> = {};
          const settings = ctx.settings as Record<string, unknown>;
          for (const key of allowed) {
            if (key in settings) filtered[key] = settings[key];
          }
          // Routing (symbol/exchange/interval) is read by computeOnBackend from
          // ctx.settings / fallback, not from indicator params — keep it out of
          // the filtered payload by design.
          const out = await computeOnBackend(entry.id, filtered, ctx);
          return out.points.map((p) => {
            const values: Record<string, number | null> = {};
            for (const plot of entry.plots) {
              const v = (p as Record<string, unknown>)[plot.key];
              values[plot.key] =
                typeof v === "number" ? v : v === null || v === undefined ? null : Number(v);
            }
            return { time: p.time, values };
          });
        },
      }),
    );
  }
  return body.indicators;
}
