// Backend indicator wiring. Every formula lives in tradex_trading; this file
// only teaches the chart about them through the Tier-2 external-data
// contract, so a new backend registry entry reaches the UI with zero JS
// changes.
import { createTier2Indicator, type Tier2Context } from "openalgo-charts/indicators";
import {
  registerIndicator,
  type IndicatorInput,
  type IndicatorPlot,
  type IndicatorSettings,
} from "openalgo-charts";

export interface CatalogueEntry {
  id: string;
  name: string;
  category: string;
  placement: "overlay" | "pane";
  params: { name: string; type: string; default: number }[];
  plots: { key: string; kind: string; title: string }[];
}

/** Chart context the fetch needs beyond what Tier2Context carries. */
let currentSymbol = "";
let currentExchange = "";

export function setIndicatorContext(exchange: string, symbol: string): void {
  currentExchange = exchange;
  currentSymbol = symbol;
}

interface ComputeResponse {
  id: string;
  points: ({ time: number } & Record<string, unknown>)[];
}

async function computeOnBackend(
  id: string,
  params: IndicatorSettings,
  from: number,
  to: number,
): Promise<ComputeResponse> {
  const resp = await fetch("/api/charts/indicators/compute", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      exchange: currentExchange,
      symbol: currentSymbol,
      interval: currentInterval,
      from,
      to,
      id,
      params,
    }),
  });
  if (!resp.ok) throw new Error(`indicator ${id} compute failed: ${await resp.text()}`);
  return resp.json() as Promise<ComputeResponse>;
}

let currentInterval = "5m";

export function setIndicatorInterval(interval: string): void {
  currentInterval = interval;
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
    const inputs: IndicatorInput[] = entry.params.map((p) => ({
      key: p.name,
      type: p.type === "int" ? "number" : "number",
      label: p.name.replace(/_/g, " "),
      default: Number(p.default),
    }));

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
        // Symbol/exchange/interval live in module state, not settings, so
        // refetchOn cannot express them; every fetch is window-scoped anyway
        // and the host refetches on symbol change by re-adding instances.
        fetch: async (ctx: Tier2Context) => {
          const out = await computeOnBackend(
            entry.id,
            Object.fromEntries(inputs.map((i) => [i.key, i.default])),
            ctx.from,
            ctx.to,
          );
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
