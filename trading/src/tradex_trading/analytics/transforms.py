"""Series transforms (Family B) — pure ports of openalgo-charts' transform tier.

Each transform re-buckets raw OHLC into a derived element series driven by
price movement, not the clock. Incremental state machines (reset/push/flush)
mirror the TS ``ISeriesTransform`` contract so live bars can extend a batch
without recomputing history. ``compute_transform`` is the stateless entry
point used by the API route: build fresh, feed all bars, flush, then make
times strictly increasing.
"""
from __future__ import annotations

import math
from typing import Any


class _Transform:
    def reset(self) -> None: ...
    def push(self, bar: dict[str, Any]) -> list[dict[str, Any]]: ...
    def flush(self) -> list[dict[str, Any]]:
        return []


def _ensure_increasing_times(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prev = -math.inf
    out: list[dict[str, Any]] = []
    for b in bars:
        t = b["time"]
        if t <= prev:
            t = prev + 1
        prev = t
        out.append({**b, "time": t})
    return out


def run_transform(t: _Transform, bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    t.reset()
    out: list[dict[str, Any]] = []
    for b in bars:
        out.extend(t.push(b))
    out.extend(t.flush())
    return _ensure_increasing_times(out)


class _HeikinAshi(_Transform):
    def __init__(self) -> None:
        self._prev_open = math.nan
        self._prev_close = math.nan

    def reset(self) -> None:
        self._prev_open = math.nan
        self._prev_close = math.nan

    def push(self, bar: dict[str, Any]) -> list[dict[str, Any]]:
        ha_close = (bar["open"] + bar["high"] + bar["low"] + bar["close"]) / 4
        ha_open = (
            (bar["open"] + bar["close"]) / 2
            if math.isnan(self._prev_open)
            else (self._prev_open + self._prev_close) / 2
        )
        ha_high = max(bar["high"], ha_open, ha_close)
        ha_low = min(bar["low"], ha_open, ha_close)
        self._prev_open = ha_open
        self._prev_close = ha_close
        return [
            {
                "time": bar["time"],
                "open": ha_open,
                "high": ha_high,
                "low": ha_low,
                "close": ha_close,
                "volume": bar.get("volume"),
            }
        ]


class _Renko(_Transform):
    def __init__(self, box_size: float = 2) -> None:
        if box_size <= 0:
            raise ValueError("openalgo-charts: Renko boxSize must be > 0")
        self._box = box_size
        self._edge = math.nan

    def reset(self) -> None:
        self._edge = math.nan

    def push(self, bar: dict[str, Any]) -> list[dict[str, Any]]:
        p = bar["close"]
        out: list[dict[str, Any]] = []
        if math.isnan(self._edge):
            self._edge = math.floor(p / self._box) * self._box
            return out
        while p >= self._edge + self._box:
            lo = self._edge
            hi = self._edge + self._box
            out.append({"time": bar["time"], "open": lo, "high": hi, "low": lo, "close": hi})
            self._edge = hi
        while p <= self._edge - self._box:
            hi = self._edge
            lo = self._edge - self._box
            out.append({"time": bar["time"], "open": hi, "high": hi, "low": lo, "close": lo})
            self._edge = lo
        return out


class _RangeBars(_Transform):
    def __init__(self, range: float = 3) -> None:
        if range <= 0:
            raise ValueError("openalgo-charts: range must be > 0")
        self._range = range
        self._cur: dict[str, Any] | None = None

    def reset(self) -> None:
        self._cur = None

    def push(self, bar: dict[str, Any]) -> list[dict[str, Any]]:
        p = bar["close"]
        out: list[dict[str, Any]] = []
        if self._cur is None:
            self._cur = {"time": bar["time"], "open": p, "high": p, "low": p, "close": p}
        else:
            self._cur["high"] = max(self._cur["high"], p)
            self._cur["low"] = min(self._cur["low"], p)
            self._cur["close"] = p
            self._cur["time"] = bar["time"]
        if self._cur["high"] - self._cur["low"] >= self._range:
            out.append(self._cur)
            self._cur = None
        return out

    def flush(self) -> list[dict[str, Any]]:
        if self._cur is None:
            return []
        c = self._cur
        self._cur = None
        return [c]


class _LineBreak(_Transform):
    def __init__(self, lines: float = 3) -> None:
        self._n = max(1, lines)
        self._lines: list[dict[str, float]] = []

    def reset(self) -> None:
        self._lines = []

    def push(self, bar: dict[str, Any]) -> list[dict[str, Any]]:
        p = bar["close"]
        if len(self._lines) == 0:
            self._lines.append({"open": p, "close": p})
            return []
        recent = self._lines[-self._n :]
        max_high = -math.inf
        min_low = math.inf
        for line in recent:
            max_high = max(max_high, line["open"], line["close"])
            min_low = min(min_low, line["open"], line["close"])
        last = self._lines[-1]
        box: dict[str, float] | None = None
        if p > max_high:
            box = {"open": last["close"], "close": p}
        elif p < min_low:
            box = {"open": last["close"], "close": p}
        if box is None:
            return []
        self._lines.append(box)
        return [
            {
                "time": bar["time"],
                "open": box["open"],
                "high": max(box["open"], box["close"]),
                "low": min(box["open"], box["close"]),
                "close": box["close"],
            }
        ]


class _WilderAtr:
    def __init__(self, period: float) -> None:
        self._period = max(1, math.floor(period))
        self._prev_close = math.nan
        self._sum = 0.0
        self._n = 0
        self._atr = math.nan

    def reset(self) -> None:
        self._prev_close = math.nan
        self._sum = 0.0
        self._n = 0
        self._atr = math.nan

    def push(self, bar: dict[str, Any]) -> None:
        tr = (
            bar["high"] - bar["low"]
            if math.isnan(self._prev_close)
            else max(
                bar["high"] - bar["low"],
                abs(bar["high"] - self._prev_close),
                abs(bar["low"] - self._prev_close),
            )
        )
        self._prev_close = bar["close"]
        if self._n < self._period:
            self._sum += tr
            self._n += 1
            self._atr = self._sum / self._n
        else:
            self._atr = (self._atr * (self._period - 1) + tr) / self._period

    def value(self) -> float:
        return self._atr


class _PointFigure(_Transform):
    def __init__(
        self,
        box_size: float = 1,
        reversal: float = 3,
        method: str = "hl",
        mode: str = "fixed",
        percent: float | None = None,
        atr_period: float = 14,
        atr_multiplier: float = 1,
    ) -> None:
        self._mode = mode
        self._reversal = max(1, math.floor(reversal))
        self._method = method
        self._fixed_box = math.nan if box_size is None else box_size
        self._percent = math.nan if percent is None else percent
        self._atr_mult = atr_multiplier
        self._atr = _WilderAtr(atr_period)
        self._box = math.nan
        self._dir = 0
        self._top = math.nan
        self._bot = math.nan
        self._time = 0
        if mode == "fixed" and not (self._fixed_box > 0):
            raise ValueError("openalgo-charts: P&F boxSize must be > 0")
        if mode == "percent" and not (self._percent > 0):
            raise ValueError("openalgo-charts: P&F percent must be > 0")

    def reset(self) -> None:
        self._box = math.nan
        self._dir = 0
        self._top = math.nan
        self._bot = math.nan
        self._time = 0
        self._atr.reset()

    def _resolve_box(self, price: float) -> float:
        box = math.nan
        if self._mode == "fixed":
            box = self._fixed_box
        elif self._mode == "percent":
            box = (abs(price) * self._percent) / 100
        else:
            box = self._atr.value() * self._atr_mult
        if box > 0 and math.isfinite(box):
            return box
        if self._box > 0:
            return self._box
        pct = abs(price) * 0.01
        return pct if pct > 0 else 1

    def _column(self) -> dict[str, Any]:
        box = self._box
        low = self._bot * box
        high = (self._top + 1) * box
        up = self._dir >= 0
        return {
            "time": self._time,
            "open": low if up else high,
            "close": high if up else low,
            "high": high,
            "low": low,
            "boxSize": box,
            "boxes": self._top - self._bot + 1,
        }

    def push(self, bar: dict[str, Any]) -> list[dict[str, Any]]:
        if self._mode == "atr":
            self._atr.push(bar)

        use_hl = self._method == "hl"
        up_price = bar["high"] if use_hl else bar["close"]
        down_price = bar["low"] if use_hl else bar["close"]

        if math.isnan(self._top):
            self._box = self._resolve_box(bar["close"])
            b = math.floor(bar["close"] / self._box)
            self._top = b
            self._bot = b
            self._dir = 0
            self._time = bar["time"]
            return []

        hb = math.floor(up_price / self._box)
        lb = math.floor(down_price / self._box)

        if self._dir == 0:
            up = hb - self._top
            down = self._bot - lb
            if up <= 0 and down <= 0:
                return []
            if up >= down:
                self._top = hb
                self._dir = 1
            else:
                self._bot = lb
                self._dir = -1
            self._time = bar["time"]
            return []

        out: list[dict[str, Any]] = []
        if self._dir > 0:
            if hb > self._top:
                self._top = hb
            elif self._top - lb >= self._reversal:
                boundary = self._top * self._box
                out.append(self._column())
                self._box = self._resolve_box(down_price)
                self._top = math.ceil(boundary / self._box) - 1
                self._bot = math.floor(down_price / self._box)
                self._dir = -1
                self._time = bar["time"]
        else:
            if lb < self._bot:
                self._bot = lb
            elif hb - self._bot >= self._reversal:
                boundary = (self._bot + 1) * self._box
                out.append(self._column())
                self._box = self._resolve_box(up_price)
                self._bot = math.floor(boundary / self._box)
                self._top = math.floor(up_price / self._box)
                self._dir = 1
                self._time = bar["time"]
        return out

    def flush(self) -> list[dict[str, Any]]:
        if self._dir == 0 or math.isnan(self._top):
            return []
        return [self._column()]


class _Kagi(_Transform):
    def __init__(self, reversal: float = 2) -> None:
        if reversal <= 0:
            raise ValueError("openalgo-charts: Kagi reversal must be > 0")
        self._reversal = reversal
        self._dir = 0
        self._ext = math.nan
        self._prev_shoulder = -math.inf
        self._prev_waist = math.inf
        self._thick = False

    def reset(self) -> None:
        self._dir = 0
        self._ext = math.nan
        self._prev_shoulder = -math.inf
        self._prev_waist = math.inf
        self._thick = False

    def push(self, bar: dict[str, Any]) -> list[dict[str, Any]]:
        p = bar["close"]
        if math.isnan(self._ext):
            self._ext = p
            return []
        out: list[dict[str, Any]] = []
        if self._dir >= 0:
            if p > self._ext:
                self._ext = p
                if p > self._prev_shoulder:
                    self._thick = True
            elif self._ext - p >= self._reversal:
                out.append(self._vertex(bar["time"], self._ext, self._thick))
                self._prev_shoulder = self._ext
                self._dir = -1
                self._ext = p
        else:
            if p < self._ext:
                self._ext = p
                if p < self._prev_waist:
                    self._thick = False
            elif p - self._ext >= self._reversal:
                out.append(self._vertex(bar["time"], self._ext, self._thick))
                self._prev_waist = self._ext
                self._dir = 1
                self._ext = p
        return out

    def flush(self) -> list[dict[str, Any]]:
        if math.isnan(self._ext):
            return []
        return [self._vertex(0, self._ext, self._thick)]

    @staticmethod
    def _vertex(time: int, price: float, thick: bool) -> dict[str, Any]:
        return {
            "time": time,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1 if thick else 0,
        }


def compute_heikin_ashi(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return run_transform(_HeikinAshi(), bars)


def compute_renko(bars: list[dict[str, Any]], *, box_size: float = 2) -> list[dict[str, Any]]:
    return run_transform(_Renko(box_size=box_size), bars)


def compute_range_bars(bars: list[dict[str, Any]], *, range: float = 3) -> list[dict[str, Any]]:
    return run_transform(_RangeBars(range=range), bars)


def compute_line_break(bars: list[dict[str, Any]], *, lines: float = 3) -> list[dict[str, Any]]:
    return run_transform(_LineBreak(lines=lines), bars)


def compute_point_figure(
    bars: list[dict[str, Any]], *, box_size: float = 1, reversal: float = 3
) -> list[dict[str, Any]]:
    return run_transform(_PointFigure(box_size=box_size, reversal=reversal), bars)


def compute_kagi(bars: list[dict[str, Any]], *, reversal: float = 2) -> list[dict[str, Any]]:
    return run_transform(_Kagi(reversal=reversal), bars)


TRANSFORM_SPECS: dict[str, dict[str, Any]] = {
    "heikin-ashi": {
        "id": "heikin-ashi",
        "name": "Heikin Ashi",
        "function": compute_heikin_ashi,
        "params": {},
    },
    "renko": {"id": "renko", "name": "Renko", "function": compute_renko, "params": {"box_size": 2}},
    "range-bars": {
        "id": "range-bars",
        "name": "Range Bars",
        "function": compute_range_bars,
        "params": {"range": 3},
    },
    "line-break": {
        "id": "line-break",
        "name": "Line Break",
        "function": compute_line_break,
        "params": {"lines": 3},
    },
    "point-figure": {
        "id": "point-figure",
        "name": "Point & Figure",
        "function": compute_point_figure,
        "params": {"box_size": 1, "reversal": 3},
    },
    "kagi": {"id": "kagi", "name": "Kagi", "function": compute_kagi, "params": {"reversal": 2}},
}


def compute_transform(
    tid: str, bars: list[dict[str, Any]], params: dict[str, Any]
) -> list[dict[str, Any]]:
    spec = TRANSFORM_SPECS.get(tid)
    if spec is None:
        raise ValueError(f"unknown transform id: {tid!r}")
    fn = spec["function"]
    kwargs = dict(spec["params"])
    for key, default in spec["params"].items():
        value = params.get(key, default)
        kwargs[key] = int(value) if isinstance(default, int) else float(value)
    return fn(bars, **kwargs)
