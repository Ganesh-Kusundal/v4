// Golden generator (P2 Task 1): runs the ACTUAL openalgo-charts
// profile + seasonality reference functions over the shared fixtures and the
// two synthetic fixtures, writing per-study JSON goldens for Task 2's parity gate.
//
// The library is resolved and verified against `openalgo-charts.pin` before
// anything is captured (see scripts/library-dist.mjs); run through
// `scripts/regenerate-goldens.sh`.
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { resolveLibrary } from "./library-dist.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const outDir = join(here, "..", "trading", "tests", "analytics", "goldens", "profiles");
const lib = resolveLibrary({ kinds: ["profile", "indicators"] });
const {
  computeVolumeProfile,
  computeTpo,
  computeMarketProfile,
  computeFootprint,
} = await import(lib.entry("profile"));
const { SEASONALITY } = await import(lib.entry("indicators"));
const fixtures = JSON.parse(
  readFileSync(join(outDir, "..", "fixtures.json"), "utf8"),
);
const bars = fixtures.bars.map(([time, open, high, low, close, volume]) => ({
  time, open, high, low, close, volume,
}));

// Floats round-trip through Number.isFinite (keep) else null, matching the
// indicator golden convention.
function sanitize(obj) {
  if (typeof obj === "number") return Number.isFinite(obj) ? obj : null;
  if (Array.isArray(obj)) return obj.map(sanitize);
  if (obj !== null && typeof obj === "object") {
    const out = {};
    for (const [k, v] of Object.entries(obj)) out[k] = sanitize(v);
    return out;
  }
  return obj;
}

// --- Synthetic footprint trades (plan-mandated rule) -------------------------
// Per fixture bar: one ask print at high for volume/2, one bid print at low for
// volume/2. Run over ALL 300 bars' trades at the first bar's time.
const footprintTime = bars[0].time;
const trades = [];
for (const b of bars) {
  const half = (b.volume ?? 0) / 2;
  trades.push({ price: b.high, qty: half, side: "ask" });
  trades.push({ price: b.low, qty: half, side: "bid" });
}

// --- Synthetic seasonality series (plan-mandated rule) -----------------------
// Deterministic 13-month series: Jan 2025..Jan 2026, one bar per month at UTC
// first-of-month midnight, close = 100 + 3*month_index (Jan 2025 = month 0).
const seasonBars = [];
for (let m = 0; m < 13; m++) {
  const ts = Math.round(Date.UTC(2025, m, 1) / 1000);
  const close = 100 + 3 * m;
  seasonBars.push({
    time: ts,
    open: close - 1,
    high: close + 1,
    low: close - 2,
    close,
    volume: 1000,
  });
}

const GOLDENS = [
  {
    id: "volume-profile",
    settings: { tickSize: 0.1, valueAreaPercent: 0.7 },
    result: computeVolumeProfile(bars, 0.1, 0.7),
  },
  {
    id: "tpo",
    settings: { periodBars: 30, tickSize: 0.1, valueAreaPercent: 0.7, ibPeriods: 2 },
    result: computeTpo(bars, 30, 0.1, 0.7, 2),
  },
  {
    id: "market-profile",
    settings: {
      tickSize: 0.1, rowTicks: 1, session: "day", blockMinutes: 30,
      valueAreaPercent: 0.7, initialBalancePeriods: 2, compositeSessions: 1, tailEdges: 0,
    },
    result: computeMarketProfile(bars, {
      tickSize: 0.1, rowTicks: 1, session: "day", blockMinutes: 30,
      valueAreaPercent: 0.7, initialBalancePeriods: 2, compositeSessions: 1, tailEdges: 0,
    }),
  },
  {
    id: "footprint",
    settings: { time: footprintTime, tickSize: 0.1, rowTicks: 1 },
    result: computeFootprint(footprintTime, trades, 0.1, 1),
  },
  {
    id: "seasonality",
    settings: { startYear: 2015, ignoredMonths: "" },
    result: SEASONALITY.table({
      bars: seasonBars,
      settings: { startYear: 2015, ignoredMonths: "" },
    }),
  },
];

mkdirSync(outDir, { recursive: true });
for (const g of GOLDENS) {
  writeFileSync(
    join(outDir, `${g.id}.json`),
    JSON.stringify({ id: g.id, settings: g.settings, result: sanitize(g.result) }, null, 1),
  );
}
console.log(
  `wrote 5 profile/seasonality goldens from openalgo-charts ${lib.version} (${lib.sha.slice(0, 7)})`,
);
