// Transform tier: every series transform renders bars computed by tradex_trading
// (POST /api/charts/transforms/{id}). The chart posts its visible window, the
// backend returns transformed bars, and this module renders them verbatim. No
// transform math in JS.
//
// Renderer wiring — see the reference openalgo-charts-master/src/transform/index.ts:
// the `openalgo-charts/transform` tier (imported as a side-effect below) registers
// the 'point-figure' and 'kagi' chart-types into the SAME registry the base entry
// uses (the tier bundles import the base as external, so createChart sees what they
// register). The four candlestick kinds (heikin-ashi/renko/range-bars/line-break)
// render as an ordinary candlestick series and need no new chart-type. Only
// point-figure and kagi have custom renderers, and they come from the tier import.
import type { Bar } from "openalgo-charts";
import "openalgo-charts/transform"; // side-effect: registers point-figure + kagi renderers
import { registerTransformChartTypes } from "openalgo-charts/transform";
import { expectJson } from "./http";

export type TransformKind = "candlestick" | "point-figure" | "kagi";

export interface TransformSpec {
  id: string;
  name: string;
  kind: TransformKind;
  params: { key: string; label: string; default: number }[];
}

export const TRANSFORMS: TransformSpec[] = [
  { id: "heikin-ashi", name: "Heikin Ashi", kind: "candlestick", params: [] },
  { id: "renko", name: "Renko", kind: "candlestick", params: [{ key: "box_size", label: "Box size", default: 2 }] },
  { id: "range-bars", name: "Range Bars", kind: "candlestick", params: [{ key: "range", label: "Range", default: 3 }] },
  { id: "line-break", name: "Line Break", kind: "candlestick", params: [{ key: "lines", label: "Lines", default: 3 }] },
  { id: "point-figure", name: "Point & Figure", kind: "point-figure", params: [{ key: "box_size", label: "Box size", default: 1 }, { key: "reversal", label: "Reversal", default: 3 }] },
  { id: "kagi", name: "Kagi", kind: "kagi", params: [{ key: "reversal", label: "Reversal", default: 2 }] },
];

export function transformById(id: string): TransformSpec | undefined {
  return TRANSFORMS.find((t) => t.id === id);
}

export async function computeTransform(
  id: string,
  params: Record<string, unknown>,
  bars: Bar[],
): Promise<Bar[]> {
  const resp = await fetch(`/api/charts/transforms/${encodeURIComponent(id)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, params, bars }),
  });
  const body = await expectJson<{ id: string; bars: Bar[] }>(resp);
  return body.bars;
}

// Belt-and-suspenders guard: package.json marks the transform tier as a
// side-effect (so Vite keeps the bare import above), and the tier also runs this
// on import. Calling it explicitly is idempotent and guarantees the point-figure/
// kagi renderers are present even if a future bundler tree-shakes the side-effect.
registerTransformChartTypes();
