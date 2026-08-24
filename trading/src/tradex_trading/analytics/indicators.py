"""Technical indicators — stdlib implementation with optional numpy acceleration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

NumericValue = float | Decimal


def _to_float(value: NumericValue) -> float:
    """Convert Decimal or float to float."""
    if isinstance(value, Decimal):
        return float(value)
    return value


def sma(values: list, period: int) -> list:
    """Simple Moving Average.

    Args:
        values: List of numeric values (Decimal or float)
        period: Window size

    Returns:
        List of SMA values (same length as input, None-padded at start)
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return [None] * len(values)

    floats = [_to_float(v) for v in values]
    result: list[float | None] = [None] * (period - 1)

    window_sum = sum(floats[:period])
    result.append(window_sum / period)

    for i in range(period, len(floats)):
        window_sum += floats[i] - floats[i - period]
        result.append(window_sum / period)

    return result


def ema(values: list, period: int) -> list:
    """Exponential Moving Average.

    Args:
        values: List of numeric values (Decimal or float)
        period: Window size

    Returns:
        List of EMA values (same length as input, None-padded at start)
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return [None] * len(values)

    floats = [_to_float(v) for v in values]
    result: list[float | None] = [None] * (period - 1)

    # Initial SMA for first EMA value
    initial_sma = sum(floats[:period]) / period
    result.append(initial_sma)

    multiplier = 2.0 / (period + 1)
    prev_ema = initial_sma

    for i in range(period, len(floats)):
        current_ema = (floats[i] - prev_ema) * multiplier + prev_ema
        result.append(current_ema)
        prev_ema = current_ema

    return result


def rsi(values: list, period: int = 14) -> list:
    """Relative Strength Index.

    Args:
        values: List of numeric values (Decimal or float)
        period: Lookback period (default: 14)

    Returns:
        List of RSI values (0-100, None-padded at start)
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period + 1:
        return [None] * len(values)

    floats = [_to_float(v) for v in values]
    result: list[float | None] = [None] * period

    # Calculate gains and losses
    gains = []
    losses = []
    for i in range(1, len(floats)):
        change = floats[i] - floats[i - 1]
        gains.append(max(0, change))
        losses.append(max(0, -change))

    # Initial average gain/loss
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        result.append(100.0)
    else:
        rs = avg_gain / avg_loss
        result.append(100.0 - (100.0 / (1.0 + rs)))

    # Smoothed averages
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(100.0 - (100.0 / (1.0 + rs)))

    return result


def roc(values: list, period: int = 10) -> list:
    """Rate of Change — percentage change over *period* bars.

    Args:
        values: List of numeric values (Decimal or float)
        period: Lookback period (default: 10)

    Returns:
        List of ROC percentages (None-padded at start; 0.0 for flat base)
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period + 1:
        return [None] * len(values)

    floats = [_to_float(v) for v in values]
    result: list[float | None] = [None] * period
    for i in range(period, len(floats)):
        base = floats[i - period]
        if base == 0:
            result.append(0.0)
        else:
            result.append(((floats[i] - base) / base) * 100.0)
    return result


def macd(values: list, period: int = 26) -> list:
    """MACD line — fast EMA minus slow EMA.

    The uniform indicator signature takes a single ``period``; here it is the
    slow EMA period and the fast EMA is ``period // 2`` (e.g. 26 -> fast 13),
    giving the standard fast/slow spread without extra parameters.

    Args:
        values: List of numeric values (Decimal or float)
        period: Slow EMA period (default: 26)

    Returns:
        List of MACD-line values (None-padded at start)
    """
    if period < 2:
        raise ValueError("period must be at least 2")
    fast_period = max(2, period // 2)
    fast = ema(values, fast_period)
    slow = ema(values, period)
    result: list[float | None] = []
    for f, s in zip(fast, slow, strict=True):
        if f is None or s is None:
            result.append(None)
        else:
            result.append(f - s)
    return result


__all__ = [
    "atr",
    "bollinger",
    "ema",
    "macd",
    "obv",
    "roc",
    "rsi",
    "sma",
    "stochastic",
    "supertrend",
    "vwap_session",
]


def true_ranges(candles: list) -> list[float | None]:
    """True range per candle: max(H-L, |H-prevC|, |L-prevC|); first is None."""
    if not candles:
        return []
    out: list[float | None] = [None]
    for i in range(1, len(candles)):
        h = _to_float(candles[i].ohlc.high.value)
        low = _to_float(candles[i].ohlc.low.value)
        pc = _to_float(candles[i - 1].ohlc.close.value)
        out.append(max(h - low, abs(h - pc), abs(low - pc)))
    return out


def atr(candles: list, period: int = 14) -> list[float | None]:
    """Average True Range (Wilder smoothing).

    Args:
        candles: Candle objects exposing ``.ohlc.high/.low/.close`` (Decimal)
        period: Smoothing window (default 14)

    Returns:
        ATR values (None until the first full window)
    """
    if period <= 0:
        raise ValueError("period must be positive")
    trs = true_ranges(candles)
    # TR[0] is None; the seeded average consumes the first `period` finite TRs
    # (candles 1..period), so the first ATR lands exactly on index `period`.
    finite = [t for t in trs[1:] if t is not None]
    n = len(candles)
    if len(finite) < period or n <= period:
        return [None] * n
    out: list[float | None] = [None] * period
    prev = sum(finite[:period]) / period
    out.append(prev)
    for i in range(period + 1, n):
        tr = trs[i]
        value = tr if tr is not None else 0.0
        prev = (prev * (period - 1) + value) / period
        out.append(prev)
    return out


def bollinger(values: list, period: int = 20, num_std: float = 2.0) -> dict[str, list]:
    """Bollinger Bands: middle SMA with upper/lower at +-num_std deviations.

    Returns dict of parallel lists keyed 'upper'/'middle'/'lower', each
    None-padded for the warmup region.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    mid = sma(values, period)
    floats = [_to_float(v) for v in values]
    n = len(floats)
    upper: list[float | None] = [None] * n
    lower: list[float | None] = [None] * n
    for i in range(period - 1, n):
        window = floats[i - period + 1 : i + 1]
        m = mid[i]
        if m is None:
            continue
        variance = sum((x - m) ** 2 for x in window) / period
        sd = variance ** 0.5
        offset = num_std * sd
        upper[i] = m + offset
        lower[i] = m - offset
    return {"upper": upper, "middle": mid, "lower": lower}


def obv(candles: list) -> list[float]:
    """On-Balance Volume: cumulative volume signed by close-to-close direction.

    Takes candles (needs close and volume); output has no warmup padding —
    the first bar contributes 0.
    """
    if not candles:
        return []
    out = [0.0]
    for i in range(1, len(candles)):
        close = _to_float(candles[i].ohlc.close.value)
        prev_close = _to_float(candles[i - 1].ohlc.close.value)
        vol = float(candles[i].volume.value)
        if close > prev_close:
            out.append(out[-1] + vol)
        elif close < prev_close:
            out.append(out[-1] - vol)
        else:
            out.append(out[-1])
    return out


def stochastic(candles: list, k_period: int = 14, d_period: int = 3) -> dict[str, list]:
    """Stochastic Oscillator: %K raw and %D (SMA of %K), both 0-100.

    Returns dict keyed 'k'/'d'.
    """
    if k_period <= 0 or d_period <= 0:
        raise ValueError("periods must be positive")
    n = len(candles)
    k: list[float | None] = [None] * n
    for i in range(k_period - 1, n):
        window = candles[i - k_period + 1 : i + 1]
        hh = max(_to_float(c.ohlc.high.value) for c in window)
        ll = min(_to_float(c.ohlc.low.value) for c in window)
        close = _to_float(candles[i].ohlc.close.value)
        rng = hh - ll
        k[i] = 0.0 if rng == 0 else ((close - ll) / rng) * 100.0
    # %D: rolling SMA over the defined %K values only (skip the warmup pad).
    d: list[float | None] = [None] * n
    start = k_period - 1
    for i in range(start + d_period - 1, n):
        window = [v for v in k[i - d_period + 1 : i + 1] if v is not None]
        if len(window) == d_period:
            d[i] = sum(window) / d_period
    return {"k": k, "d": d}


def supertrend(candles: list, period: int = 10, multiplier: float = 3.0) -> dict[str, list]:
    """Supertrend line with its direction flag (+1 up / -1 down).

    Returns dict keyed 'line'/'direction'. Direction flips when price closes
    through the band; None-padded until the first ATR is available.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(candles)
    atrs = atr(candles, period)
    line: list[float | None] = [None] * n
    direction: list[int | None] = [None] * n
    upper_band: float | None = None
    lower_band: float | None = None
    started = False
    for i in range(n):
        a = atrs[i]
        if a is None:
            continue
        h = _to_float(candles[i].ohlc.high.value)
        low = _to_float(candles[i].ohlc.low.value)
        c = _to_float(candles[i].ohlc.close.value)
        mid = (h + low) / 2.0
        ub = mid + multiplier * a
        lb = mid - multiplier * a
        if not started:
            upper_band, lower_band = ub, lb
            direction[i] = 1
            line[i] = lb
            started = True
            continue
        assert upper_band is not None and lower_band is not None
        # Bands ratchet: never loosen against an established trend.
        pc = _to_float(candles[i - 1].ohlc.close.value)
        upper_band = ub if (ub < upper_band or pc > upper_band) else upper_band
        lower_band = lb if (lb > lower_band or pc < lower_band) else lower_band
        prev_dir = direction[i - 1]
        if prev_dir == 1:
            direction[i] = -1 if c < lower_band else 1
        else:
            direction[i] = 1 if c > upper_band else -1
        line[i] = lower_band if direction[i] == 1 else upper_band
    return {"line": line, "direction": direction}


def vwap_session(candles: list) -> list[float | None]:
    """VWAP anchored to each session start (09:15 IST calendar day boundary).

    Cumulative (typical price * volume) / cumulative volume, resetting at
    each new session day. Needs candles with volume; None where volume is 0.
    """
    out: list[float | None] = []
    cum_pv = 0.0
    cum_v = 0.0
    current_day = None
    for c in candles:
        ts = c.timestamp
        day = ts.date()
        if day != current_day:
            current_day = day
            cum_pv = 0.0
            cum_v = 0.0
        typical = (
            _to_float(c.ohlc.high.value)
            + _to_float(c.ohlc.low.value)
            + _to_float(c.ohlc.close.value)
        ) / 3.0
        v = float(c.volume.value)
        cum_pv += typical * v
        cum_v += v
        out.append(cum_pv / cum_v if cum_v > 0 else None)
    return out


# ---------------------------------------------------------------------------
# Registry — single source of truth for what the chart frontend may compute.
#
# Each spec describes one indicator the way the chart's catalogue endpoint
# serves it: id, display name, category, placement (overlay draws on the
# price pane; pane gets its own sub-pane), parameter schema with defaults,
# and the plots the computation returns. The frontend builds its whole
# indicator menu from this catalogue; adding an entry here is the entire
# act of shipping a new indicator to the UI.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """One registered indicator: metadata + callable + plot shape."""

    id: str
    name: str
    category: str
    placement: str  # 'overlay' | 'pane'
    params: tuple[tuple[str, str, Any], ...]  # (name, type, default)
    plots: tuple[tuple[str, str, str], ...]  # (plot key, kind, title)
    levels: tuple[dict[str, Any], ...] = ()  # fixed reference lines
    fn: Callable[..., Any] | None = None


@dataclass(slots=True)
class IndicatorResult:
    """Aligned computation output: one value-list per plot key."""

    values: dict[str, list] = field(default_factory=dict)


_REGISTRY: dict[str, IndicatorSpec] = {}


def register_indicator(spec: IndicatorSpec) -> None:
    """Register an indicator spec by id. Last registration wins."""
    _REGISTRY[spec.id] = spec


def get_indicator_spec(indicator_id: str) -> IndicatorSpec | None:
    """Look up a spec; None for unknown ids (callers decide fail-loudness)."""
    return _REGISTRY.get(indicator_id)


def indicator_catalogue() -> list[dict[str, Any]]:
    """Serialisable catalogue for the chart frontend's menu."""
    return [
        {
            "id": s.id,
            "name": s.name,
            "category": s.category,
            "placement": s.placement,
            "params": [{"name": p[0], "type": p[1], "default": p[2]} for p in s.params],
            "plots": [{"key": p[0], "kind": p[1], "title": p[2]} for p in s.plots],
            "levels": list(s.levels),
        }
        for s in _REGISTRY.values()
    ]


def compute_indicator(
    indicator_id: str,
    candles: list,
    params: dict[str, Any] | None = None,
) -> dict[str, list]:
    """Compute a registered indicator over candles; returns per-plot lists.

    Params are validated against the spec: unknown names are rejected rather
    than silently ignored, so a frontend typo fails loudly instead of
    computing defaults that look right.
    """
    spec = _REGISTRY.get(indicator_id)
    if spec is None or spec.fn is None:
        raise ValueError(f"unknown indicator: {indicator_id!r}")
    supplied = dict(params or {})
    known = {p[0] for p in spec.params}
    unknown = set(supplied) - known
    if unknown:
        raise ValueError(f"unknown params for {indicator_id}: {sorted(unknown)}")
    merged = {p[0]: supplied.get(p[0], p[2]) for p in spec.params}
    result = spec.fn(candles, **merged)
    if isinstance(result, dict):
        return result
    return {"value": result}


def _builtin_specs() -> list[IndicatorSpec]:
    closes_only = lambda candles: [_to_float(c.ohlc.close.value) for c in candles]  # noqa: E731

    def _fn_sma(candles, period):
        return sma(closes_only(candles), int(period))

    def _fn_ema(candles, period):
        return ema(closes_only(candles), int(period))

    def _fn_rsi(candles, period):
        return rsi(closes_only(candles), int(period))

    def _fn_roc(candles, period):
        return roc(closes_only(candles), int(period))

    def _fn_macd(candles, period):
        fast_period = max(2, int(period) // 2)
        f = ema(closes_only(candles), fast_period)
        s = ema(closes_only(candles), int(period))
        return [
            None if (a is None or b is None) else a - b
            for a, b in zip(f, s, strict=True)
        ]

    return [
        IndicatorSpec(
            id="sma", name="SMA", category="Trend", placement="overlay",
            params=(("period", "int", 20),),
            plots=(("value", "line", "SMA"),),
            fn=_fn_sma,
        ),
        IndicatorSpec(
            id="ema", name="EMA", category="Trend", placement="overlay",
            params=(("period", "int", 20),),
            plots=(("value", "line", "EMA"),),
            fn=_fn_ema,
        ),
        IndicatorSpec(
            id="rsi", name="RSI", category="Momentum", placement="pane",
            params=(("period", "int", 14),),
            plots=(("value", "line", "RSI"),),
            levels=({"value": 70}, {"value": 30}),
            fn=_fn_rsi,
        ),
        IndicatorSpec(
            id="roc", name="ROC", category="Momentum", placement="pane",
            params=(("period", "int", 10),),
            plots=(("value", "line", "ROC"),),
            levels=({"value": 0}),
            fn=_fn_roc,
        ),
        IndicatorSpec(
            id="macd", name="MACD", category="Momentum", placement="pane",
            params=(("period", "int", 26),),
            plots=(("value", "line", "MACD"),),
            levels=({"value": 0}),
            fn=_fn_macd,
        ),
        IndicatorSpec(
            id="bollinger", name="Bollinger Bands", category="Volatility",
            placement="overlay",
            params=(("period", "int", 20), ("num_std", "float", 2.0)),
            plots=(
                ("upper", "line", "Upper"),
                ("middle", "line", "Middle"),
                ("lower", "line", "Lower"),
            ),
            fn=lambda candles, period, num_std: bollinger(
                closes_only(candles), int(period), float(num_std)
            ),
        ),
        IndicatorSpec(
            id="atr", name="ATR", category="Volatility", placement="pane",
            params=(("period", "int", 14),),
            plots=(("value", "line", "ATR"),),
            fn=atr,
        ),
        IndicatorSpec(
            id="vwap", name="VWAP (session)", category="Volume", placement="overlay",
            params=(),
            plots=(("value", "line", "VWAP"),),
            fn=vwap_session,
        ),
        IndicatorSpec(
            id="obv", name="OBV", category="Volume", placement="pane",
            params=(),
            plots=(("value", "line", "OBV"),),
            fn=obv,
        ),
        IndicatorSpec(
            id="stochastic", name="Stochastic", category="Momentum", placement="pane",
            params=(("k_period", "int", 14), ("d_period", "int", 3)),
            plots=(("k", "line", "%K"), ("d", "line", "%D"),),
            levels=({"value": 80}, {"value": 20}),
            fn=stochastic,
        ),
        IndicatorSpec(
            id="supertrend", name="Supertrend", category="Trend", placement="overlay",
            params=(("period", "int", 10), ("multiplier", "float", 3.0)),
            plots=(("line", "line", "Supertrend"),),
            fn=supertrend,
        ),
    ]


for _spec in _builtin_specs():
    register_indicator(_spec)
