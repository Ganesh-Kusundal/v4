"""Complex studies — openalgo-charts parity (studies.ts / signals.ts / ranges.ts).

Batch 5 parallel split: five complex studies ported from the reference TS
descriptors. Helpers (``sma``, ``wma``, ``_change``, ``_rolling_sum``,
``_sma_skip_none``) are imported from ``indicators.py`` / ``volume_flow.py`` /
``studies_simple.py`` and never redefined.

TS sources
- ``src/indicators/studies.ts`` — CPR, RANGE_ANALYSIS
- ``src/indicators/signals.ts`` — VORTEX
- ``src/indicators/ranges.ts`` — RELATIVE_VIGOR_INDEX, RELATIVE_VOLATILITY_INDEX
- ``src/indicators/calc.ts`` — ``rollingSum``, ``sma``, ``stdev``, ``swma``
- ``src/feed/time.ts`` — ``sessionStartFlags``, ``calendarPeriodFlags``,
  ``isNewIstDay`` / week / month boundaries and ``medianGap``

Parity notes
- ``_sma_skip_none`` mirrors calc.ts ``sma`` NaN-counting semantics; every
  ``fromFirstValue(...)`` stack (RVI bands, RVGI, RVI smoothing) reduces to it.
- ``_seeded_ema`` mirrors ranges.ts ``seededEma`` (the reference EMA): a
  NaN-strict SMA seed, recursing only over finite values and re-seeding on a
  hole. RVI's up/down sources are NaN for ``length - 1`` bars (the stdev
  warmup) then a mix of real zeros and finite values, so the seed lands at
  ``length - 1 + 14 - 1`` on a clean market — index 22 on the defaults.
- ``_swma`` mirrors calc.ts ``swma``: the fixed 4-bar 1/2/2/1 kernel, first
  value at index 3.
- ``_stdev_ts`` mirrors calc.ts ``stdev``: population stdev (``/period``)
  around the NaN-strict SMA mean.
- ``_from_first_value`` mirrors ranges.ts ``fromFirstValue``: slice off the
  leading warmup Nones, smooth the live tail, re-pad the front. The smoother
  receives ``(tail, start)`` exactly as the TS signature does, which VWMA needs
  to align the volume slice.
- ``_shifted`` mirrors ranges.ts ``shifted``: the reference ``offset`` is a
  drawing displacement folded into the column, so a positive offset moves
  values later and drops any pushed past the end.
- CPR reads trading sessions back from the timestamps exactly as
  ``sessionStartFlags`` does: median gap, ``max(4*gap, 4h)`` break threshold,
  daily cadence gate, calendar fallback for weekly/monthly frames. The floor
  pivot (``(H+L+C)/3``) carries over whole from the previous period, so the
  first period stays null.

These SPECs are NOT registered here — the batch-5 merge task wires them into
the registry and extends the golden parity harness.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from tradex_trading.analytics.indicators import (
    IndicatorSpec,
    _change,
    _rma,
    _rolling_sum,
    _to_float,
    sma,
    wma,
)
from tradex_trading.analytics.studies_simple import _src_val, _vwma
from tradex_trading.analytics.volume_flow import _sma_skip_none

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _stdev_ts(values: list[float | None], period: int) -> list[float | None]:
    """calc.ts ``stdev``: rolling population stdev, NaN-strict over the window."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    means = _sma_skip_none(values, period)
    for i in range(period - 1, n):
        m = means[i]
        if m is None:
            continue
        acc = 0.0
        ok = True
        for k in range(period):
            v = values[i - k]
            if v is None:
                ok = False
                break
            d = v - m
            acc += d * d
        if ok:
            out[i] = math.sqrt(acc / period)
    return out


def _swma(values: list[float]) -> list[float | None]:
    """calc.ts ``swma``: the fixed 4-bar 1/2/2/1 kernel; first value at index 3."""
    n = len(values)
    out: list[float | None] = [None] * n
    for i in range(3, n):
        out[i] = (
            values[i - 3] + 2.0 * values[i - 2] + 2.0 * values[i - 1] + values[i]
        ) / 6.0
    return out


def _from_first_value(
    values: list[float | None],
    smooth: Callable[[list[float | None], int], list[float | None]],
) -> list[float | None]:
    """ranges.ts ``fromFirstValue``: smooth the tail after the first real value."""
    n = len(values)
    out: list[float | None] = [None] * n
    start = 0
    while start < n and values[start] is None:
        start += 1
    if start >= n:
        return out
    tail = smooth(values[start:], start)
    for i, v in enumerate(tail):
        if start + i < n:
            out[start + i] = v
    return out


def _seeded_ema(values: list[float | None], period: int) -> list[float | None]:
    """ranges.ts ``seededEma``: the reference EMA with a NaN-strict SMA seed."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0:
        return out
    seed = _sma_skip_none(values, period)
    k = 2.0 / (period + 1)
    prev: float | None = None
    for i in range(n):
        if prev is None:
            prev = seed[i]
        else:
            v = values[i]
            prev = v * k + prev * (1.0 - k) if v is not None else None
        out[i] = prev
    return out


def _shifted(values: list[float | None], offset: int) -> list[float | None]:
    """ranges.ts ``shifted``: fold a plot offset into the column."""
    if offset == 0:
        return list(values)
    n = len(values)
    out: list[float | None] = [None] * n
    for i, v in enumerate(values):
        at = i + offset
        if 0 <= at < n:
            out[at] = v
    return out


def _true_range_series(
    highs: list[float], lows: list[float], closes: list[float]
) -> list[float]:
    """trueRange over series: ``max(H-L, |H-pC|, |L-pC|)``, bar 0 is ``H-L``."""
    out = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        h, lo, pc = highs[i], lows[i], closes[i - 1]
        out.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return out


# ---------------------------------------------------------------------------
# CPR with Floor Pivot (studies.ts CPR)
# ---------------------------------------------------------------------------

IST_OFFSET = 5 * 3600 + 30 * 60
DAY_SECONDS = 86400
HOUR_SECONDS = 3600


def _bar_time(candle: Any) -> int:
    """UTC seconds of a bar, whether a dict (``time``/``timestamp``) or a Candle.

    The harness Candle stores a tz-naive IST datetime; IST is a fixed UTC+5:30
    offset, so the original UTC epoch is recovered exactly.
    """
    if isinstance(candle, dict):
        t = candle.get("time")
        if t is None:
            t = candle.get("timestamp")
        if t is None:
            raise ValueError("candle dict has no time/timestamp for CPR")
        if isinstance(t, (int, float)):
            return int(t)
        return int(t.timestamp())
    naive = candle.timestamp
    return int((naive - datetime(1970, 1, 1)).total_seconds()) - IST_OFFSET


def _ist_day(t: int) -> int:
    """Epoch day in IST (cheap: IST is a fixed offset)."""
    return (t + IST_OFFSET) // DAY_SECONDS


def _ist_week(t: int) -> int:
    """Monday-based IST week index. Epoch day 4 is Monday 1970-01-05."""
    return (_ist_day(t) - 4) // 7


def _ist_ym(t: int) -> tuple[int, int]:
    """IST year/month (the frameBoundary monthly test's inputs)."""
    dt = datetime.fromtimestamp(t + IST_OFFSET, tz=UTC)
    return dt.year, dt.month


def _median_spacing(times: list[int]) -> int:
    """Median gap between consecutive bars; 0 for a series that never advances."""
    if len(times) < 2:
        return 0
    gaps = sorted(
        times[i] - times[i - 1] for i in range(1, len(times)) if times[i] - times[i - 1] > 0
    )
    if not gaps:
        return 0
    return gaps[len(gaps) // 2]


def _auto_pivot_period(times: list[int]) -> str:
    """One step coarser than the bars: intraday→daily, daily→weekly, else monthly."""
    gap = _median_spacing(times)
    if gap <= 0 or gap < DAY_SECONDS:
        return "daily"
    if gap < 7 * DAY_SECONDS:
        return "weekly"
    return "monthly"


def _session_start_indices(times: list[int]) -> list[int] | None:
    """feed/time.ts ``sessionStartIndices``: bars that open a new trading session."""
    gap = _median_spacing(times)
    if gap <= 0 or gap >= DAY_SECONDS:
        return None
    threshold = max(4 * gap, 4 * HOUR_SECONDS)
    starts = [
        i for i in range(1, len(times)) if times[i] - times[i - 1] >= threshold
    ]
    if not starts:
        return None
    opens = [times[0]] + [times[i] for i in starts]
    spans = sorted(opens[i] - opens[i - 1] for i in range(1, len(opens)))
    if spans[len(spans) // 2] > 36 * HOUR_SECONDS:
        return None
    return starts


def _session_start_flags(times: list[int]) -> list[bool]:
    """feed/time.ts ``sessionStartFlags`` (IST default zone)."""
    out = [False] * len(times)
    starts = _session_start_indices(times)
    if starts is None:
        for i in range(1, len(times)):
            out[i] = _ist_day(times[i - 1]) != _ist_day(times[i])
        return out
    for i in starts:
        out[i] = True
    return out


def _calendar_period_flags(
    times: list[int], is_new: Callable[[int, int], bool]
) -> list[bool]:
    """feed/time.ts ``calendarPeriodFlags``: boundary test on session opens."""
    out = [False] * len(times)
    starts = _session_start_indices(times)
    if starts is None:
        for i in range(1, len(times)):
            out[i] = is_new(times[i - 1], times[i])
        return out
    prev_open = times[0]
    for i in starts:
        if is_new(prev_open, times[i]):
            out[i] = True
        prev_open = times[i]
    return out


PERIOD_PREFIX = {"daily": "d", "weekly": "w", "monthly": "m"}
PERIOD_LABEL = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}
PIVOT_LEVELS = ("Pivot", "S1", "S2", "S3", "R1", "R2", "R3", "Bc", "Tc")


def _pivot_columns(
    candles: list,
    times: list[int],
    period: str,
    active: bool,
    show: dict[str, bool],
    opens_frame: list[bool],
) -> dict[str, list[float | None]]:
    """One period's nine columns: extremes accumulate, frozen whole at the boundary."""
    n = len(candles)
    pref = PERIOD_PREFIX[period]
    cols: dict[str, list[float | None]] = {
        f"{pref}{k}": [None] * n for k in PIVOT_LEVELS
    }
    if not active:
        return cols

    prev_high: float | None = None
    prev_low: float | None = None
    prev_close: float | None = None
    cur_high: float | None = None
    cur_low: float | None = None
    cur_close: float | None = None

    for i in range(n):
        if opens_frame[i]:
            prev_high, prev_low, prev_close = cur_high, cur_low, cur_close
            cur_high, cur_low = None, None
        h = _src_val(candles[i], "high")
        lo = _src_val(candles[i], "low")
        c = _src_val(candles[i], "close")
        cur_high = h if cur_high is None else max(cur_high, h)
        cur_low = lo if cur_low is None else min(cur_low, lo)
        cur_close = c
        if prev_high is None or prev_low is None or prev_close is None:
            continue

        p = (prev_high + prev_low + prev_close) / 3.0
        width = prev_high - prev_low
        sup1 = 2.0 * p - prev_high
        res1 = 2.0 * p - prev_low
        bottom = (prev_high + prev_low) / 2.0

        if show["pivot"]:
            cols[f"{pref}Pivot"][i] = p
        if show["support"]:
            if show["s1r1"]:
                cols[f"{pref}S1"][i] = sup1
            cols[f"{pref}S2"][i] = p - width
            cols[f"{pref}S3"][i] = sup1 - width
        if show["resistance"]:
            if show["s1r1"]:
                cols[f"{pref}R1"][i] = res1
            cols[f"{pref}R2"][i] = p + width
            cols[f"{pref}R3"][i] = res1 + width
        if show["cpr"]:
            cols[f"{pref}Bc"][i] = bottom
            cols[f"{pref}Tc"][i] = 2.0 * p - bottom
    return cols


def cpr(
    candles: list,
    pivot_mode: str = "auto",
    show_daily: bool = True,
    show_weekly: bool = False,
    show_monthly: bool = False,
    display_pivots: bool = True,
    display_support: bool = True,
    display_resistance: bool = True,
    display_cpr: bool = True,
    display_s1r1: bool = False,
) -> dict[str, list[float | None]]:
    """CPR with Floor Pivot — d/w/m frames of Pivot / S1-S3 / R1-R3 / Bc / Tc.

    Matches ``CPR.calc`` in studies.ts: the floor pivot ``(H+L+C)/3`` is the
    previous frame's extremes handed over whole at the frame boundary, so the
    first period has nothing behind it and stays null. ``auto`` resolves to
    exactly one frame (daily for intraday bars); ``manual`` stacks the frames
    the show toggles switch on. S1/R1 are null unless ``display_s1r1`` (the
    published script draws them with its plot calls commented out).
    """
    times = [_bar_time(c) for c in candles]
    manual = pivot_mode == "manual"
    auto = _auto_pivot_period(times)
    show = {
        "pivot": display_pivots,
        "support": display_support,
        "resistance": display_resistance,
        "cpr": display_cpr,
        "s1r1": display_s1r1,
    }
    wanted = (
        {
            "daily": show_daily,
            "weekly": show_weekly,
            "monthly": show_monthly,
        }
        if manual
        else {
            "daily": auto == "daily",
            "weekly": auto == "weekly",
            "monthly": auto == "monthly",
        }
    )
    flags = {
        "daily": _session_start_flags(times),
        "weekly": _calendar_period_flags(
            times, lambda a, b: _ist_week(a) != _ist_week(b)
        ),
        "monthly": _calendar_period_flags(
            times, lambda a, b: _ist_ym(a) != _ist_ym(b)
        ),
    }
    out: dict[str, list[float | None]] = {}
    for period in ("daily", "weekly", "monthly"):
        out.update(
            _pivot_columns(candles, times, period, wanted[period], show, flags[period])
        )
    return out


# ---------------------------------------------------------------------------
# Range Analysis (studies.ts RANGE_ANALYSIS)
# ---------------------------------------------------------------------------


def range_analysis(
    candles: list, show_average: bool = False, avg_length: int = 3
) -> dict[str, list[float | None]]:
    """Range Analysis — bar high-low histogram, optionally SMA-averaged.

    Matches ``RANGE_ANALYSIS.calc`` in studies.ts: ``range`` is ``high - low``
    on every bar; ``avg_range`` is ``sma(range, avgLength)`` and is all null
    unless ``show_average`` is true.
    """
    highs = [_src_val(c, "high") for c in candles]
    lows = [_src_val(c, "low") for c in candles]
    rng = [h - lo for h, lo in zip(highs, lows)]
    n = len(rng)
    if not show_average:
        return {"range": rng, "avg_range": [None] * n}
    return {"range": rng, "avg_range": sma(rng, max(1, int(round(float(avg_length)))))}


# ---------------------------------------------------------------------------
# Vortex (signals.ts VORTEX)
# ---------------------------------------------------------------------------


def vortex(candles: list, length: int = 14) -> dict[str, list[float | None]]:
    """Vortex Indicator — window travel up (VI+) vs down (VI-) over true range.

    Matches ``VORTEX.calc`` in signals.ts: both movement terms straddle a bar
    boundary, so bar 0 has no term and the rolling sums are shifted forward one
    bar — the first value lands at index ``length``. The denominator is a
    rolling sum of true range, not Wilder's ATR.
    """
    n = len(candles)
    period = max(1, math.floor(float(length)))
    highs = [_src_val(c, "high") for c in candles]
    lows = [_src_val(c, "low") for c in candles]
    closes = [_src_val(c, "close") for c in candles]
    up_term = [abs(highs[i] - lows[i - 1]) for i in range(1, n)]
    down_term = [abs(lows[i] - highs[i - 1]) for i in range(1, n)]
    vmp = _rolling_sum(up_term, period)
    vmm = _rolling_sum(down_term, period)
    tr = _true_range_series(highs, lows, closes)
    tr_sum = _rolling_sum(tr, period)
    vip: list[float | None] = [None] * n
    vim: list[float | None] = [None] * n
    for i in range(1, n):
        t = tr_sum[i]
        if t is None or t == 0:
            continue
        u = vmp[i - 1]
        d = vmm[i - 1]
        if u is None or d is None:
            continue
        vip[i] = u / t
        vim[i] = d / t
    return {"vip": vip, "vim": vim}


# ---------------------------------------------------------------------------
# Relative Vigor Index (ranges.ts RELATIVE_VIGOR_INDEX)
# ---------------------------------------------------------------------------


def relative_vigor_index(
    candles: list, length: int = 10, offset: int = 0
) -> dict[str, list[float | None]]:
    """Relative Vigor Index — body-over-range, swma-smoothed, summed over length.

    Matches ``RELATIVE_VIGOR_INDEX.calc`` in ranges.ts: both halves are smoothed
    by the 1/2/2/1 ``swma`` before the ``length`` rolling sum, so the first
    value lands at index ``length + 2``. ``signal`` is the ``swma`` of the RVGI
    itself. ``offset`` is a drawing displacement folded into both columns.
    """
    period = max(1, int(round(float(length))))
    n = len(candles)
    closes = [_src_val(c, "close") for c in candles]
    opens = [_src_val(c, "open") for c in candles]
    highs = [_src_val(c, "high") for c in candles]
    lows = [_src_val(c, "low") for c in candles]
    body = _swma([closes[i] - opens[i] for i in range(n)])
    span = _swma([highs[i] - lows[i] for i in range(n)])
    numerator = _from_first_value(
        body, lambda t, _start: _rolling_sum(t, period)
    )
    denominator = _from_first_value(
        span, lambda t, _start: _rolling_sum(t, period)
    )
    rvgi: list[float | None] = [None] * n
    for i in range(n):
        d = denominator[i]
        v = numerator[i]
        if d is None or d == 0 or v is None:
            continue
        rvgi[i] = v / d
    signal = _from_first_value(rvgi, lambda t, _start: _swma(t))
    off = int(round(float(offset)))
    return {"rvgi": _shifted(rvgi, off), "signal": _shifted(signal, off)}


# ---------------------------------------------------------------------------
# Relative Volatility Index (ranges.ts RELATIVE_VOLATILITY_INDEX)
# ---------------------------------------------------------------------------


def _smoothing_ma(
    kind: str,
    values: list[float | None],
    vols: list[float],
    length: int,
) -> list[float | None]:
    """ranges.ts ``smoothingMa``: the smoothing block's kernel switch."""
    if kind == "EMA":
        return _seeded_ema(values, length)
    if kind == "SMMA (RMA)":
        return _from_first_value(values, lambda t, _start: _rma(t, length))
    if kind == "WMA":
        return _from_first_value(values, lambda t, _start: wma(t, length))
    if kind == "VWMA":
        return _from_first_value(
            values, lambda t, start: _vwma(t, vols[start:], length)
        )
    return _from_first_value(values, lambda t, _start: _sma_skip_none(t, length))


def relative_volatility_index(
    candles: list,
    length: int = 10,
    offset: int = 0,
    ma_type: str = "SMA",
    ma_length: int = 14,
    bb_mult: float = 2,
) -> dict[str, list[float | None]]:
    """Relative Volatility Index — RSI's arithmetic on volatility, plus smoothing.

    Matches ``RELATIVE_VOLATILITY_INDEX.calc`` in ranges.ts: ``length`` is the
    standard deviation's window only (the smoothing EMA is the fixed 14 the
    reference hard-codes). Up/down sources are ``change <= 0 ? 0 : stddev`` and
    ``change > 0 ? 0 : stddev``, so their EMA seed waits for a clean 14-bar
    window and the first RVI lands at ``length + 12`` on a one-way market.
    ``ma`` follows ``ma_type`` (None/SMA/EMA/SMMA/WMA/VWMA); the Bollinger
    bands are ``ma +/- bb_mult * stdev`` and exist only for
    ``SMA + Bollinger Bands``. ``offset`` displaces only the RVI.
    """
    window = max(1, int(round(float(length))))
    ema_length = 14
    closes = [_src_val(c, "close") for c in candles]
    n = len(closes)
    sd = _stdev_ts(closes, window)
    delta = _change(closes, 1)
    up_source: list[float | None] = [None] * n
    down_source: list[float | None] = [None] * n
    for i in range(n):
        d = delta[i]
        sdi = sd[i]
        up_source[i] = 0.0 if (d is not None and d <= 0) else sdi
        down_source[i] = 0.0 if (d is not None and d > 0) else sdi
    upper = _seeded_ema(up_source, ema_length)
    lower = _seeded_ema(down_source, ema_length)
    rvi: list[float | None] = [None] * n
    for i in range(n):
        u = upper[i]
        lo = lower[i]
        if u is None or lo is None:
            continue
        total = u + lo
        if total == 0:
            continue
        rvi[i] = (u / total) * 100.0

    smooth_len = max(1, int(round(float(ma_length))))
    mult = float(bb_mult)
    if ma_type == "None":
        ma: list[float | None] = [None] * n
    else:
        vols = [_to_float(c.volume.value) if getattr(c, "volume", None) else 0.0 for c in candles]
        ma = _smoothing_ma(ma_type, rvi, vols, smooth_len)

    is_bb = ma_type == "SMA + Bollinger Bands"
    if is_bb:
        band = [
            None if v is None else v * mult
            for v in _from_first_value(
                rvi, lambda t, _start: _stdev_ts(t, smooth_len)
            )
        ]
    else:
        band = [None] * n

    bb_upper: list[float | None] = [None] * n
    bb_lower: list[float | None] = [None] * n
    for i in range(n):
        m = ma[i]
        b = band[i]
        if m is None or b is None:
            continue
        bb_upper[i] = m + b
        bb_lower[i] = m - b

    off = int(round(float(offset)))
    return {
        "rvi": _shifted(rvi, off),
        "ma": ma,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
    }


# ---------------------------------------------------------------------------
# Specs — ready for registry import; NOT registered here (merge task wires).
# ---------------------------------------------------------------------------


def _fn_cpr(
    candles: list,
    pivot_mode: str,
    show_daily: bool,
    show_weekly: bool,
    show_monthly: bool,
    display_pivots: bool,
    display_support: bool,
    display_resistance: bool,
    display_cpr: bool,
    display_s1r1: bool,
) -> dict[str, list]:
    return cpr(
        candles,
        pivot_mode=pivot_mode,
        show_daily=bool(show_daily),
        show_weekly=bool(show_weekly),
        show_monthly=bool(show_monthly),
        display_pivots=bool(display_pivots),
        display_support=bool(display_support),
        display_resistance=bool(display_resistance),
        display_cpr=bool(display_cpr),
        display_s1r1=bool(display_s1r1),
    )


def _fn_range_analysis(candles: list, show_average: bool, avg_length: int) -> dict[str, list]:
    return range_analysis(candles, bool(show_average), int(avg_length))


def _fn_vortex(candles: list, length: int) -> dict[str, list]:
    return vortex(candles, int(length))


def _fn_relative_vigor_index(candles: list, length: int, offset: int) -> dict[str, list]:
    return relative_vigor_index(candles, int(length), int(offset))


def _fn_relative_volatility_index(
    candles: list,
    length: int,
    offset: int,
    ma_type: str,
    ma_length: int,
    bb_mult: float,
) -> dict[str, list]:
    return relative_volatility_index(
        candles, int(length), int(offset), ma_type, int(ma_length), float(bb_mult)
    )


_CPR_PLOTS = tuple(
    (f"{PERIOD_PREFIX[p]}{k}", "line", f"{PERIOD_LABEL[p]} {k}")
    for p in ("daily", "weekly", "monthly")
    for k in PIVOT_LEVELS
)

SPEC_CPR = IndicatorSpec(
    id="cpr",
    name="CPR with Floor Pivot",
    category="Trend",
    placement="overlay",
    params=(
        ("pivot_mode", "select", "auto"),
        ("show_daily", "bool", True),
        ("show_weekly", "bool", False),
        ("show_monthly", "bool", False),
        ("display_pivots", "bool", True),
        ("display_support", "bool", True),
        ("display_resistance", "bool", True),
        ("display_cpr", "bool", True),
        ("display_s1r1", "bool", False),
    ),
    plots=_CPR_PLOTS,
    fn=_fn_cpr,
)

SPEC_RANGE_ANALYSIS = IndicatorSpec(
    id="range-analysis",
    name="Range Analysis",
    category="Volatility",
    placement="pane",
    params=(
        ("show_average", "bool", False),
        ("avg_length", "int", 3),
    ),
    plots=(
        ("range", "histogram", "Range"),
        ("avg_range", "line", "Average Range"),
    ),
    fn=_fn_range_analysis,
)

SPEC_VORTEX = IndicatorSpec(
    id="vortex",
    name="Vortex Indicator",
    category="Trend",
    placement="pane",
    params=(("length", "int", 14),),
    plots=(
        ("vip", "line", "VI +"),
        ("vim", "line", "VI -"),
    ),
    levels=({"value": 1},),
    fn=_fn_vortex,
)

SPEC_RELATIVE_VIGOR_INDEX = IndicatorSpec(
    id="relative-vigor-index",
    name="Relative Vigor Index",
    category="Momentum",
    placement="pane",
    params=(
        ("length", "int", 10),
        ("offset", "int", 0),
    ),
    plots=(
        ("rvgi", "line", "RVGI"),
        ("signal", "line", "Signal"),
    ),
    levels=({"value": 0},),
    fn=_fn_relative_vigor_index,
)

SPEC_RELATIVE_VOLATILITY_INDEX = IndicatorSpec(
    id="relative-volatility-index",
    name="Relative Volatility Index",
    category="Volatility",
    placement="pane",
    params=(
        ("length", "int", 10),
        ("offset", "int", 0),
        ("ma_type", "select", "SMA"),
        ("ma_length", "int", 14),
        ("bb_mult", "float", 2),
    ),
    plots=(
        ("rvi", "line", "RVI"),
        ("ma", "line", "RVI-based MA"),
        ("bb_upper", "line", "Upper Bollinger Band"),
        ("bb_lower", "line", "Lower Bollinger Band"),
    ),
    levels=({"value": 80}, {"value": 50}, {"value": 20}),
    fn=_fn_relative_volatility_index,
)

SPECS = [
    SPEC_CPR,
    SPEC_RANGE_ANALYSIS,
    SPEC_VORTEX,
    SPEC_RELATIVE_VIGOR_INDEX,
    SPEC_RELATIVE_VOLATILITY_INDEX,
]

__all__ = [
    "cpr",
    "range_analysis",
    "vortex",
    "relative_vigor_index",
    "relative_volatility_index",
    "SPEC_CPR",
    "SPEC_RANGE_ANALYSIS",
    "SPEC_VORTEX",
    "SPEC_RELATIVE_VIGOR_INDEX",
    "SPEC_RELATIVE_VOLATILITY_INDEX",
    "SPECS",
]
