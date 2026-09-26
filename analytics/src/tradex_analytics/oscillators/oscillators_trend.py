"""Oscillators & trend — ADX/DMI, Aroon, Aroon Oscillator, Awesome Oscillator, CCI.

Batch 2 parallel split: this module is intentionally separate from
``indicators.py`` so four indicator-port tasks can proceed concurrently
without merge conflicts. A later merge task registers the ``SPEC_*``
objects below into the shared registry and adds the golden ``PARAM_MAP``
entries.

Helpers are imported from ``indicators.py`` — never redefined here.
Wilder's RMA is implemented locally to match ``calc.ts::rma`` exactly
(seed = SMA of first ``period`` values, then ``(prev*(p-1)+v)/p``) with
explicit NaN propagation; it is deliberately kept distinct from the
canonical ``indicators._rma`` (which does not propagate NaN).

Parity notes (openalgo-charts ``src/indicators/momentum.ts`` / ``oscillators.ts`` / ``calc.ts``):
- ADX: TR/+DM/-DM smoothed by RMA(period); +DI/-DI = 100*RMADM/RMATR;
  DX = 100*|DI+ - DI-|/(DI+ + DI-); ADX = RMA(DX, adxPeriod) from first
  finite DX.
- Aroon: highestBars/lowestBars over ``length+1`` bars, scaled to 0..100
  via ``100*(offset+length)/length``; Aroon-Osc = 100*(upBars-downBars)/length.
- AO: SMA(hl2,5) - SMA(hl2,34), fixed periods.
- CCI: (TP - SMA(TP,period)) / (k * meanDev), meanDev = mean(|TP - SMA|);
  zero when meanDev == 0; ``ma`` is the maType kernel over the CCI line
  (fromFirstValue) with Bollinger companions only for that kernel.
"""

from __future__ import annotations

from tradex_analytics.indicators import (
    IndicatorSpec,
    LEGACY_PARAM_ALIASES,
    _from_first_value,
    _smoothing_ma,
    _stdev,
    _to_float,
    sma,
    true_ranges,
)

LEGACY_PARAM_ALIASES["adx"] = {"adx_period": "adxPeriod"}
LEGACY_PARAM_ALIASES["cci"] = {
    "ma_type": "maType",
    "ma_length": "maLength",
    "bb_mult": "bbMult",
}

# ---------------------------------------------------------------------------
# Local Wilder RMA — matches calc.ts::rma (NaN -> None, seed = SMA)
# ---------------------------------------------------------------------------

def _rma(values: list[float], period: int) -> list[float | None]:
    """Wilder's RMA: seed is SMA of first ``period`` values, then
    ``(prev*(period-1)+v)/period``. None before index ``period-1``."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    # TS sums raw values (no finiteness guard) — here inputs are finite.
    s = sum(values[:period])
    prev = s / period
    out[period - 1] = prev
    for i in range(period, n):
        v = values[i]
        # Propagate NaN like TS would (NaN arithmetic -> NaN)
        if isinstance(v, float) and v != v:  # nan
            prev = float("nan")
        elif prev != prev:  # prev is nan
            prev = float("nan")
        else:
            prev = (prev * (period - 1) + v) / period
        out[i] = prev
    # Convert nan to None for parity with None-padded backend
    for i, val in enumerate(out):
        if isinstance(val, float) and val != val:
            out[i] = None
    return out


def _highest_bars(values: list[float], period: int) -> list[float | None]:
    """Reference ``highestBars``: offset to highest bar, 0 = current."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        best = values[i]
        at = 0
        for k in range(1, period):
            v = values[i - k]
            if v > best:
                best = v
                at = k
        out[i] = 0 if at == 0 else -at
    return out


def _lowest_bars(values: list[float], period: int) -> list[float | None]:
    """Reference ``lowestBars``: offset to lowest bar."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        best = values[i]
        at = 0
        for k in range(1, period):
            v = values[i - k]
            if v < best:
                best = v
                at = k
        out[i] = 0 if at == 0 else -at
    return out


# ---------------------------------------------------------------------------
# Indicator functions
# ---------------------------------------------------------------------------

def adx(
    candles: list,
    period: int = 14,
    adx_period: int = 14,
) -> dict[str, list]:
    """ADX / DMI — Wilder's ADX matching ``momentum.ts::ADX``.

    Returns dict keyed ``plusDi`` / ``minusDi`` / ``adx`` (None-padded).
    """
    if period <= 0 or adx_period <= 0:
        raise ValueError("period must be positive")
    n = len(candles)
    if n == 0:
        return {"plusDi": [], "minusDi": [], "adx": []}
    highs = [_to_float(c.ohlc.high.value) for c in candles]
    lows = [_to_float(c.ohlc.low.value) for c in candles]
    tr = true_ranges(candles)
    plus_dm: list[float] = [0.0] * n
    minus_dm: list[float] = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0

    tr = true_ranges(candles)
    # Engine parity (momentum.ts): bar 0 has no true range — the Wilder TR
    # seed window is tr[1..period], so smoothing starts at global index 1.
    tr_r: list[float | None] = [None] * n
    if n > 1:
        tail = _rma([float(v) for v in tr[1:]], int(period))
        for j, val in enumerate(tail):
            tr_r[1 + j] = val
    plus_r = _rma(plus_dm, int(period))
    minus_r = _rma(minus_dm, int(period))

    plus_di: list[float | None] = [None] * n
    minus_di: list[float | None] = [None] * n
    dx: list[float | None] = [None] * n
    held_p: float | None = None
    held_m: float | None = None
    for i in range(n):
        trv = tr_r[i]
        # Zero range holds the previous DI (engine hold-last rule); a None
        # range leaves everything gapped.
        if trv is not None and trv != 0:
            pr = plus_r[i]
            mr = minus_r[i]
            if pr is not None and mr is not None:
                held_p = (pr / trv) * 100.0
                held_m = (mr / trv) * 100.0
        if held_p is None or held_m is None:
            continue
        plus_di[i] = held_p
        minus_di[i] = held_m
        s = held_p + held_m
        dx[i] = 0.0 if s == 0 else (abs(held_p - held_m) / s) * 100.0

    # ADX = RMA(DX) from first finite DX
    adx_out: list[float | None] = [None] * n
    start = -1
    for i, v in enumerate(dx):
        if v is not None:
            start = i
            break
    if start >= 0:
        # Slice from first finite DX onward — all finite in practice
        slice_vals: list[float] = []
        for i in range(start, n):
            v = dx[i]
            # Preserve None as nan to mirror TS NaN propagation if gap exists
            if v is None:
                slice_vals.append(float("nan"))
            else:
                slice_vals.append(float(v))
        smoothed = _rma(slice_vals, int(adx_period))
        for j, val in enumerate(smoothed):
            adx_out[start + j] = val

    return {"plusDi": plus_di, "minusDi": minus_di, "adx": adx_out}


def aroon(candles: list, length: int = 14) -> dict[str, list]:
    """Aroon Up/Down — matches ``oscillators.ts::AROON``."""
    if length <= 0:
        raise ValueError("length must be positive")
    n = len(candles)
    if n == 0:
        return {"up": [], "down": []}
    highs = [_to_float(c.ohlc.high.value) for c in candles]
    lows = [_to_float(c.ohlc.low.value) for c in candles]
    period = int(length) + 1
    up_bars = _highest_bars(highs, period)
    down_bars = _lowest_bars(lows, period)
    up: list[float | None] = [None] * n
    down: list[float | None] = [None] * n
    for i in range(n):
        ub = up_bars[i]
        if ub is not None:
            up[i] = (100.0 * (ub + length)) / length
        db = down_bars[i]
        if db is not None:
            down[i] = (100.0 * (db + length)) / length
    return {"up": up, "down": down}


def aroon_oscillator(candles: list, length: int = 14) -> dict[str, list]:
    """Aroon Oscillator — Up minus Down, matches ``oscillators.ts::AROON_OSCILLATOR``."""
    if length <= 0:
        raise ValueError("length must be positive")
    n = len(candles)
    if n == 0:
        return {"osc": []}
    highs = [_to_float(c.ohlc.high.value) for c in candles]
    lows = [_to_float(c.ohlc.low.value) for c in candles]
    period = int(length) + 1
    up_bars = _highest_bars(highs, period)
    down_bars = _lowest_bars(lows, period)
    osc: list[float | None] = [None] * n
    for i in range(n):
        ub = up_bars[i]
        db = down_bars[i]
        if ub is not None and db is not None:
            osc[i] = (100.0 * (ub - db)) / length
    return {"osc": osc}


def awesome_oscillator(candles: list) -> dict[str, list]:
    """Awesome Oscillator — SMA(hl2,5) - SMA(hl2,34)."""
    n = len(candles)
    if n == 0:
        return {"ao": []}
    hl2 = [(_to_float(c.ohlc.high.value) + _to_float(c.ohlc.low.value)) / 2.0 for c in candles]
    fast = sma(hl2, 5)
    slow = sma(hl2, 34)
    out: list[float | None] = [None] * n
    for i in range(n):
        f = fast[i]
        s = slow[i]
        if f is not None and s is not None:
            out[i] = f - s
    return {"ao": out}


def cci(
    candles: list,
    period: int = 20,
    constant: float = 0.015,
    ma_type: str = "SMA",
    ma_length: int = 20,
    bb_mult: float = 2.0,
) -> dict[str, list]:
    """Commodity Channel Index plus its engine smoothing companions.

    Base ``cci`` matches ``momentum.ts::CCI`` without the optional smoothing
    block: ``(TP - SMA(TP)) / (k * meanDev)`` where ``meanDev =
    mean(|TP - SMA|)``. Zero when meanDev == 0. ``ma`` is the ``ma_type``
    kernel over the CCI line (all-None when ``ma_type`` is ``'None'``);
    ``bbUpper``/``bbLower`` exist only for the ``'SMA + Bollinger Bands'``
    kernel (``fromFirstValue`` stdev offset scaled by ``bb_mult``),
    otherwise all-None. Returns dict keyed ``cci``/``ma``/``bbUpper``/
    ``bbLower``.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(candles)
    if n == 0:
        return {"cci": [], "ma": [], "bbUpper": [], "bbLower": []}
    tp = [
        (_to_float(c.ohlc.high.value) + _to_float(c.ohlc.low.value) + _to_float(c.ohlc.close.value)) / 3.0
        for c in candles
    ]
    avg = sma(tp, int(period))
    out: list[float | None] = [None] * n
    k = float(constant)
    for i in range(int(period) - 1, n):
        a = avg[i]
        if a is None:
            continue
        dev = 0.0
        for j in range(int(period)):
            dev += abs(tp[i - j] - a)
        md = dev / int(period)
        if md < 1e-12:
            out[i] = 0.0
        else:
            out[i] = (tp[i] - a) / (k * md) if k != 0 else 0.0
    mt = str(ma_type)
    length = max(1, int(ma_length))
    vols = [float(c.volume.value) for c in candles]
    if mt == "None":
        ma: list[float | None] = [None] * n
    else:
        ma = _smoothing_ma(mt, out, vols, length)
    if mt == "SMA + Bollinger Bands":
        mult = float(bb_mult)
        band: list[float | None] = _from_first_value(
            out,
            lambda tail, _s: [
                None if v is None else v * mult for v in _stdev(tail, length)
            ],
        )
    else:
        band = [None] * n
    return {
        "cci": out,
        "ma": ma,
        "bbUpper": [
            None if a is None or b is None else a + b
            for a, b in zip(ma, band, strict=True)
        ],
        "bbLower": [
            None if a is None or b is None else a - b
            for a, b in zip(ma, band, strict=True)
        ],
    }


# ---------------------------------------------------------------------------
# Specs — ready for registry import; NOT registered here (merge task wires them).
# ---------------------------------------------------------------------------

def _fn_adx(candles, period, adxPeriod):
    return adx(candles, int(period), int(adxPeriod))


def _fn_aroon(candles, length):
    return aroon(candles, int(length))


def _fn_aroon_oscillator(candles, length):
    return aroon_oscillator(candles, int(length))


def _fn_awesome(candles):
    return awesome_oscillator(candles)


def _fn_cci(candles, period, constant, maType, maLength, bbMult):
    return cci(candles, int(period), float(constant), str(maType), int(maLength), float(bbMult))


SPEC_ADX = IndicatorSpec(
    id="adx",
    name="ADX / DMI",
    category="Trend",
    placement="pane",
    params=(("period", "int", 14), ("adxPeriod", "int", 14)),
    plots=(
        ("plusDi", "line", "+DI"),
        ("minusDi", "line", "-DI"),
        ("adx", "line", "ADX"),
    ),
    levels=({"value": 25},),
    fn=_fn_adx,
)

SPEC_AROON = IndicatorSpec(
    id="aroon",
    name="Aroon",
    category="Trend",
    placement="pane",
    params=(("length", "int", 14),),
    plots=(
        ("up", "line", "Aroon Up"),
        ("down", "line", "Aroon Down"),
    ),
    fn=_fn_aroon,
)

SPEC_AROON_OSCILLATOR = IndicatorSpec(
    id="aroon-oscillator",
    name="Aroon Oscillator",
    category="Trend",
    placement="pane",
    params=(("length", "int", 14),),
    plots=(("osc", "line", "Oscillator"),),
    levels=({"value": 0},),
    fn=_fn_aroon_oscillator,
)

SPEC_AWESOME_OSCILLATOR = IndicatorSpec(
    id="awesome-oscillator",
    name="Awesome Oscillator",
    category="Momentum",
    placement="pane",
    params=(),
    plots=(("ao", "column", "AO"),),
    levels=({"value": 0},),
    fn=_fn_awesome,
)

SPEC_CCI = IndicatorSpec(
    id="cci",
    name="CCI",
    category="Momentum",
    placement="pane",
    params=(
        ("period", "int", 20),
        ("constant", "float", 0.015),
        ("maType", "select", "SMA"),
        ("maLength", "int", 20),
        ("bbMult", "float", 2.0),
    ),
    plots=(
        ("cci", "line", "CCI"),
        ("ma", "line", "CCI-based MA"),
        ("bbUpper", "line", "Upper Bollinger Band"),
        ("bbLower", "line", "Lower Bollinger Band"),
    ),
    levels=({"value": 100}, {"value": 0}, {"value": -100}),
    fn=_fn_cci,
)
