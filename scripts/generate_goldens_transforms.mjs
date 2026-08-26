// One-time golden generator: runs the ACTUAL openalgo-charts transform classes
// over the shared fixtures and writes per-transform JSON goldens.
import {
  HeikinAshiTransform,
  RenkoTransform,
  RangeBarsTransform,
  LineBreakTransform,
  PointFigureTransform,
  KagiTransform,
} from "/Users/apple/Downloads/openalgo-charts-master/dist/openalgo-charts.transform.mjs";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const outDir = join(here, "..", "trading", "tests", "analytics", "goldens", "transforms");
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
console.log("wrote 6 transform goldens");
