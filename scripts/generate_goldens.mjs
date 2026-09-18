// Golden generator: runs the ACTUAL openalgo-charts indicator code on the shared
// fixtures and writes per-indicator JSON goldens.
//
// The library now comes from `resolveLibrary()`, which refuses to hand it over
// unless the checkout is at the commit in `openalgo-charts.pin` and its dist/ is
// newer than its src/. The previous version of this file imported by an absolute
// path into one developer's ~/Downloads, and that checkout is 2.1.7 — so the
// fixtures the parity suite treats as ground truth were captured from a different
// library version than the one the host and the pin use, silently.
//
// Run via `scripts/regenerate-goldens.sh`, never on its own: the provenance record
// is written from the whole fixture set at the end, so a generator run alone
// leaves it describing a directory this one only partly updated.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { resolveLibrary } from "./library-dist.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const outDir = join(here, "..", "trading", "tests", "analytics", "goldens");
const lib = resolveLibrary({ kinds: ["indicators"] });
const { BUILTIN_INDICATORS } = await import(lib.entry("indicators"));

const fixtures = JSON.parse(readFileSync(join(outDir, "fixtures.json"), "utf8"));
const bars = fixtures.bars.map(([time, open, high, low, close, volume]) => ({
  time, open, high, low, close, volume,
}));

/**
 * The series to capture out of one `calc()` result.
 *
 * The union of what the descriptor *declares* (`plots`) and what the calculation
 * *actually returns* — because the two are not the same thing, and capturing only
 * the declared ones silently truncates the golden:
 *
 *   consolidation-breakout at 2.1.8 declares `rangeHigh`/`rangeLow` and also
 *   returns `breakUp`/`breakDown`/`insideAge` ("hidden" series the host paints
 *   through its tint hook). A golden built from `plots` alone loses those three,
 *   and `test_golden_parity` — which iterates the *backend's* keys and looks each
 *   one up in the golden — then dies on a missing key. A truncated golden reads
 *   exactly like a math regression, so the capture has to follow the result.
 *
 * A "series" is an array whose entries are all numeric (or absent, for warm-up
 * bars). Arrays of objects (footprint cells, pivot records) are not series and
 * are left to the profile goldens, which capture whole structured results.
 *
 * @param {object} descriptor an indicator descriptor
 * @param {object} result its `calc()` output
 */
function seriesOf(descriptor, result) {
  const keys = new Set([
    ...(descriptor.plots ?? []).map((p) => p.key),
    ...Object.keys(result),
  ]);
  const plots = {};
  for (const key of keys) {
    const arr = result[key];
    if (!Array.isArray(arr)) continue;
    if (arr.some((v) => v !== null && v !== undefined && !Number.isFinite(v))) continue;
    plots[key] = arr.map((v) => (Number.isFinite(v) ? v : null));
  }
  return plots;
}

mkdirSync(outDir, { recursive: true });
let written = 0;
const skipped = [];
for (const d of BUILTIN_INDICATORS) {
  if (d.id === "seasonality") continue; // UI table widget, not a series
  try {
    const settings = {};
    for (const inp of d.inputs ?? []) settings[inp.key] = inp.default;
    const result = d.calc(bars, settings);
    const plots = seriesOf(d, result);
    const undeclared = Object.keys(plots).filter(
      (k) => !(d.plots ?? []).some((p) => p.key === k),
    );
    if (undeclared.length > 0) {
      // Not a problem, but it is the case that used to be lost silently.
      console.log(`${d.id}: undeclared series captured: ${undeclared.join(', ')}`);
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
console.log(
  `wrote ${written} goldens from openalgo-charts ${lib.version} (${lib.sha.slice(0, 7)})`,
);
if (skipped.length) console.log(`skipped:\n${skipped.join("\n")}`);
