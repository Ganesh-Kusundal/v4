"""Seasonality heatmap (Trend tier) — pure port of openalgo-charts' study.

Tabulates monthly percentage changes, one row per calendar year, as a
``{rows, options}`` table for the chart's ``table`` hook. Months resolve on
``Asia/Kolkata`` by default. ``compute_seasonality`` is the stateless entry
point used by the API route.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from tradex_trading.analytics.indicators import IndicatorSpec

DEFAULT_TIMEZONE = "Asia/Kolkata"
POS_DEFAULT = "#089981"
NEG_DEFAULT = "#F23745"
NEUTRAL = "#787b86"
LIGHT_ALPHA = 0.1
HEAVY_ALPHA = 0.5

MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

_POSITIONS = {
    "Left": "bottom-left",
    "Center": "bottom-center",
    "Right": "bottom-right",
}


def _round_half_up(x: float) -> int:
    return math.floor(x + 0.5)


def _zoned_parts(utc_seconds: int, zone: str) -> dict[str, int]:
    dt = datetime.fromtimestamp(utc_seconds, ZoneInfo(zone))
    return {"year": dt.year, "month": dt.month, "day": dt.day,
            "hour": dt.hour, "minute": dt.minute}


def _zoned_wall_clock_to_utc_seconds(
    year: int, month: int, day: int,
    hour: int = 0, minute: int = 0, second: int = 0,
    zone: str = DEFAULT_TIMEZONE,
) -> int:
    dt = datetime(year, month, day, hour, minute, second, tzinfo=ZoneInfo(zone))
    return int(dt.timestamp())


def with_opacity(color: str, alpha: float) -> str:
    """``#rrggbb`` plus an alpha byte, exactly as the TS ``withOpacity``."""
    hex_ = color
    if len(hex_) == 4 and hex_.startswith("#") and all(
        c in "0123456789abcdefABCDEF" for c in hex_[1:]
    ):
        hex_ = f"#{hex_[1]}{hex_[1]}{hex_[2]}{hex_[2]}{hex_[3]}{hex_[3]}"
    elif len(hex_) == 9 and hex_.startswith("#"):
        hex_ = hex_[:7]
    elif not (len(hex_) == 7 and hex_.startswith("#")):
        return color
    byte = _round_half_up(min(1, max(0, alpha)) * 255)
    return f"{hex_}{byte:02x}"


def ramp_color(value: float | None, cutoff: float, pos: str, neg: str) -> str | None:
    if value is None or not math.isfinite(value):
        return None
    base = pos if value >= 0 else neg
    t = min(1, abs(value) / cutoff) if cutoff > 0 else 1
    return with_opacity(base, LIGHT_ALPHA + (HEAVY_ALPHA - LIGHT_ALPHA) * t)


def _pct(v: float) -> str:
    return f"{v:.2f}%"


def _month_span_utc(year: int, month: int, zone: str) -> int:
    # ``datetime`` rejects month 13; ``Date.UTC`` rolls it into January of the
    # next year, which is how the reference computes December's end.
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return _zoned_wall_clock_to_utc_seconds(ny, nm, 1, zone=zone)


def _month_spans(bars: list[dict[str, Any]], zone: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    frm = 0
    until = 0
    for bar in bars:
        if cur is None or bar["time"] < frm or bar["time"] >= until:
            p = _zoned_parts(bar["time"], zone)
            cur = {"year": p["year"], "month": p["month"], "last": bar["close"]}
            out.append(cur)
            frm = _zoned_wall_clock_to_utc_seconds(p["year"], p["month"], 1, zone=zone)
            until = _month_span_utc(p["year"], p["month"], zone)
        else:
            cur["last"] = bar["close"]
    return out


def _month_key(year: int, month: int) -> int:
    return year * 100 + month


def _ignored_keys(spec: str) -> set[int]:
    out: set[int] = set()
    for item in spec.split(","):
        digits = "".join(c for c in item if c not in " -")
        if not (len(digits) == 6 and digits.isdigit()):
            continue
        key = int(digits)
        month = key % 100
        if 1 <= month <= 12:
            out.add(key)
    return out


def _build_matrix(bars: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, Any]:
    spans = _month_spans(bars, str(settings.get("timezone", DEFAULT_TIMEZONE)))
    start_year = math.floor(float(settings.get("startYear", 2015)) + 0.5)
    skipped = _ignored_keys(str(settings.get("ignoredMonths", "")))

    forming = spans[-1] if spans else None
    if forming is not None:
        skipped.add(_month_key(forming["year"], forming["month"]))

    years: list[int] = []
    by_year: dict[int, list[float | None]] = {}
    for i in range(1, len(spans)):
        span = spans[i]
        prev = spans[i - 1]
        if span["year"] < start_year or _month_key(span["year"], span["month"]) in skipped:
            continue
        if not math.isfinite(prev["last"]) or not math.isfinite(span["last"]) or prev["last"] == 0:
            continue
        row = by_year.get(span["year"])
        if row is None:
            row = [None] * 12
            by_year[span["year"]] = row
            years.append(span["year"])
        row[span["month"] - 1] = (100 * (span["last"] - prev["last"])) / abs(prev["last"])
    return {"years": years, "byYear": by_year, "skipped": skipped}


def _column(matrix: dict[str, Any], month: int) -> list[float]:
    out: list[float] = []
    for year in matrix["years"]:
        v = matrix["byYear"].get(year, [None] * 12)[month]
        if v is not None:
            out.append(v)
    return out


def _mean(v: list[float]) -> float | None:
    if not v:
        return None
    s = 0
    for x in v:
        s += x
    return s / len(v)


def _sample_stdev(v: list[float]) -> float | None:
    if len(v) < 2:
        return None
    mu = _mean(v)
    if mu is None:
        return None
    acc = 0
    for x in v:
        acc += (x - mu) * (x - mu)
    return math.sqrt(acc / (len(v) - 1))


def _percent_positive(v: list[float]) -> float | None:
    if not v:
        return None
    pos = sum(1 for x in v if x >= 0)
    return (100 * pos) / len(v)


def _build_table(bars: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, Any]:
    matrix = _build_matrix(bars, settings)
    if not matrix["years"]:
        return {"rows": [], "options": {}}

    cutoff = float(settings.get("cutoffPercent", 10))
    pos = str(settings.get("posColor", POS_DEFAULT))
    neg = str(settings.get("negColor", NEG_DEFAULT))
    header_bg = with_opacity(NEUTRAL, 0.2)
    skip_bg = with_opacity(NEUTRAL, 0.5)

    def label(text: str) -> dict[str, str]:
        return {"text": text, "bgColor": header_bg}

    rows = [[label("Year")] + [label(m) for m in MONTH_NAMES]]
    row_weights: list[float] = [1]

    for year in matrix["years"]:
        row = matrix["byYear"].get(year, [None] * 12)
        cells = [label(str(year))]
        for m in range(12):
            if _month_key(year, m + 1) in matrix["skipped"]:
                cells.append({"text": "SKIP", "bgColor": skip_bg})
                continue
            v = row[m]
            cells.append(
                {"text": _pct(v), "bgColor": ramp_color(v, cutoff, pos, neg)}
                if v is not None
                else {"text": ""}
            )
        rows.append(cells)
        row_weights.append(1)

    show_avg = settings.get("showAvg", True) is not False
    show_st_dev = settings.get("showStDev", True) is not False
    show_pos = settings.get("showPos", True) is not False
    if show_avg or show_st_dev or show_pos:
        rows.append([{"text": "", "bgColor": header_bg} for _ in range(13)])
        row_weights.append(0.3)

        if show_avg:
            cells = [label("Avgs:")]
            for m in range(12):
                v = _mean(_column(matrix, m))
                cells.append(
                    {"text": _pct(v), "bgColor": ramp_color(v, cutoff, pos, neg)}
                    if v is not None
                    else {"text": ""}
                )
            rows.append(cells)
            row_weights.append(1)
        if show_st_dev:
            cells = [label("StDev:")]
            for m in range(12):
                v = _sample_stdev(_column(matrix, m))
                cells.append({"text": f"{v:.2f}" if v is not None else "", "bgColor": header_bg})
            rows.append(cells)
            row_weights.append(1)
        if show_pos:
            cells = [label("Pos%:")]
            for m in range(12):
                v = _percent_positive(_column(matrix, m))
                cells.append(
                    {"text": f"{_round_half_up(v)}%", "bgColor": ramp_color(v - 50, 50, pos, neg)}
                    if v is not None else {"text": ""}
                )
            rows.append(cells)
            row_weights.append(1)

    return {
        "rows": rows,
        "options": {
            "position": _POSITIONS.get(
                str(settings.get("tablePosition", "Center")), "bottom-center"
            ),
            "cellWidth": [52] + [46] * 12,
            "cellHeight": 18,
            "rowWeights": row_weights,
            "widthPercent": float(settings.get("tableWidth", 100)),
            "heightPercent": float(settings.get("tableHeight", 95)),
            "fontSize": 10,
            "margin": 8,
        },
    }


def compute_seasonality(bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    settings = {
        "startYear": int(params.get("start_year", 2015)),
        "ignoredMonths": str(params.get("ignored_months", "")),
        "cutoffPercent": float(params.get("cutoff_percent", 10)),
        "posColor": str(params.get("pos_color", POS_DEFAULT)),
        "negColor": str(params.get("neg_color", NEG_DEFAULT)),
        "showAvg": bool(params.get("show_avg", True)),
        "showStDev": bool(params.get("show_st_dev", True)),
        "showPos": bool(params.get("show_pos", True)),
        "tablePosition": str(params.get("table_position", "Center")),
        "tableWidth": float(params.get("table_width", 100)),
        "tableHeight": float(params.get("table_height", 95)),
    }
    return _build_table(bars, settings)


def _fn_seasonality(
    candles: list,
    startYear: int = 2015,
    cutoffPercent: float = 10.0,
    tablePosition: str = "Center",
    tableWidth: int = 100,
    tableHeight: int = 95,
    showAvg: bool = True,
    showStDev: bool = True,
    showPos: bool = True,
    ignoredMonths: str = "",
) -> dict[str, list]:
    """Seasonality line — all-None by engine design (openalgo-charts parity).

    Matches ``SEASONALITY.calc`` (src/indicators/seasonality.ts): the plot
    draws nothing and only owns the pane; the heatmap matrix ships through
    the table hook (see ``compute_seasonality`` above). Params mirror the
    engine inputs so the catalogue exposes the same settings surface.
    """
    return {"seasonality": [None] * len(candles)}


SPEC_SEASONALITY = IndicatorSpec(
    id="seasonality",
    name="Seasonality",
    category="Trend",
    placement="pane",
    params=(
        ("startYear", "int", 2015),
        ("cutoffPercent", "float", 10.0),
        ("tablePosition", "select", "Center"),
        ("tableWidth", "int", 100),
        ("tableHeight", "int", 95),
        ("showAvg", "bool", True),
        ("showStDev", "bool", True),
        ("showPos", "bool", True),
        ("ignoredMonths", "text", ""),
    ),
    plots=(("seasonality", "line", "Seasonality"),),
    fn=_fn_seasonality,
)
