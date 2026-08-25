// One-time golden generator: runs the ACTUAL openalgo-charts indicator code on
// the shared fixtures and writes per-indicator JSON goldens.
import { BUILTIN_INDICATORS } from "/Users/apple/Downloads/openalgo-charts-master/dist/openalgo-charts.indicators.mjs";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const outDir = join(here, "..", "trading", "tests", "analytics", "goldens");
const fixtures = JSON.parse(readFileSync(join(outDir, "fixtures.json"), "utf8"));
const bars = fixtures.bars.map(([time, open, high, low, close, volume]) => ({
  time, open, high, low, close, volume,
}));

mkdirSync(outDir, { recursive: true });
let written = 0;
const skipped = [];
for (const d of BUILTIN_INDICATORS) {
  if (d.id === "seasonality") continue; // UI table widget, not a series
  try {
    const settings = {};
    for (const inp of d.inputs ?? []) settings[inp.key] = inp.default;
    const result = d.calc(bars, settings);
    const plots = {};
    for (const p of d.plots) {
      const arr = result[p.key];
      if (!Array.isArray(arr)) continue;
      plots[p.key] = arr.map((v) => (Number.isFinite(v) ? v : null));
    }
    writeFileSync(
      join(outDir, `${d.id}.json`),
      JSON.stringify({ id: d.id, settings, plots }, null, 1),
    );
    written++;
  } catch (e) {
    skipped.push(`${d.id}: ${e.message}`);
  }
}
console.log(`wrote ${written} goldens`);
if (skipped.length) console.log(`skipped:\n${skipped.join("\n")}`);
