"""Price profiles (Family C) — pure ports of openalgo-charts' profile tier.

Four distributions over a price grid: volume-at-price, time-at-price (TPO),
market profile (sessions of TPO blocks), and bid/ask footprint. Each mirrors
the TS reference field-for-field so the golden-parity gate holds at 1e-9.
``compute_profile`` is the stateless entry point used by the API route.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Kolkata"
IST_OFFSET_SECONDS = 5 * 3600 + 30 * 60
DAY_SECONDS = 86400


def _round_half_up(x: float) -> int:
    # JS `Math.round` is round-half-up; Python's builtin `round` is
    # round-half-even (banker's). The two differ at exact .5 boundaries, and
    # the reference goldens exercise one (114.05 -> 114.1), so we port
    # Math.round as floor(x + 0.5), which is equivalent for every double.
    return math.floor(x + 0.5)


def bucket_price(p: float, step: float) -> float:
    """Bucket a price to the tick grid, exactly as the TS ``bucketPrice``."""
    return _round_half_up((_round_half_up(p / step) * step) * 1e8) / 1e8


def price_buckets(low: float, high: float, step: float) -> list[float]:
    """Inclusive list of bucket prices spanning [low, high] on the tick grid."""
    lo = bucket_price(low, step)
    hi = bucket_price(high, step)
    out: list[float] = []
    p = lo
    while p <= hi + step / 2:
        out.append(bucket_price(p, step))
        p += step
    return out


def _ist_parts(utc_seconds: int) -> dict[str, int]:
    """Calendar parts of an instant on the fixed +5:30 IST clock."""
    dt = datetime.fromtimestamp(utc_seconds, ZoneInfo(DEFAULT_TIMEZONE))
    return {"year": dt.year, "month": dt.month, "day": dt.day,
            "hour": dt.hour, "minute": dt.minute}


def _parts_in(utc_seconds: int, zone: str) -> dict[str, int]:
    if zone == DEFAULT_TIMEZONE:
        return _ist_parts(utc_seconds)
    dt = datetime.fromtimestamp(utc_seconds, ZoneInfo(zone))
    return {"year": dt.year, "month": dt.month, "day": dt.day,
            "hour": dt.hour, "minute": dt.minute}


def _minute_of_day(utc_seconds: int, zone: str) -> int:
    if zone == DEFAULT_TIMEZONE:
        s = ((utc_seconds + IST_OFFSET_SECONDS) % DAY_SECONDS + DAY_SECONDS) % DAY_SECONDS
        return math.floor(s / 60)
    p = _parts_in(utc_seconds, zone)
    return p["hour"] * 60 + p["minute"]


def _day_index_in(utc_seconds: int, zone: str) -> int:
    if zone == DEFAULT_TIMEZONE:
        return math.floor((utc_seconds + IST_OFFSET_SECONDS) / DAY_SECONDS)
    p = _parts_in(utc_seconds, zone)
    civil = datetime(p["year"], p["month"], p["day"])
    return math.floor(civil.timestamp() / DAY_SECONDS)


def _zoned_wall_clock_to_utc_seconds(
    year: int, month: int, day: int,
    hour: int = 0, minute: int = 0, second: int = 0,
    zone: str = DEFAULT_TIMEZONE,
) -> int:
    # IST is a fixed offset, so a single conversion matches the reference's
    # DST-corrected two-pass arithmetic.
    dt = datetime(year, month, day, hour, minute, second, tzinfo=ZoneInfo(zone))
    return int(dt.timestamp())


def _window_open_on(utc_seconds: int, w: dict[str, Any], zone: str, day_offset: int) -> int:
    p = _parts_in(utc_seconds, zone)
    civil = datetime(p["year"], p["month"], p["day"] + day_offset)
    return _zoned_wall_clock_to_utc_seconds(
        civil.year, civil.month, civil.day,
        math.floor(w["startMinute"] / 60), w["startMinute"] % 60, 0, zone,
    )


def _in_window(utc_seconds: int, w: dict[str, Any], zone: str = DEFAULT_TIMEZONE) -> bool:
    if w["startMinute"] == w["endMinute"]:
        return True
    m = _minute_of_day(utc_seconds, w.get("zone") or zone)
    if w["startMinute"] < w["endMinute"]:
        return w["startMinute"] <= m < w["endMinute"]
    return m >= w["startMinute"] or m < w["endMinute"]


def _session_key(utc_seconds: int, mode: str, zone: str, w: dict[str, Any] | None) -> int:
    if mode == "composite":
        return 0
    if mode == "month":
        p = _parts_in(utc_seconds, zone)
        return p["year"] * 12 + (p["month"] - 1)
    day_index = _day_index_in(utc_seconds, zone)
    if (
        w is not None
        and w["startMinute"] > w["endMinute"]
        and _minute_of_day(utc_seconds, zone) < w["endMinute"]
    ):
        day_index -= 1
    if mode == "day":
        return day_index
    return math.floor((day_index + 3) / 7)


# --- volume-profile ---------------------------------------------------------

def compute_volume_profile(
    bars: list[dict[str, Any]], tick_size: float, value_area_percent: float = 0.7
) -> dict[str, Any]:
    """Volume at price, distributing each bar's volume across its range."""
    vol: dict[float, float] = {}
    for b in bars:
        buckets = price_buckets(b["low"], b["high"], tick_size)
        share = (b.get("volume") or 0) / len(buckets)
        for bk in buckets:
            vol[bk] = vol.get(bk, 0) + share
    bucket_list = [{"price": p, "volume": v} for p, v in sorted(vol.items(), key=lambda kv: -kv[0])]

    if not bucket_list:
        return {"buckets": bucket_list, "poc": 0, "vah": 0, "val": 0, "totalVolume": 0}

    total: float = 0
    for b in bucket_list:
        total += b["volume"]
    poc_idx = 0
    for i in range(1, len(bucket_list)):
        if bucket_list[i]["volume"] > bucket_list[poc_idx]["volume"]:
            poc_idx = i

    upper = lower = poc_idx
    acc = bucket_list[poc_idx]["volume"]
    target = total * value_area_percent
    while acc < target and (upper > 0 or lower < len(bucket_list) - 1):
        up_vol = bucket_list[upper - 1]["volume"] if upper > 0 else -1
        down_vol = bucket_list[lower + 1]["volume"] if lower < len(bucket_list) - 1 else -1
        if up_vol >= down_vol:
            upper -= 1
            acc += bucket_list[upper]["volume"]
        else:
            lower += 1
            acc += bucket_list[lower]["volume"]

    return {
        "buckets": bucket_list,
        "poc": bucket_list[poc_idx]["price"],
        "vah": bucket_list[upper]["price"],
        "val": bucket_list[lower]["price"],
        "totalVolume": total,
    }


# --- tpo --------------------------------------------------------------------

def compute_tpo(bars: list[dict[str, Any]], period_bars: int, tick_size: float,
                value_area_percent: float = 0.7, ib_periods: int = 2) -> dict[str, Any]:
    """Time Price Opportunity: count periods that traded at each price."""
    period = max(1, period_bars)
    count: dict[float, int] = {}
    ib_high = -math.inf
    ib_low = math.inf

    num_periods = math.ceil(len(bars) / period)
    for p in range(num_periods):
        sl = bars[p * period:(p + 1) * period]
        if not sl:
            continue
        pHigh = -math.inf
        pLow = math.inf
        for b in sl:
            pHigh = max(pHigh, b["high"])
            pLow = min(pLow, b["low"])
        if p < ib_periods:
            ib_high = max(ib_high, pHigh)
            ib_low = min(ib_low, pLow)
        for bk in price_buckets(pLow, pHigh, tick_size):
            count[bk] = count.get(bk, 0) + 1

    buckets = [{"price": p, "count": c} for p, c in sorted(count.items(), key=lambda kv: -kv[0])]

    if not buckets:
        return {"buckets": buckets, "poc": 0, "vah": 0, "val": 0, "ib": {"high": 0, "low": 0}}

    total: float = 0
    for b in buckets:
        total += b["count"]
    poc_idx = 0
    for i in range(1, len(buckets)):
        if buckets[i]["count"] > buckets[poc_idx]["count"]:
            poc_idx = i

    upper = lower = poc_idx
    acc = buckets[poc_idx]["count"]
    target = total * value_area_percent
    while acc < target and (upper > 0 or lower < len(buckets) - 1):
        up = buckets[upper - 1]["count"] if upper > 0 else -1
        down = buckets[lower + 1]["count"] if lower < len(buckets) - 1 else -1
        if up >= down:
            upper -= 1
            acc += buckets[upper]["count"]
        else:
            lower += 1
            acc += buckets[lower]["count"]

    return {
        "buckets": buckets,
        "poc": buckets[poc_idx]["price"],
        "vah": buckets[upper]["price"],
        "val": buckets[lower]["price"],
        "ib": {"high": ib_high, "low": ib_low},
    }


# --- market-profile ---------------------------------------------------------

def _tpo_letter(period: int) -> str:
    m = ((period % 52) + 52) % 52
    return chr(65 + m) if m < 26 else chr(97 + (m - 26))


def _poc_and_value_area(levels: list[dict[str, Any]], va_pct: float) -> dict[str, float]:
    total = 0
    for level in levels:
        total += level["count"]
    poc_idx = 0
    for i in range(1, len(levels)):
        if levels[i]["count"] > levels[poc_idx]["count"]:
            poc_idx = i
    upper = lower = poc_idx
    acc = levels[poc_idx]["count"]
    target = total * va_pct
    while acc < target and (upper > 0 or lower < len(levels) - 1):
        up = levels[upper - 1]["count"] if upper > 0 else -1
        down = levels[lower + 1]["count"] if lower < len(levels) - 1 else -1
        if up >= down:
            upper -= 1
            acc += levels[upper]["count"]
        else:
            lower += 1
            acc += levels[lower]["count"]
    return {
        "poc": levels[poc_idx]["price"],
        "vah": levels[upper]["price"],
        "val": levels[lower]["price"],
    }


def _tail_run(levels: list[dict[str, Any]], frm: int, step: int) -> int:
    n = 0
    i = frm
    while 0 <= i < len(levels):
        if levels[i]["count"] != 1:
            break
        n += 1
        i += step
    return n


def _classify_day(ib: dict[str, float], high: float, low: float, ext: dict[str, float],
                  levels: list[dict[str, Any]], poc: float) -> str:
    ib_range = ib["high"] - ib["low"]
    rng = high - low
    if rng <= 0 or ib_range <= 0:
        return "normal"
    if ext["up"] > 0 and ext["down"] > 0:
        return "neutral"
    poc_idx = next((i for i, level in enumerate(levels) if level["price"] == poc), -1)
    if poc_idx >= 0 and len(levels) >= 5:
        peak = levels[poc_idx]["count"]
        min_mid = math.inf
        second = 0
        for i in range(len(levels)):
            d = abs(i - poc_idx)
            if d > 1:
                second = max(second, levels[i]["count"])
            if 0 < d <= max(2, math.floor(len(levels) / 4)):
                min_mid = min(min_mid, levels[i]["count"])
        if second >= peak * 0.7 and min_mid <= peak * 0.35:
            return "double-distribution"
    ratio = rng / ib_range
    if ratio >= 2.5:
        return "trend"
    if ratio >= 1.4:
        return "normal-variation"
    return "normal"


def _classify_open(open_: float, first: dict[str, Any] | None, second: dict[str, Any] | None,
                   high: float, low: float) -> str:
    if first is None:
        return "auction"
    rng = high - low
    if rng <= 0:
        return "auction"
    first_range = first["high"] - first["low"]
    pos = (open_ - low) / rng
    if (pos > 0.7 or pos < 0.3) and first_range <= rng * 0.35:
        return "drive"
    if second is not None:
        probed_down = first["low"] < open_ - first_range * 0.25
        probed_up = first["high"] > open_ + first_range * 0.25
        if (probed_down and second["high"] > first["high"]) or (
            probed_up and second["low"] < first["low"]
        ):
            return "test-drive"
        if (probed_up and second["low"] < open_) or (probed_down and second["high"] > open_):
            return "rejection-reverse"
    return "auction"


def compute_market_profile(
    bars: list[dict[str, Any]],
    tick_size: float = 0.05,
    row_ticks: int = 1,
    session: str = "day",
    block_minutes: float = 30,
    value_area_percent: float = 0.7,
    initial_balance_periods: int = 2,
    composite_sessions: int = 1,
    tail_edges: int = 0,
    window: dict[str, Any] | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    """Market profile: session-grouped TPO blocks with POC, VA, tails, types."""
    zone = timezone or DEFAULT_TIMEZONE
    base_tick = tick_size if tick_size > 0 else 0.05
    rt = max(1, math.floor(row_ticks))
    row = base_tick * rt
    block_sec = max(1, _round_half_up(block_minutes * 60))
    va_pct = min(1, max(0, value_area_percent))
    ib_periods = max(1, math.floor(initial_balance_periods))
    merge = max(1, math.floor(composite_sessions))
    win = window
    cal = (win.get("zone") if win is not None else None) or zone

    groups: dict[int, list[dict[str, Any]]] = {}
    order: list[int] = []
    for b in bars:
        if win is not None and not _in_window(b["time"], win, zone):
            continue
        k = _session_key(b["time"], session, cal, win)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(b)

    buckets_of_bars: list[list[dict[str, Any]]] = []
    if merge > 1 and session != "composite":
        for i in range(0, len(order), merge):
            sl: list[dict[str, Any]] = []
            for j in range(i, min(i + merge, len(order))):
                sl.extend(groups[order[j]])
            buckets_of_bars.append(sl)
    else:
        for k in order:
            buckets_of_bars.append(groups[k])

    sessions: list[dict[str, Any]] = []
    for g in buckets_of_bars:
        if not g:
            continue
        first = g[0]["time"]
        anchor = first
        if win is not None and win["startMinute"] != win["endMinute"]:
            d = _window_open_on(first, win, cal, 0)
            anchor = d if d <= first else _window_open_on(first, win, cal, -1)

        acc_map: dict[float, dict[str, Any]] = {}
        by_period: dict[int, dict[str, Any]] = {}
        max_period = 0
        ib_high = -math.inf
        ib_low = math.inf
        total_volume = 0
        high = -math.inf
        low = math.inf

        for b in g:
            period = max(0, math.floor((b["time"] - anchor) / block_sec))
            if period > max_period:
                max_period = period
            if period < ib_periods:
                ib_high = max(ib_high, b["high"])
                ib_low = min(ib_low, b["low"])
            high = max(high, b["high"])
            low = min(low, b["low"])
            vol = b.get("volume") or 0
            total_volume += vol

            p = by_period.get(period)
            if p is None:
                p = {"index": period, "letter": _tpo_letter(period), "startTime": b["time"],
                     "endTime": b["time"], "high": b["high"], "low": b["low"], "volume": 0}
                by_period[period] = p
            p["endTime"] = b["time"]
            p["high"] = max(p["high"], b["high"])
            p["low"] = min(p["low"], b["low"])
            p["volume"] += vol

            cells = price_buckets(b["low"], b["high"], row)
            v_share = vol / max(1, len(cells))
            for price in cells:
                a = acc_map.get(price)
                if a is None:
                    a = {"periods": set(), "volume": 0.0}
                    acc_map[price] = a
                a["periods"].add(period)
                a["volume"] += v_share

        period_detail = sorted(by_period.values(), key=lambda x: x["index"])

        levels: list[dict[str, Any]] = []
        for price, a in acc_map.items():
            ps = sorted(a["periods"])
            levels.append({
                "price": price, "count": len(ps), "periods": ps,
                "volume": a["volume"], "letters": "".join(_tpo_letter(x) for x in ps),
            })
        levels.sort(key=lambda x: -x["price"])
        if not levels:
            continue

        va = _poc_and_value_area(levels, va_pct)

        developing: list[dict[str, Any]] = []
        for p in period_detail:
            upto: list[dict[str, Any]] = []
            for level in levels:
                n = sum(1 for q in level["periods"] if q <= p["index"])
                if n > 0:
                    upto.append({"price": level["price"], "count": n})
            if upto:
                r = _poc_and_value_area(upto, va_pct)
                developing.append({"periodIndex": p["index"], "time": p["endTime"], **r})

        single_prints: list[float] = []
        for i in range(1, len(levels) - 1):
            if levels[i]["count"] == 1:
                single_prints.append(levels[i]["price"])

        buying_tail = None
        selling_tail = None
        if tail_edges > 0:
            top = _tail_run(levels, 0, 1)
            if top >= tail_edges:
                selling_tail = {"high": levels[0]["price"], "low": levels[top - 1]["price"]}
            bot = _tail_run(levels, len(levels) - 1, -1)
            if bot >= tail_edges:
                buying_tail = {
                    "high": levels[len(levels) - bot]["price"],
                    "low": levels[len(levels) - 1]["price"],
                }

        if math.isfinite(ib_high):
            ib = {"high": ib_high, "low": ib_low}
        else:
            ib = {"high": levels[0]["price"], "low": levels[-1]["price"]}
        range_extension = {"up": max(0, high - ib["high"]), "down": max(0, ib["low"] - low)}

        volume_poc = levels[0]["price"]
        max_vol = -1
        for level in levels:
            if level["volume"] > max_vol:
                max_vol = level["volume"]
                volume_poc = level["price"]

        sessions.append({
            "startTime": first,
            "endTime": g[-1]["time"],
            "levels": levels,
            "poc": va["poc"], "vah": va["vah"], "val": va["val"],
            "high": high, "low": low,
            "open": g[0]["open"],
            "close": g[-1]["close"],
            "periods": max_period + 1,
            "periodDetail": period_detail,
            "initialBalance": ib,
            "rangeExtension": range_extension,
            "singlePrints": single_prints,
            "buyingTail": buying_tail,
            "sellingTail": selling_tail,
            "poorHigh": levels[0]["count"] > 1,
            "poorLow": levels[-1]["count"] > 1,
            "developing": developing,
            "dayType": _classify_day(ib, high, low, range_extension, levels, va["poc"]),
            "openType": _classify_open(g[0]["open"],
                                       period_detail[0] if period_detail else None,
                                       period_detail[1] if len(period_detail) > 1 else None,
                                       high, low),
            "volumePoc": volume_poc,
            "totalVolume": total_volume,
        })

    options = {
        "tickSize": tick_size, "rowTicks": rt, "session": session, "blockMinutes": block_minutes,
        "valueAreaPercent": value_area_percent, "initialBalancePeriods": initial_balance_periods,
        "compositeSessions": composite_sessions, "tailEdges": tail_edges, "timezone": zone,
    }
    return {"sessions": sessions, "options": options}


def naked_levels(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Prior-session levels no later session has traded back through — the
    "naked" POC / VAH / VAL that so often act as magnets. Oldest first, each
    tagged with the session it came from."""
    out: list[dict[str, Any]] = []
    sessions = result["sessions"]
    for i in range(len(sessions)):
        for kind in ("poc", "vah", "val"):
            price = sessions[i][kind]
            touched = False
            for j in range(i + 1, len(sessions)):
                if price <= sessions[j]["high"] and price >= sessions[j]["low"]:
                    touched = True
                    break
            if not touched:
                out.append({"time": sessions[i]["startTime"], "price": price, "kind": kind})
    return out


def row_of(price: float, options: dict[str, Any]) -> float:
    """Round a price onto the profile's row grid — handy for hit-testing."""
    return bucket_price(
        price,
        options["tickSize"] * max(1, math.floor(options["rowTicks"])),
    )


# --- footprint --------------------------------------------------------------

def compute_footprint(
    time: int, trades: list[dict[str, Any]], tick_size: float, row_ticks: int = 1
) -> dict[str, Any]:
    """One bar's footprint from its classified bid/ask trades.

    The shape — `cells`, `delta`, the running `minDelta`/`maxDelta`, `rowSize`,
    `tradeCount` and (when there are trades) `open`/`close`/`high`/`low` — mirrors
    openalgo-charts 2.1.8's `computeFootprint` field for field, because the pinned
    golden records the whole object and the parity gate compares it exactly. The
    running extremes are accumulated in *trade* order as the reference does, not
    derived from the sorted cells afterwards: the two orders disagree in the last
    bits of a float sum, and min/max over an accumulator is order-sensitive in the
    values it sees, not just in the sum.

    `open`/`close`/`high`/`low` are the trades' own printed prices, not the bucket
    prices the cells are keyed by. They are absent — not zero — when there are no
    trades, so a consumer cannot mistake an empty bar for one that traded at zero.
    """
    row = tick_size * max(1, math.floor(row_ticks))
    cells_map: dict[float, dict[str, Any]] = {}
    delta = 0.0
    min_delta = 0.0
    max_delta = 0.0
    for t in trades:
        price = bucket_price(t["price"], row)
        cell = cells_map.get(price)
        if cell is None:
            cell = {"price": price, "bidVol": 0.0, "askVol": 0.0}
            cells_map[price] = cell
        if t["side"] == "bid":
            cell["bidVol"] += t["qty"]
            delta -= t["qty"]
        else:
            cell["askVol"] += t["qty"]
            delta += t["qty"]
        min_delta = min(min_delta, delta)
        max_delta = max(max_delta, delta)
    cells = sorted(cells_map.values(), key=lambda c: -c["price"])
    out: dict[str, Any] = {
        "time": time,
        "cells": cells,
        "delta": delta,
        "minDelta": min_delta,
        "maxDelta": max_delta,
        "rowSize": row,
        "tradeCount": len(trades),
    }
    if trades:
        prices = [t["price"] for t in trades]
        out["open"] = prices[0]
        out["close"] = prices[-1]
        out["high"] = max(prices)
        out["low"] = min(prices)
    return out


def diagonal_imbalances(cells: list[dict[str, Any]], ratio: float = 3) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(len(cells)):
        here = cells[i]
        below = cells[i + 1] if i + 1 < len(cells) else None
        above = cells[i - 1] if i - 1 >= 0 else None
        if below is not None and here["askVol"] >= ratio * max(1, below["bidVol"]):
            out.append({"price": here["price"], "side": "buy"})
        if above is not None and here["bidVol"] >= ratio * max(1, above["askVol"]):
            out.append({"price": here["price"], "side": "sell"})
    return out


def cumulative_delta(bars: list[dict[str, Any]]) -> list[float]:
    out: list[float] = []
    acc = 0
    for b in bars:
        acc += b["delta"]
        out.append(acc)
    return out


def stacked_imbalances(
    cells: list[dict[str, Any]], ratio: float = 3, min_stack: int = 3
) -> list[dict[str, Any]]:
    imb = diagonal_imbalances(cells, ratio)
    by_side: dict[float, str] = {}
    for i in imb:
        by_side[i["price"]] = i["side"]
    out: list[dict[str, Any]] = []
    run: dict[str, Any] | None = None
    for c in cells:
        side = by_side.get(c["price"])
        if side is not None and (run is None or run["side"] == side):
            if run is None:
                run = {"side": side, "prices": []}
            run["prices"].append(c["price"])
        else:
            if run is not None and len(run["prices"]) >= min_stack:
                out.append({
                    "startPrice": run["prices"][0],
                    "endPrice": run["prices"][-1],
                    "side": run["side"],
                    "count": len(run["prices"]),
                })
            run = {"side": side, "prices": [c["price"]]} if side is not None else None
    if run is not None and len(run["prices"]) >= min_stack:
        out.append({
            "startPrice": run["prices"][0],
            "endPrice": run["prices"][-1],
            "side": run["side"],
            "count": len(run["prices"]),
        })
    return out


# --- dispatcher -------------------------------------------------------------

PROFILE_SPECS: dict[str, dict[str, Any]] = {
    "volume-profile": {
        "id": "volume-profile",
        "name": "Volume Profile",
        "function": compute_volume_profile,
        "params": {"tick_size": 0.1, "value_area_percent": 0.7},
    },
    "tpo": {
        "id": "tpo",
        "name": "Time Price Opportunity",
        "function": compute_tpo,
        "params": {"period_bars": 30, "tick_size": 0.1, "value_area_percent": 0.7, "ib_periods": 2},
    },
    "market-profile": {
        "id": "market-profile",
        "name": "Market Profile",
        "function": compute_market_profile,
        "params": {
            "tick_size": 0.1,
            "row_ticks": 1,
            "session": "day",
            "block_minutes": 30,
            "value_area_percent": 0.7,
            "initial_balance_periods": 2,
            "composite_sessions": 1,
            "tail_edges": 0,
        },
    },
    "footprint": {
        "id": "footprint",
        "name": "Footprint",
        "function": compute_footprint,
        "params": {"time": 0, "tick_size": 0.1, "row_ticks": 1},
    },
}


def compute_profile(pid: str, bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    spec = PROFILE_SPECS.get(pid)
    if spec is None:
        raise ValueError(f"unknown profile id: {pid!r}")
    kwargs: dict[str, Any] = {}
    for key, default in spec["params"].items():
        value = params.get(key, default)
        if isinstance(default, bool):
            kwargs[key] = bool(value)
        elif isinstance(default, int):
            kwargs[key] = int(value)
        elif isinstance(default, float):
            kwargs[key] = float(value)
        else:
            kwargs[key] = value
    if pid == "footprint":
        return spec["function"](kwargs.pop("time"), bars, **kwargs)
    return spec["function"](bars, **kwargs)
