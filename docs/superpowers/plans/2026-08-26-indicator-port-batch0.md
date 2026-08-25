# Indicator Port Batch 0 — Golden Harness + Bug Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the TS-golden parity harness and fix the three audited correctness bugs (MACD non-standard, stochastic unsmoothed %K, EMA seeding convention) so backend output is bit-exact with openalgo-charts.

**Architecture:** A one-time Node script executes the actual openalgo-charts indicator code (from its built dist bundle) on a deterministic fixture candle set and writes committed JSON goldens. A parametrized pytest compares every backend registry indicator against its golden at 1e-9 tolerance. Backend fixes land until the suite is green.

**Tech Stack:** Python 3.13 stdlib only on the backend side; Node ESM importing `/Users/apple/Downloads/openalgo-charts-master/dist/openalgo-charts.indicators.mjs` for goldens; pytest.

## Global Constraints

- Reference source of truth: `/Users/apple/Downloads/openalgo-charts-master` (its `dist/openalgo-charts.indicators.mjs` exports `BUILTIN_INDICATORS`).
- Exact parity: values match to `pytest.approx(abs=1e-9)`; `None` positions align bar-for-bar.
- TS `ema` (`src/indicators/ema.ts`) seeds from `values[0]`, emits from index 0 — this becomes the backend convention.
- TS rolling helpers treat non-finite window members as "bad" → whole-window NaN (see `src/indicators/calc.ts:13-36`). Ports must reproduce this.
- Param names in the backend stay snake_case (`k_period`, not `kPeriod`) — parity is mathematical, not lexical.
- All work in `/Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4`. Run tests with `uv run pytest <path> -q`.
- Existing test conventions: duck-typed candle fixtures like `trading/tests/analytics/test_indicator_registry.py:31-79`.
- Seasonality excluded (UI table widget, no series).

## Verified reference facts (from executing the dist bundle)

| id | TS plot keys | numeric/source inputs (defaults) |
|---|---|---|
| sma | `ma` | length=20 |
| ema | `ma` | length=20 |
| rsi | `rsi` | length=14 overbought=70 oversold=30 |
| roc | `roc` | length=9 |
| macd | `histogram,macd,signal` | fastPeriod=12 slowPeriod=26 signalPeriod=9 |
| bollinger | `upper,basis,lower` | length=20 stdDev=2 |
| atr | `atr` | period=14 |
| obv | `obv,ma,bbUpper,bbLower` | maLength=14 bbMult=2 (default maType=None → ma/bb plots null) |
| vwap | `vwap,upper1..3,lower1..3` | bandMult defaults (session anchor) |
| stochastic | `k,d` | kPeriod=14 kSmoothing=3 dPeriod=3 |
| supertrend | `up,down` | period=10 multiplier=3 |

---

### Task 1: Fixture candle generator

**Files:**
- Create: `scripts/generate_fixtures.py`
- Create (generated): `trading/tests/analytics/goldens/fixtures.json`

**Interfaces:**
- Produces: `trading/tests/analytics/goldens/fixtures.json` — `{"bars": [[time_utc_s, open, high, low, close, volume], ...]}` consumed by Task 2 (Node harness) and Task 3 (pytest loader).

- [ ] **Step 1: Write the generator script**

```python
"""Deterministic fixture candles shared by the TS golden harness and pytest."""
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
OUT = Path(__file__).resolve().parents[1] / "trading" / "tests" / "analytics" / "goldens" / "fixtures.json"


def main() -> None:
    rng = random.Random(42)
    t0_day1 = int(datetime(2026, 7, 15, 9, 15, tzinfo=IST).timestamp())
    t0_day2 = int(datetime(2026, 7, 16, 9, 15, tzinfo=IST).timestamp())

    price = 100.0
    bars = []
    for i in range(300):
        ts = t0_day1 + i * 60 if i < 240 else t0_day2 + (i - 240) * 60
        shock = rng.gauss(0, 0.6)
        if i < 80:        # steady uptrend
            step = 0.15 + math.sin(i / 15.0) * 0.2
        elif i < 140:     # chop
            step = shock
        elif i < 170:     # flat run
            step = 0.0
        else:             # downtrend
            step = -0.12 + shock * 0.4
        close = max(1.0, price + step)
        o = price
        h = max(o, close) + abs(rng.gauss(0, 0.3))
        low = min(o, close) - abs(rng.gauss(0, 0.3))
        vol = 0.0 if i in (50, 51, 200) else float(rng.randint(1000, 5000))
        bars.append([ts, round(o, 4), round(h, 4), round(low, 4), round(close, 4), vol])
        price = close

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"bars": bars}))
    print(f"wrote {len(bars)} bars to {OUT}")


if __name__ == "__main__":
    main()
```

Design notes baked into the data: a day boundary at bar 240 (exercises VWAP session reset), zero-volume bars at 50/51/200 (volume-indicator edge), a 30-bar flat run (zero-sd Bollinger edge), trend/chop regimes.

- [ ] **Step 2: Run it**

Run: `uv run python scripts/generate_fixtures.py`
Expected: `wrote 300 bars to .../goldens/fixtures.json`

- [ ] **Step 3: Sanity-check the JSON**

Run: `uv run python -c "import json;d=json.load(open('trading/tests/analytics/goldens/fixtures.json'));b=d['bars'];assert len(b)==300;assert b[240][0]-b[239][0]>60;print(b[0]);print(b[50])"`
Expected: first bar printed, bar 50 has volume 0.0, day-gap assertion passes.

- [ ] **Step 4: Commit**

```bash
git add scripts/generate_fixtures.py trading/tests/analytics/goldens/fixtures.json
git commit -m "test: deterministic fixture candles for indicator goldens"
```

---

### Task 2: Node golden harness

**Files:**
- Create: `scripts/generate_goldens.mjs`
- Create (generated): `trading/tests/analytics/goldens/<id>.json` for every computable openalgo-charts indicator

**Interfaces:**
- Consumes: `goldens/fixtures.json` (Task 1).
- Produces: `goldens/<id>.json` — `{"id", "settings": {ts_key: default}, "plots": {ts_plot_key: [number|null]}}`, consumed by Task 3 and all later port batches.

- [ ] **Step 1: Verify the dist bundle imports and exposes what we need**

Run:
```bash
node -e "import('/Users/apple/Downloads/openalgo-charts-master/dist/openalgo-charts.indicators.mjs').then(m => console.log(Array.isArray(m.BUILTIN_INDICATORS), m.BUILTIN_INDICATORS.length))"
```
Expected: `true <count ≈ 80>`.

- [ ] **Step 2: Write the harness**

```js
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
```

- [ ] **Step 3: Run it**

Run: `node scripts/generate_goldens.mjs`
Expected: `wrote <N> goldens` with N ≥ 70; inspect any skips. If a skip is a Batch 0 target (macd/stochastic/sma/etc.), stop and debug — those must generate.

- [ ] **Step 4: Spot-check three goldens against the audit facts**

Run: `uv run python -c "
import json
g = 'trading/tests/analytics/goldens/'
macd = json.load(open(g+'macd.json'))
assert set(macd['plots']) == {'histogram','macd','signal'}, macd['plots'].keys()
assert macd['settings']['fastPeriod'] == 12
stoch = json.load(open(g+'stochastic.json'))
assert stoch['settings']['kSmoothing'] == 3
n = len(macd['plots']['macd'])
assert all(len(v)==n for v in macd['plots'].values())
print('ok')"
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add scripts/generate_goldens.mjs trading/tests/analytics/goldens/
git commit -m "test: TS golden fixtures from openalgo-charts source"
```

---

### Task 3: Parity test runner (expect red)

**Files:**
- Create: `trading/tests/analytics/test_golden_parity.py`

**Interfaces:**
- Consumes: `compute_indicator(id, candles, params) -> dict[str, list]` (existing registry API), goldens from Task 2.
- Produces: the acceptance gate every later batch must turn green.

- [ ] **Step 1: Write the test**

```python
"""Golden parity: every registered indicator must match the actual
openalgo-charts TypeScript implementation exactly (1e-9, None-aligned)."""
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tradex_trading.analytics.indicators import compute_indicator, get_indicator_spec

GOLDENS = Path(__file__).parent / "goldens"

# TS descriptor id -> {TS settings key: backend param name}
PARAM_MAP = {
    "sma": {"length": "period"},
    "ema": {"length": "period"},
    "rsi": {"length": "period"},
    "roc": {"length": "period"},
    "macd": {"fastPeriod": "fast", "slowPeriod": "slow", "signalPeriod": "signal"},
    "bollinger": {"length": "period", "stdDev": "num_std"},
    "atr": {},
    "obv": {},
    "vwap": {},
    "stochastic": {"kPeriod": "k_period", "kSmoothing": "smooth_k", "dPeriod": "d_period"},
    "supertrend": {},
}

# backend plot key -> TS golden plot key (identity when absent)
PLOT_MAP = {
    "sma": {"value": "ma"},
    "ema": {"value": "ma"},
    "bollinger": {"middle": "basis", "upper": "upper", "lower": "lower"},
}


class _OHLC:
    def __init__(self, o, h, l, c):
        self.open = _P(o); self.high = _P(h); self.low = _P(l); self.close = _P(c)


class _P:
    def __init__(self, v):
        self.value = Decimal(str(v))


class _Vol:
    def __init__(self, v):
        self.value = v


class _Candle:
    def __init__(self, time, o, h, l, c, v):
        self.timestamp = datetime.fromtimestamp(time)  # tz-naive, IST wall clock
        self.ohlc = _OHLC(o, h, l, c)
        self.volume = _Vol(v)


def _candles():
    bars = json.loads((GOLDENS / "fixtures.json").read_text())["bars"]
    return [_Candle(*b) for b in bars]


CANDLES = _candles()


def _golden_ids():
    return sorted(p.stem for p in GOLDENS.glob("*.json") if p.stem != "fixtures")


def _compare(name, actual, expected):
    assert len(actual) == len(expected), f"{name}: length {len(actual)} != {len(expected)}"
    for i, (a, e) in enumerate(zip(actual, expected)):
        if e is None:
            assert a is None, f"{name}[{i}]: expected None, got {a}"
        else:
            assert a is not None, f"{name}[{i}]: expected {e}, got None"
            assert a == pytest.approx(e, abs=1e-9), f"{name}[{i}]"


@pytest.mark.parametrize("ts_id", [gid for gid in _golden_ids()])
def test_parity_against_ts_source(ts_id):
    spec = get_indicator_spec(ts_id)
    if spec is None:
        pytest.skip(f"{ts_id}: not yet ported")
    if ts_id not in PARAM_MAP:
        pytest.skip(f"{ts_id}: param mapping not yet defined (later batch)")
    golden = json.loads((GOLDENS / f"{ts_id}.json").read_text())
    params = {py: golden["settings"][ts] for ts, py in PARAM_MAP[ts_id].items()}
    got = compute_indicator(ts_id, CANDLES, params)

    if ts_id == "supertrend":
        # TS splits the line into up/down by direction; backend emits one line.
        up, down, direction = golden["plots"]["up"], golden["plots"]["down"], got["direction"]
        expected = [
            u if d == 1 else w
            for u, w, d in zip(up, down, direction)
            if True
        ]
        _compare("line", got["line"], expected)
        return

    for bk, gk in PLOT_MAP.get(ts_id, {k: k for k in got}).items():
        _compare(f"{ts_id}.{bk}", got[bk], golden["plots"][gk])


def test_every_backend_indicator_has_param_mapping():
    """Registry ids beyond Batch 0 must be explicitly mapped or skipped."""
    for entry in __import__("tradex_trading.analytics.indicators", fromlist=["indicator_catalogue"]).indicator_catalogue():
        assert entry["id"] in PARAM_MAP, f"{entry['id']} missing from PARAM_MAP"
```

Note on the supertrend branch: zip pairs each bar's direction with that bar's up/down value; where direction is still `None` (warmup) both TS plots are null too, so the comparison holds.

- [ ] **Step 2: Run it and record the failures**

Run: `uv run pytest trading/tests/analytics/test_golden_parity.py -q`
Expected: FAILURES on `ema`, `macd`, `stochastic`, and anything ema-derived. PASSING: `sma`, `rsi`, `roc`, `bollinger`, `atr`, `obv`.

Contingency — legitimate divergences: if `vwap` fails only around the day boundary (TS session anchoring vs backend IST-day reset semantics) or `supertrend` differs only in warmup direction seeding, add the id to a module-level `KNOWN_DIVERGENCES: dict[str, str]` with a one-line justification comment and `pytest.skip(KNOWN_DIVERGENCES[ts_id])` in the test. Everything else must be fixed in Tasks 4–6, never allowlisted.

- [ ] **Step 3: Commit the red test**

```bash
git add trading/tests/analytics/test_golden_parity.py
git commit -m "test: golden parity gate against openalgo-charts TS source (red)"
```

---

### Task 4: EMA seed convention change

**Files:**
- Modify: `trading/src/tradex_trading/analytics/indicators.py:48-78` (`ema`)
- Test: existing suite + Task 3 parity test

**Interfaces:**
- Produces: `ema(values: list, period: int) -> list[float]` — full-length list seeded from `values[0]`, NO None padding (was: SMA-seeded with `period-1` Nones). All callers (registry `_fn_ema`, engine, scanners) consume the same signature.
- Note: supertrend/rsi/atr use Wilder RMA/SMA seeding internally and are untouched.

- [ ] **Step 1: Find tests asserting the old warmup padding**

Run: `grep -rn "ema" trading/tests/analytics/test_analytics.py trading/tests/analytics/test_engine_historical.py | grep -in "none\|warm\|\[:.*\]"`
Fix any assertion that expects leading `None`s from `ema` (the tail-non-None test still passes since ema now emits more defined values).

- [ ] **Step 2: Replace the function body**

```python
def ema(values: list, period: int) -> list:
    """Exponential Moving Average, seeded from values[0] (openalgo-charts parity).

    Emits from index 0 — no warmup padding. k = 2/(period+1).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    floats = [_to_float(v) for v in values]
    if not floats:
        return []
    k = 2.0 / (period + 1)
    prev = floats[0]
    out = [prev]
    for i in range(1, len(floats)):
        prev = floats[i] * k + prev * (1.0 - k)
        out.append(prev)
    return out
```

- [ ] **Step 3: Add a seed-convention unit test**

Append to `TestGoldenValues` in `trading/tests/analytics/test_indicator_registry.py`:

```python
    def test_ema_seeds_from_first_value(self):
        result = ema(CLOSES, 3)
        assert result[0] == CLOSES[0]
        k = 2.0 / 4.0
        assert result[1] == pytest.approx(CLOSES[1] * k + CLOSES[0] * (1 - k))
```

- [ ] **Step 4: Run analytics tests**

Run: `uv run pytest trading/tests/analytics/ trading/tests/analytics/test_golden_parity.py -q`
Expected: new ema test passes; `ema` parity green; count of failures reduced (remaining: macd, stochastic).

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/analytics/indicators.py trading/tests/analytics/
git commit -m "fix: EMA seeds from first value for openalgo-charts parity"
```

---

### Task 5: MACD rewrite (12/26/9 + signal + histogram)

**Files:**
- Modify: `trading/src/tradex_trading/analytics/indicators.py:157-182` (`macd`), `:494-501` (`_fn_macd`), `:530-536` (macd spec)
- Modify: `trading/src/tradex_trading/analytics/engine.py:24-26,52-53` (`_FUNCS`/`_DEFAULTS`/compute branch)
- Modify: `trading/src/tradex_trading/analytics/__init__.py:6` (re-export unchanged, verify)
- Test: `trading/tests/analytics/test_analytics.py:85-93,204-222` (rewrite macd tests)

**Interfaces:**
- Produces: `macd(values: list, fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, list]` with keys `"macd"`, `"signal"`, `"histogram"` (full-length lists, no padding).
- Registry `macd` params become `(fast, slow, signal)`; plots `(histogram|kind="histogram", macd|line, signal|line)`.
- Engine: `_DEFAULTS["macd"]` removed; the `elif indicator == 'macd'` branch routes through the registry fallback like other multi-plot indicators (delete lines 52-53 and the `_FUNCS["macd"]` entry — the registry fallback path at engine.py:109-117 already covers it).

- [ ] **Step 1: Rewrite the failing unit tests first (TDD)**

Replace `test_compute_macd`, `test_macd_is_fast_ema_minus_slow_ema`, `test_macd_rejects_small_period` in `test_analytics.py`:

```python
    def test_compute_macd(self) -> None:
        result = self.engine.compute(self.series, ['macd'])
        assert set(result['macd'].columns) >= {'macd', 'signal', 'histogram'}

    def test_macd_line_is_fast_minus_slow_ema(self) -> None:
        values = CLOSES_OR_FIXTURE  # reuse whatever closes list the module already defines
        result = macd(values)
        fast, slow = ema(values, 12), ema(values, 26)
        expected = [f - s for f, s in zip(fast, slow)]
        assert result["macd"] == pytest.approx(expected)

    def test_macd_signal_is_ema_of_macd_line(self) -> None:
        result = macd(CLOSES_OR_FIXTURE)
        assert result["signal"] == pytest.approx(ema(result["macd"], 9))

    def test_macd_histogram_is_line_minus_signal(self) -> None:
        result = macd(CLOSES_OR_FIXTURE)
        assert result["histogram"] == pytest.approx(
            [m - s for m, s in zip(result["macd"], result["signal"])]
        )

    def test_macd_rejects_nonpositive_periods(self) -> None:
        with pytest.raises(ValueError):
            macd([1.0, 2.0], fast=0)
```

(Adapt `CLOSES_OR_FIXTURE` to whichever closes fixture already exists in that test module.)

- [ ] **Step 2: Run to confirm red**

Run: `uv run pytest trading/tests/analytics/test_analytics.py -k macd -q`
Expected: FAIL (signature returns list / wrong periods).

- [ ] **Step 3: Implement**

Replace `macd()`:

```python
def macd(values: list, fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, list]:
    """MACD: EMA(fast) − EMA(slow), plus signal EMA and histogram (parity with
    openalgo-charts). Full-length lists, no warmup padding."""
    if min(fast, slow, signal) < 1:
        raise ValueError("periods must be positive")
    f = ema(values, fast)
    s = ema(values, slow)
    line = [a - b for a, b in zip(f, s, strict=True)]
    sig = ema(line, signal)
    hist = [m - g for m, g in zip(line, sig, strict=True)]
    return {"macd": line, "signal": sig, "histogram": hist}
```

Replace `_fn_macd` and the spec:

```python
    def _fn_macd(candles, fast, slow, signal):
        return macd(closes_only(candles), int(fast), int(slow), int(signal))
```

```python
        IndicatorSpec(
            id="macd", name="MACD", category="Momentum", placement="pane",
            params=(("fast", "int", 12), ("slow", "int", 26), ("signal", "int", 9)),
            plots=(
                ("histogram", "histogram", "Histogram"),
                ("macd", "line", "MACD"),
                ("signal", "line", "Signal"),
            ),
            levels=({"value": 0}),
            fn=_fn_macd,
        ),
```

Update `engine.py`: delete the `"macd": macd,` entry from `_FUNCS`, delete the `elif indicator == 'macd':` branch (lines 52-53), remove `"macd": 26` from `_DEFAULTS`. The registry fallback (engine.py:109-117) computes multi-plot results already — verify `indicator_values('macd')` picks the first plot (`macd`) via the existing fallback logic, and adjust only if that path assumes a `"value"` key (read engine.py:100-125 during execution and make the fallback take `next(iter(result))` if it hardcodes `"value"`).

Also update `__all__`/docstring reference to MACD in `indicators.py` if it mentions `period // 2` (line 161 comment goes away with the rewrite).

- [ ] **Step 4: Run to green**

Run: `uv run pytest trading/tests/analytics/ trading/tests/interface/test_chart_indicators.py -q`
Expected: all pass including `macd` golden parity.

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/analytics/ trading/tests/analytics/
git commit -m "fix: standard MACD (12/26/9) with signal + histogram, TS-parity"
```

---

### Task 6: Stochastic smooth_k

**Files:**
- Modify: `trading/src/tradex_trading/analytics/indicators.py:291-314` (`stochastic`), stochastic spec (`:568-574`)
- Test: `trading/tests/analytics/test_indicator_registry.py:130-140` (update alignment assertions)

**Interfaces:**
- Produces: `stochastic(candles, k_period=14, smooth_k=3, d_period=3) -> dict[str, list]` keys `"k"/"d"` — %K is SMA(smooth_k) of raw %K; %D is SMA(d_period) of %K. Raw %K is `None` where high==low (TS: span<=0 → NaN → null; current backend wrongly emits 0.0).
- Registry params: `(("k_period","int",14), ("smooth_k","int",3), ("d_period","int",3))`; plot keys unchanged.

- [ ] **Step 1: Add a local rolling-SMA helper matching TS semantics (finite-only windows)**

Place above `stochastic`:

```python
def _rolling_sma(values: list[float | None], period: int) -> list[float | None]:
    """SMA that yields None unless ALL window entries are finite (TS calc.ts semantics)."""
    n = len(values)
    out: list[float | None] = [None] * n
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sum(window) / period
    return out
```

- [ ] **Step 2: Update the failing test first**

Rewrite `test_stochastic_bounds_and_alignment`:

```python
    def test_stochastic_smoothed_and_aligned(self):
        candles = _candles_from_closes(CLOSES)
        result = stochastic(candles, k_period=5, smooth_k=3, d_period=3)
        n = len(CLOSES)
        assert len(result["k"]) == n and len(result["d"]) == n
        for v in result["k"]:
            if v is not None:
                assert 0.0 <= v <= 100.0
        # raw %K starts at index 4; smoothed %K needs 2 more; %D 2 more again
        assert result["k"][5] is not None
        assert result["k"][4] is None
        assert result["d"][7] is not None
        assert result["d"][6] is None

    def test_stochastic_flat_window_is_none_not_zero(self):
        candles = [_candle(10, 10, 10, 10)] * 8
        result = stochastic(candles, k_period=3, smooth_k=1, d_period=1)
        assert all(v is None for v in result["k"])
```

- [ ] **Step 3: Run red, then implement**

Run: `uv run pytest trading/tests/analytics/test_indicator_registry.py -k stochastic -q` → FAIL.

Replace the body after computing raw `k` (keep the existing raw loop but emit `None` instead of `0.0` when `rng == 0`):

```python
    raw: list[float | None] = [None] * n
    for i in range(k_period - 1, n):
        window = candles[i - k_period + 1 : i + 1]
        hh = max(_to_float(c.ohlc.high.value) for c in window)
        ll = min(_to_float(c.ohlc.low.value) for c in window)
        close = _to_float(candles[i].ohlc.close.value)
        rng_span = hh - ll
        raw[i] = None if rng_span <= 0 else ((close - ll) / rng_span) * 100.0
    k = _rolling_sma(raw, smooth_k)
    d = _rolling_sma(k, d_period)
    return {"k": k, "d": d}
```

Spec change:

```python
            params=(("k_period", "int", 14), ("smooth_k", "int", 3), ("d_period", "int", 3)),
```

- [ ] **Step 4: Run to green**

Run: `uv run pytest trading/tests/analytics/ trading/tests/interface/ -q`
Expected: all pass including `stochastic` golden parity. (Scanner/engine paths using stochastic get smoother %K — scanner tests use their own fixtures and tolerate this; fix any that asserted exact old values.)

- [ ] **Step 5: Commit**

```bash
git add trading/src/tradex_trading/analytics/ trading/tests/
git commit -m "fix: stochastic smoothK + None on zero-range windows, TS-parity"
```

---

### Task 7: Full-suite gate + batch close-out

**Files:**
- No source changes unless the gate finds breakage outside analytics.

- [ ] **Step 1: Run the entire test suite**

Run: `uv run pytest trading/tests/ tests/ -q`
Expected: all pass. Fix any downstream consumer that depended on old MACD/stoch/EMA shapes (likely candidates: `strategy/core/scanner.py` fixtures, `scripts/backtest_datalake.py` smoke, chart route tests).

- [ ] **Step 2: Confirm parity summary**

Run: `uv run pytest trading/tests/analytics/test_golden_parity.py -v 2>&1 | tail -20`
Expected: sma, ema, rsi, roc, macd, bollinger, atr, obv, stochastic PASS; vwap/supertrend PASS or documented KNOWN_DIVERGENCES skip; all later-batch ids skipped as "not yet ported".

- [ ] **Step 3: Commit close-out**

```bash
git add -A
git commit -m "test: batch 0 complete — registry indicators bit-exact with openalgo-charts"
```

---

## Self-review notes

- Spec coverage: harness ✓ (Tasks 1-2), MACD ✓ (5), smoothK ✓ (6), EMA seed ✓ (4), parity gate ✓ (3), suite-green rule ✓ (7). Later batches are separate plans by design (spec §Batches).
- Type consistency: `macd` returns dict everywhere after Task 5 (engine branch deleted so no list/dict mixing); `stochastic` keeps dict contract; `ema` list-without-padding documented at the single definition site.
- Known risk called out in Task 3: vwap session-anchor semantics may legitimately differ across implementations — explicit allowlist escape hatch with mandatory justification, everything else must be fixed.
