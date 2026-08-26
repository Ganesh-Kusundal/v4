"""Volume/time averages — VWMA, TWAP, McGinley Dynamic, LSMA.

Batch 1 parallel split of the indicator-parity port (openalgo-charts
``src/indicators/averages.ts`` and ``adaptive.ts``): this module stands alone
so it can be developed beside ``indicators.py`` without merge conflicts. A
later merge task wires the SPEC_* objects below into the registry in
``indicators.py``; until then they are exported but NOT registered.

Helpers are imported from ``indicators.py`` — never redefined here.
"""

from __future__ import annotations

import math

from .indicators import IndicatorSpec, _rolling_sma, _sma_seeded_ema

__all__ = [
    "SPEC_LSMA",
    "SPEC_MCGINLEY_DYNAMIC",
    "SPEC_TWAP",
    "SPEC_VWMA",
    "lsma",
    "mcginley",
    "twap",
    "vwma",
]


def vwma(values: list[float], volumes: list[float], period: int) -> list[float | None]:
    """Volume Weighted Moving Average (openalgo-charts parity).

    Matches openalgo-charts ``vwma`` (src/indicators/calc.ts):
    ``sma(src * volume, len) / sma(volume, len)``; a window whose volume sums
    to zero yields None at that slot (TS NaN -> null), which is what a feed
    with no volume produces.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(values)
    pv = [v * (volumes[i] if i < len(volumes) else 0.0) for i, v in enumerate(values)]
    num = _rolling_sma(pv, period)
    den = _rolling_sma(volumes, period)
    out: list[float | None] = [None] * n
    for i in range(n):
        d = den[i]
        if d is not None and d != 0:
            out[i] = num[i] / d
    return out


_HOUR_SECONDS = 3600
_DAY_SECONDS = 86400


def _session_start_flags(candles: list) -> list[bool]:
    """First bar of each trading session, read back from bar gaps.

    Port of openalgo-charts ``sessionStartIndices``/``sessionStartFlags``
    (src/feed/time.ts): the overnight break is the widest recurring gap;
    candidate starts sit behind gaps >= max(4 * medianGap, 4h), accepted only
    at roughly daily cadence (median open-to-open span <= 36h). With no
    readable break (daily bars / never-closing market) it falls back to the
    IST calendar day — the zone default of the TS source. Bar timestamps here
    are naive-IST wall clock, so ``date()`` is exactly the TS ``isNewIstDay``.
    """
    n = len(candles)
    flags = [False] * n
    times = [c.timestamp for c in candles]
    gaps = sorted(
        (b - a).total_seconds()
        for a, b in zip(times, times[1:], strict=False)
        if b > a
    )
    start_indices: list[int] | None = None
    if gaps:
        gap = gaps[len(gaps) // 2]
        if 0 < gap < _DAY_SECONDS:
            threshold = max(4 * gap, 4 * _HOUR_SECONDS)
            starts = [
                i for i in range(1, n)
                if (times[i] - times[i - 1]).total_seconds() >= threshold
            ]
            if starts:
                opens = [times[0]] + [times[i] for i in starts]
                spans = sorted(
                    (opens[j] - opens[j - 1]).total_seconds()
                    for j in range(1, len(opens))
                )
                if spans[len(spans) // 2] <= 36 * _HOUR_SECONDS:
                    start_indices = starts
    if start_indices is None:
        for i in range(1, n):
            flags[i] = times[i].date() != times[i - 1].date()
        return flags
    for i in start_indices:
        flags[i] = True
    return flags


def twap(candles: list) -> list[float | None]:
    """Time Weighted Average Price — running mean of ohlc4 from the anchor.

    Matches openalgo-charts ``TWAP`` (src/indicators/averages.ts): the
    descriptor's anchor defaults to ``session``, restarting the running mean
    on each session-start flag read back from the bar gaps. Emits from index
    0 (twap[0] == ohlc4[0]); no warmup padding.
    """
    restarts = _session_start_flags(candles)
    out: list[float | None] = []
    total = 0.0
    count = 0
    for restart, c in zip(restarts, candles, strict=True):
        if restart:
            total = 0.0
            count = 0
        o = c.ohlc.open.value
        h = c.ohlc.high.value
        l = c.ohlc.low.value
        cl = c.ohlc.close.value
        total += float(o + h + l + cl) / 4.0
        count += 1
        out.append(total / count)
    return out


def mcginley(values: list[float], period: int) -> list[float | None]:
    """McGinley Dynamic (openalgo-charts parity).

    Matches openalgo-charts ``MCGINLEY_DYNAMIC`` (src/indicators/averages.ts):
    seeded from the SMA-seeded EMA (first print at index ``period - 1``),
    then ``mg += (src - mg) / (period * (src/mg)^4)``. The na(mg[1]) seed
    branch also covers prev == 0 (the ratio src/mg has no value there) and a
    non-finite step result re-seeds rather than propagating NaN.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    seed = _sma_seeded_ema(values, period)
    n = len(values)
    out: list[float | None] = [None] * n
    prev: float | None = None
    for i in range(n):
        s = seed[i]
        if prev is None or prev == 0 or s is None:
            out[i] = s
        else:
            step = period * (values[i] / prev) ** 4
            nxt = prev + (values[i] - prev) / step
            out[i] = nxt if math.isfinite(nxt) else s
        prev = out[i]
    return out


def lsma(values: list[float], period: int, offset: int = 0) -> list[float | None]:
    """Least Squares Moving Average (openalgo-charts parity).

    Matches openalgo-charts ``linreg`` (src/indicators/calc.ts): least-squares
    line over the last ``period`` values with x = 0 at the oldest bar,
    evaluated ``offset`` bars back from its right-hand end:
    ``intercept + slope * (period - 1 - offset)``. offset=0 shifts nothing.
    None before index ``period - 1``.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 1 or n < period:
        return out
    sum_x = ((period - 1) * period) // 2
    sum_x_sqr = ((period - 1) * period * (2 * period - 1)) // 6
    denom = period * sum_x_sqr - sum_x * sum_x
    if denom == 0:
        return out
    for i in range(period - 1, n):
        sum_y = 0.0
        sum_xy = 0.0
        for k in range(period):
            y = values[i - (period - 1 - k)]  # k = 0 is the oldest bar
            sum_y += y
            sum_xy += y * k
        slope = (period * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / period
        out[i] = intercept + slope * (period - 1 - offset)
    return out


# ---------------------------------------------------------------------------
# Specs — ready for registry import; NOT registered here (merge task wires them).
# ---------------------------------------------------------------------------

_CLOSING_PRICES = lambda candles: [float(c.ohlc.close.value) for c in candles]  # noqa: E731


def _fn_vwma(candles, period):
    vols = [float(c.volume.value) for c in candles]
    closes = _CLOSING_PRICES(candles)
    return vwma(closes, vols, int(period))


def _fn_twap(candles):
    return twap(candles)


def _fn_mcginley(candles, period):
    return mcginley(_CLOSING_PRICES(candles), int(period))


def _fn_lsma(candles, period, offset=0):
    return lsma(_CLOSING_PRICES(candles), int(period), int(offset))


SPEC_VWMA = IndicatorSpec(
    id="vwma", name="VWMA", category="Volume", placement="overlay",
    params=(("period", "int", 20),),
    plots=(("value", "line", "VWMA"),),
    fn=_fn_vwma,
)

SPEC_TWAP = IndicatorSpec(
    id="twap", name="TWAP", category="Trend", placement="overlay",
    params=(),
    plots=(("value", "line", "TWAP"),),
    fn=_fn_twap,
)

SPEC_MCGINLEY_DYNAMIC = IndicatorSpec(
    id="mcginley-dynamic", name="McGinley Dynamic", category="Trend",
    placement="overlay",
    params=(("period", "int", 14),),
    plots=(("value", "line", "McGinley"),),
    fn=_fn_mcginley,
)

SPEC_LSMA = IndicatorSpec(
    id="lsma", name="LSMA", category="Trend", placement="overlay",
    params=(("period", "int", 25),),
    plots=(("value", "line", "LSMA"),),
    fn=_fn_lsma,
)
