// Golden generator: runs the ACTUAL openalgo-charts transform classes over the
// shared fixtures and writes per-transform JSON goldens.
//
// Same change as the indicator generator: the library is resolved from the repo and
// verified against `openalgo-charts.pin` before a single bar is transformed, rather
// than imported from a hardcoded path into another checkout. Run through
// `scripts/regenerate-goldens.sh`.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { resolveLibrary } from "./library-dist.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const outDir = join(here, "..", "trading", "tests", "analytics", "goldens", "transforms");
const lib = resolveLibrary({ kinds: ["transform"] });
const {
  HeikinAshiTransform,
  RenkoTransform,
  RangeBarsTransform,
  LineBreakTransform,
  PointFigureTransform,
  KagiTransform,
} = await import(lib.entry("transform"));

const fixtures = JSON.parse(readFileSync(join(outDir, "..", "fixtures.json"), "utf8"));
const bars = fixtures.bars.map(([time, open, high, low, close, volume]) => ({
  time, open, high, low, close, volume,
}));

function runTransform(transform, bars) {
  transform.reset();
  const out = [];
  for (const b of bars) out.push(...transform.push(b));
  if (transform.flush) out.push(...transform.flush());
  // ensureIncreasingTimes — strict-increase collision handling, mirroring TS.
  let prev = -Infinity;
  for (const b of out) {
    if (b.time <= prev) b.time = prev + 1;
    prev = b.time;
  }
  return out;
}

const T = {
  "heikin-ashi": new HeikinAshiTransform(),
  "renko": new RenkoTransform({ boxSize: 2 }),
  "range-bars": new RangeBarsTransform({ range: 3 }),
  "line-break": new LineBreakTransform({ lines: 3 }),
  "point-figure": new PointFigureTransform({ boxSize: 1, reversal: 3 }),
  "kagi": new KagiTransform({ reversal: 2 }),
};
const SETTINGS = {
  "heikin-ashi": {},
  "renko": { boxSize: 2 },
  "range-bars": { range: 3 },
  "line-break": { lines: 3 },
  "point-figure": { boxSize: 1, reversal: 3 },
  "kagi": { reversal: 2 },
};

mkdirSync(outDir, { recursive: true });
for (const [id, transform] of Object.entries(T)) {
  const result = runTransform(transform, bars);
  const serialized = result.map((b) => [
    b.time, b.open, b.high, b.low, b.close, b.volume ?? 0,
  ]);
  writeFileSync(
    join(outDir, `${id}.json`),
    JSON.stringify({ id, settings: SETTINGS[id], bars: serialized }, null, 1),
  );
}
console.log(
  `wrote 6 transform goldens from openalgo-charts ${lib.version} (${lib.sha.slice(0, 7)})`,
);
