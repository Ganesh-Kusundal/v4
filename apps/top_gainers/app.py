"""Streamlit app — top gainers (09:50 → 15:15 window) with candlesticks + volume.

Run from the repo root:

    .venv/bin/streamlit run apps/top_gainers/app.py

Data source: the local OHLCV parquet datalake (data/ohlcv/symbol=…/data.parquet),
read directly with DuckDB — no broker or trading-package imports. All bars are
tz-naive IST wall time, so comparisons use naive TIME/TIMESTAMP literals.

Window (matches the research queries this app visualizes):
  gain  = close@15:15 / close@09:50 - 1     (per symbol, same day)
  rank  = that window gain, descending, top N
  chart = 09:15 → 15:29 session bars for the selected day & symbol
"""

from __future__ import annotations

import threading
from datetime import date, time, timedelta

import duckdb
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

LAKE_GLOB = "data/ohlcv/**/data.parquet"
SESSION_LO, SESSION_HI = "09:15", "15:29"
GAIN_FROM, GAIN_TO = "09:50", "15:15"  # the ranking window
OPEN_PROFILE_TO = "09:45"  # left-side opening profile covers 09:15 → this time
# Candle timeframes for the chart. Matches the frontend interval picker in
# frontend/src/tier2.ts (INTERVAL_OPTIONS), minus 'D' (daily doesn't make
# sense within a single trading session).
TIMEFRAME_OPTIONS = ["1m", "5m", "15m", "30m", "1h"]

# ---- Volume-profile execution model (scanner mode) -----------------------
# The strategy trades each pick against its OWN 09:15→09:50 volume profile:
# VAH/VAL are the acceptance edges, POC the fairest price, and the HVNs
# outside value the continuation targets.
VP_LEVERAGE = 5.0     # max notional = 5 × equity
VP_ALLOC_PCT = 95.0   # of equity, on top of the leverage cap (so 4.75 × eq)
VP_EOD = time(15, 15)  # every position force-closed here
VP_ENTRY_T = time(9, 50)  # first bar the strategy may act on
# Trading cost charged on BOTH entry and exit notional, in bps of notional:
# intraday brokerage + STT (0.025% sell) + exchange/GST/stamp ≈ 3-4 bps, plus
# spread/slippage. Every round-trip pays it twice, so it decides the result.
VP_COST_BPS = 5.0
VP_BUF_PCT = 10.0     # stop buffer, as % of the value-area width
VP_CONFIRM = 2        # closes beyond an edge that count as acceptance
VP_TARGET_VA = "Value area (POC / opposite edge)"
VP_TARGET_HVN = "Next high-volume node"
VP_TARGET_EOD = "EOD 15:15 only"
VP_TARGET_MODES = (VP_TARGET_VA, VP_TARGET_HVN, VP_TARGET_EOD)
VP_SETUP_CODE = {"open": "O", "fade": "F", "break": "B", "re-entry": "R"}

st.set_page_config(page_title="Top Gainers", page_icon="📈", layout="wide")
st.title("📈 Top Gainers — 09:50 → 15:15 window")
st.caption(
    f"Ranked by close@{GAIN_TO} / close@{GAIN_FROM} − 1 within the session. "
    "Candles are full-session 09:15–15:29; ranking uses only the window endpoints."
)


@st.cache_resource(ttl="10m")
def lake_dates() -> list[date]:
    """Distinct trading days present in the lake (desc) — for the date picker."""
    rows = _query(
        "SELECT DISTINCT timestamp::DATE AS d FROM read_parquet(?, hive_partitioning=true) "
        "ORDER BY d DESC",
        [LAKE_GLOB],
    )
    # .df() yields datetime64 Timestamps; the slider and cache keys want
    # plain dates (Timestamps render as "... 00:00:00" everywhere).
    return [ts.date() for ts in rows["d"]]


@st.cache_data(ttl="10m")
def day_universe(d: date) -> list[str]:
    """Symbols with bars on *d* — the ranking population."""
    rows = _query(
        "SELECT DISTINCT symbol FROM read_parquet(?, hive_partitioning=true) "
        "WHERE timestamp::DATE = ?",
        [LAKE_GLOB, d],
    )
    return sorted(rows["symbol"].tolist())


@st.cache_data(ttl="10m")
def all_symbols() -> list[str]:
    """Every symbol present anywhere in the lake — the custom-symbol picker."""
    rows = _query(
        "SELECT DISTINCT symbol FROM read_parquet(?, hive_partitioning=true)",
        [LAKE_GLOB],
    )
    return sorted(rows["symbol"].tolist())


@st.cache_data(ttl="10m")
def scanner_0945(d: date) -> pd.DataFrame:
    """Rank the universe using only information available by 09:45.

    Per symbol: opening gap vs the prior close, the opening drive
    (09:15→09:45 move), opening-range volatility expansion (OR range vs the
    symbol's 20-session average daily range) and a volume surge ratio (OR
    volume vs its 20-session average). No look-ahead enters the ranking;
    the actual window outcome is joined at display time for evaluation only.
    """
    sql = f"""
    WITH today AS (
      SELECT symbol,
        first(open ORDER BY timestamp)  AS open_d,
        last(close ORDER BY timestamp)  AS close_or,
        max(high)                       AS high_or,
        min(low)                        AS low_or,
        sum(volume)                     AS vol_or
      FROM read_parquet(?, hive_partitioning=true)
      WHERE timestamp::DATE = ?
        AND CAST(timestamp AS TIME) BETWEEN TIME '{SESSION_LO}' AND TIME '{OPEN_PROFILE_TO}'
      GROUP BY symbol
    ),
    daily AS (
      SELECT symbol,
        timestamp::DATE AS day,
        first(open ORDER BY timestamp) AS o,
        max(high) AS h,
        min(low)  AS l,
        last(close ORDER BY timestamp) AS c,
        sum(volume) AS v_or
      FROM read_parquet(?, hive_partitioning=true)
      WHERE timestamp::DATE < ?
        AND CAST(timestamp AS TIME) BETWEEN TIME '{SESSION_LO}' AND TIME '{SESSION_HI}'
      GROUP BY symbol, day
    ),
    ranked AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY day DESC) AS rn
      FROM daily
    ),
    base AS (
      SELECT symbol,
        avg((h - l) / NULLIF(o, 0)) AS avg_daily_range,
        avg(v_or)                   AS avg_or_volume,
        count(*)                    AS n_days
      FROM ranked WHERE rn <= 20 GROUP BY symbol
    ),
    prev AS (
      SELECT symbol, c AS prev_close FROM ranked WHERE rn = 1
    ),
    post AS (
      -- close at the window end (15:15): the research target's endpoint
      SELECT symbol, arg_max(close, timestamp) FILTER (
        WHERE CAST(timestamp AS TIME) <= TIME '{GAIN_TO}'
      ) AS close_to
      FROM read_parquet(?, hive_partitioning=true)
      WHERE timestamp::DATE = ?
      GROUP BY symbol
    )
    SELECT t.symbol, t.open_d, t.close_or, t.vol_or,
      t.close_or AS px_0945,
      round(100.0 * (t.open_d    / NULLIF(p.prev_close, 0) - 1), 2)   AS gap_pct,
      round(100.0 * (t.close_or  / NULLIF(t.open_d, 0)     - 1), 2)   AS or_move_pct,
      round(100.0 * (t.high_or - t.low_or) / NULLIF(t.open_d, 0), 2)  AS or_range_pct,
      round(100.0 * (t.high_or - t.close_or) / NULLIF(t.open_d, 0), 2) AS off_high_pct,
      round(100.0 * (t.close_or - t.low_or) / NULLIF(t.open_d, 0), 2)  AS off_low_pct,
      round(((t.high_or - t.low_or) / NULLIF(t.open_d, 0))
            / NULLIF(b.avg_daily_range, 0), 2)                        AS rv_ratio,
      round(t.vol_or / NULLIF(b.avg_or_volume, 0), 2)                 AS vol_ratio,
      -- research target: 09:45 → 15:15 forward move (evaluation column)
      round(100.0 * (pt.close_to / NULLIF(t.close_or, 0) - 1), 2)     AS fwd_945_pct,
      b.n_days
    FROM today t
    JOIN prev p USING (symbol)
    JOIN base b USING (symbol)
    JOIN post pt USING (symbol)
    WHERE p.prev_close > 0 AND t.open_d > 0 AND b.n_days >= 10 AND t.vol_or > 0
      AND pt.close_to > 0
    """
    scan = _query(sql, [LAKE_GLOB, d, LAKE_GLOB, d, LAKE_GLOB, d])
    if not isinstance(scan, pd.DataFrame) or scan.empty:
        return pd.DataFrame()
    # Full-lake IC study (171 sessions × 500 symbols, ~85k symbol-days,
    # poc/research/scan_0945_forward.py — LOOK-AHEAD-FREE): no pre-09:45
    # OHLCV feature predicts the *direction* of the 09:45→15:15 move
    # (all |IC| < 0.05, ICIR < 0.6). Elevated ≥+2% hit rates from
    # ranking by range/volume are volatility selection, not edge —
    # picks hit −2% just as often. Rankings below exist as exploration
    # lenses, not signals.
    scan["score_movers"] = scan.off_high_pct.rank(pct=True)
    scan["score_grinders"] = (-scan.off_low_pct).rank(pct=True)
    scan["score_fade"] = (-scan.or_move_pct).rank(pct=True)
    return scan.reset_index(drop=True)


@st.cache_data(ttl="10m")
def window_gainers(d: date) -> pd.DataFrame:
    """Per-symbol window gains for one day, plus context columns."""
    sql = f"""
    WITH win AS (
      SELECT symbol,
        first(open  ORDER BY timestamp) AS open,
        max(high)                       AS high,
        min(low)                        AS low,
        last(close ORDER BY timestamp)  AS close,
        last(close ORDER BY timestamp) FILTER (
          WHERE CAST(timestamp AS TIME) <= TIME '{GAIN_FROM}') AS px_from,
        last(close ORDER BY timestamp) FILTER (
          WHERE CAST(timestamp AS TIME) <= TIME '{GAIN_TO}')   AS px_to,
        sum(volume)                     AS volume
      FROM read_parquet(?, hive_partitioning=true)
      WHERE timestamp::DATE = ?
        AND CAST(timestamp AS TIME) BETWEEN TIME '{SESSION_LO}' AND TIME '{SESSION_HI}'
      GROUP BY symbol
    )
    SELECT symbol, px_from, px_to, open, high, low, close, volume,
      round((px_to / px_from - 1) * 100, 2) AS window_gain_pct,
      round((close / open - 1) * 100, 2)     AS intraday_pct
    FROM win
    WHERE px_from > 0 AND px_from IS NOT NULL AND px_to IS NOT NULL
    ORDER BY window_gain_pct DESC
    """
    return _query(sql, [LAKE_GLOB, d])


@st.cache_data(ttl="10m")
def bars(symbol: str, d: date, timeframe: str = "1m") -> pd.DataFrame:
    """Full-session bars for one symbol/day, resampled to *timeframe*.

    The lake stores 1m bars; coarser timeframes are aggregated here with
    market-aligned pandas resampling (minute bins align to the wall clock so
    5m bins start at 09:15, 09:20, … matching the session grid).
    """
    sql = f"""
    SELECT timestamp, open, high, low, close, volume
    FROM read_parquet(?, hive_partitioning=true)
    WHERE symbol = ? AND timestamp::DATE = ?
      AND CAST(timestamp AS TIME) BETWEEN TIME '{SESSION_LO}' AND TIME '{SESSION_HI}'
    ORDER BY timestamp
    """
    df = _query(sql, [LAKE_GLOB, symbol, d])
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    return _resample_bars(df, timeframe)


def _vp_profile(symbol: str, d: date) -> dict | None:
    """The pre-window volume profile the session trades against.

    Built from this symbol's 1m bars STRICTLY BEFORE 09:50, so the POC, the
    70% value area (VAH/VAL) and the HVN/LVN nodes are all knowable at the
    entry candle — no look-ahead anywhere in the level set.
    """
    pre = bars(symbol, d, "1m")
    if pre is None or pre.empty:
        return None
    pre = pre[pre["timestamp"].dt.time < VP_ENTRY_T]
    if pre.empty:
        return None
    return _volume_profile(pre)


def _vp_levels(prof: dict, buf_pct: float, target_mode: str) -> dict:
    """Trade levels derived from a profile: edges, stop buffer, HVN targets.

    ``buf_pct`` is the stop buffer as a % of the value-area WIDTH, so the stop
    scales with how wide the morning's acceptance was instead of a fixed tick
    count. ``up``/``dn`` are the high-volume nodes outside the value area —
    the natural continuation targets for an accepted breakout.
    """
    vah, val, poc = float(prof["vah"]), float(prof["val"]), float(prof["poc"])
    hvns = sorted(float(x) for x in (prof.get("hvn") or []))
    return {
        "poc": poc,
        "vah": vah,
        "val": val,
        "buf": max((vah - val) * float(buf_pct) / 100.0, 1e-9),
        "up": [h for h in hvns if h > vah],
        "dn": [h for h in hvns if h < val],
        "target_mode": str(target_mode),
    }


def _vp_target(lv: dict, side: int, setup: str) -> float | None:
    """Profit target for a setup — ``None`` = ride it to the 15:15 close."""
    if lv["target_mode"] == VP_TARGET_EOD:
        return None
    if setup == "fade":
        return float(lv["poc"])            # rotation back to the fairest price
    if setup == "break":
        pool = lv["up"] if side > 0 else lv["dn"]
        if lv["target_mode"] == VP_TARGET_HVN and pool:
            return float(pool[0] if side > 0 else pool[-1])
        return None                        # acceptance: let it run to the close
    return float(lv["vah"]) if side > 0 else float(lv["val"])  # re-entry


def _vp_regime(open_px: float, lv: dict) -> str:
    """Where the 09:50 open sits vs the pre-window value area."""
    if open_px > lv["vah"]:
        return "above"
    if open_px < lv["val"]:
        return "below"
    return "inside"


def _vp_setup(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    i: int,
    lv: dict,
    regime: str,
    confirm: int,
    allow_fade: bool,
    allow_break: bool,
    allow_reentry: bool,
) -> tuple[int, float, float, float | None, str] | None:
    """The volume-profile entry decision on bar *i*.

    Three canonical setups, each long/short symmetric (value-area rotation vs
    acceptance, plus the "80% rule" for opens outside value):

      • ``re-entry`` — the 09:50 open was OUTSIDE value and price closes back
                       INSIDE it: trade ACROSS the value area to the far edge.
      • ``fade``     — price tags a value-area edge and closes back inside it:
                       rotation to the POC (VAH acts as resistance, VAL as
                       support — the most-watched levels in the profile).
      • ``break``    — acceptance: ``confirm`` consecutive closes BEYOND an
                       edge: trade with the move, stop back at the edge.

    Returns ``(side, entry_px, stop_px, target_px_or_None, setup)`` or None.
    The fill is this bar's close (bar-based simulation) and the stop always
    sits beyond the level that defined the setup.
    """
    close = float(closes[i])
    vah, val, buf = lv["vah"], lv["val"], lv["buf"]

    # 1) 80% rule: open outside value, close back inside → trade across it.
    if allow_reentry:
        if regime == "above" and close < vah:
            return (-1, close, vah + buf, _vp_target(lv, -1, "re-entry"), "re-entry")
        if regime == "below" and close > val:
            return (1, close, val - buf, _vp_target(lv, 1, "re-entry"), "re-entry")

    # 2) Fade a rejected edge back toward the POC.
    if allow_fade:
        if float(highs[i]) >= vah and close < vah:
            return (-1, close, vah + buf, _vp_target(lv, -1, "fade"), "fade")
        if float(lows[i]) <= val and close > val:
            return (1, close, val - buf, _vp_target(lv, 1, "fade"), "fade")

    # 3) Acceptance: consecutive closes beyond an edge → trade the trend.
    if allow_break and i + 1 >= int(confirm):
        seg = slice(i - int(confirm) + 1, i + 1)
        if bool((closes[seg] > vah).all()):
            return (1, close, vah - buf, _vp_target(lv, 1, "break"), "break")
        if bool((closes[seg] < val).all()):
            return (-1, close, val + buf, _vp_target(lv, -1, "break"), "break")
    return None


def _simulate_vp(
    picks: list[str],
    d: date,
    tf: str,
    buf_pct: float,
    confirm: int,
    target_mode: str,
    start_equity: float,
    risk_pct: float,
    allow_fade: bool = True,
    allow_break: bool = True,
    allow_reentry: bool = True,
    open_entry: bool = True,
    leverage: float = VP_LEVERAGE,
    alloc_pct: float = VP_ALLOC_PCT,
    cost_bps: float = VP_COST_BPS,
    gap_bars: int = 2,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, dict]]:
    """One shared-capital account trading every scan pick on volume profile.

    Rules (see docs/design/2026-09-14-volume-profile-execution.md):
      • Levels come from the pick's OWN 09:15→09:50 profile — POC, VAH, VAL
        and the HVN/LVN nodes — fixed for the whole session, all knowable at
        the entry candle. No look-ahead.
      • Opening position (``open_entry``): an open OUTSIDE value is an
        imbalance, so take the day-type side at the 09:50 open — long above
        VAH / short below VAL — with the stop back at the edge. An open
        inside value waits for a setup.
      • Afterwards the three profile setups fire (long & short): re-entry
        across value (80% rule), edge fade to the POC, and ``confirm``-bar
        acceptance beyond an edge.
      • Exits: stop (checked BEFORE the target, so a bar spanning both is
        scored as a loss), target, or the 15:15 close. One position per
        symbol, no pyramiding, ``gap_bars`` cooldown after each exit.
      • Sizing: qty = equity × risk% ÷ |entry − stop| (whole shares), capped
        by an equal per-pick budget and the total cap equity × alloc% ×
        leverage. Costs (bp per side) are charged on BOTH the entry and the
        exit notional, and equity compounds across trades.

    Returns (trades, fills, equity_curve, per_symbol) where ``per_symbol``
    carries each pick's profile/levels/regime for the chart and the per-stock
    table.
    """
    cols = [
        "symbol", "setup", "side", "entry_time", "entry", "stop", "target",
        "exit_time", "exit", "qty", "pnl", "cost", "ret_pct", "equity_after",
        "exit_reason",
    ]
    ctxs: dict[str, dict] = {}
    per_symbol: dict[str, dict] = {}
    for sym in picks:
        prof = _vp_profile(sym, d)
        if prof is None:
            continue
        bb = bars(sym, d, tf)
        if bb is None or bb.empty:
            continue
        bb = bb.reset_index(drop=True)
        tt = bb["timestamp"].dt.time
        first = np.flatnonzero((tt >= VP_ENTRY_T).to_numpy())
        if not len(first):
            continue
        first_i = int(first[0])
        lv = _vp_levels(prof, buf_pct, target_mode)
        regime = _vp_regime(float(bb["open"].iloc[first_i]), lv)
        ctxs[sym] = {
            "bb": bb, "tt": tt,
            "close": bb["close"].to_numpy(dtype=float),
            "high": bb["high"].to_numpy(dtype=float),
            "low": bb["low"].to_numpy(dtype=float),
            "lv": lv, "regime": regime, "first_i": first_i, "opened_at": -1,
        }
        per_symbol[sym] = {"prof": prof, "lv": lv, "regime": regime}
    if not ctxs:
        return pd.DataFrame(columns=cols), pd.DataFrame(), pd.DataFrame(), per_symbol

    trades: list[dict] = []
    fills: list[dict] = []
    eq_pts: list[dict] = []
    equity = float(start_equity)
    open_notional = 0.0
    state: dict[str, dict] = {}
    cooldown: dict[str, int] = {s: 0 for s in ctxs}
    opened_day: set[str] = set()

    # Flatten all bars into one chronological stream with per-symbol pointers.
    stream: list[tuple[pd.Timestamp, str, int]] = []
    for sym, cx in ctxs.items():
        for i in range(len(cx["bb"])):
            stream.append((cx["bb"]["timestamp"].iloc[i], sym, i))
    stream.sort(key=lambda r: (r[0], r[1]))

    def _close(sym: str, i: int, px: float, reason: str) -> None:
        nonlocal equity, open_notional
        cx = ctxs[sym]
        bb = cx["bb"]
        st = state.pop(sym)
        gross = (px - st["entry"]) * st["qty"] * st["side"]
        exit_cost = px * st["qty"] * (cost_bps / 10_000.0)
        # Equity already paid the entry-side cost at open — add only the gross
        # minus the exit-side cost here, so equity_end stays CAP + Σ(pnl).
        equity += gross - exit_cost
        net = gross - st["cost"] - exit_cost  # full round-trip, both sides
        open_notional -= st["entry"] * st["qty"]
        cooldown[sym] = i + int(gap_bars)
        trades.append({
            "symbol": sym,
            "setup": st["setup"],
            "side": "LONG" if st["side"] > 0 else "SHORT",
            "entry_time": st["entry_time"], "entry": round(st["entry"], 2),
            "stop": round(st["stop"], 2),
            "target": None if st["target"] is None else round(st["target"], 2),
            "exit_time": bb["timestamp"].iloc[i], "exit": round(px, 2),
            "qty": int(round(st["qty"])),
            "pnl": round(net, 2),
            "cost": round(st["cost"] + exit_cost, 2),
            "ret_pct": round(100.0 * net / max(start_equity, 1e-9), 4),
            "equity_after": round(equity, 2),
            "exit_reason": reason,
        })
        fills.append({"symbol": sym, "timestamp": bb["timestamp"].iloc[i],
                      "price": px, "marker": "×", "kind": "exit",
                      "setup": st["setup"],
                      "side": "LONG" if st["side"] > 0 else "SHORT"})
        eq_pts.append({"timestamp": bb["timestamp"].iloc[i], "equity": equity})

    def _open(sym: str, i: int, side: int, stop_px: float,
              target: float | None, setup: str, px: float | None = None) -> None:
        nonlocal open_notional, equity
        cx = ctxs[sym]
        bb = cx["bb"]
        px = float(cx["close"][i]) if px is None else float(px)
        stop_px = float(stop_px)
        if not (np.isfinite(px) and np.isfinite(stop_px)):
            return
        rps = abs(px - stop_px)
        if rps <= 0 or equity <= 0:
            return
        room_notional = max(equity * (alloc_pct / 100.0) * leverage - open_notional, 0.0)
        # Equal notional budget per pick so one symbol cannot starve the rest;
        # budget freed by an exited pick flows back through room_notional.
        per_pick_cap = equity * (alloc_pct / 100.0) * leverage / max(len(ctxs), 1)
        q = min(
            equity * (risk_pct / 100.0) / rps,   # risk-based size
            room_notional / px,                   # portfolio notional cap
            per_pick_cap / px,                    # per-pick budget
        )
        q = float(np.floor(q))  # whole shares
        if not np.isfinite(q) or q < 1:
            return
        state[sym] = {"side": side, "qty": float(q), "entry": px,
                      "stop": stop_px, "target": target, "setup": setup,
                      "entry_time": bb["timestamp"].iloc[i],
                      "cost": px * q * (cost_bps / 10_000.0)}
        cx["opened_at"] = i
        open_notional += px * q
        # Charge the entry-side cost; it also shrinks room for later entries.
        equity -= state[sym]["cost"]
        fills.append({"symbol": sym, "timestamp": bb["timestamp"].iloc[i],
                      "price": px, "marker": "▲" if side > 0 else "▼",
                      "kind": "entry", "setup": setup,
                      "side": "LONG" if side > 0 else "SHORT"})

    eq_pts.append({"timestamp": stream[0][0], "equity": equity})
    for _ts, sym, i in stream:
        cx = ctxs[sym]
        bb = cx["bb"]
        tt = cx["tt"].iloc[i]
        px = float(cx["close"][i])
        st = state.get(sym)

        # Hard rule first: everything is flat by the 15:15 close.
        if st is not None and tt >= VP_EOD:
            _close(sym, i, px, "EOD 15:15")
            continue

        # Manage an open position. The stop is checked BEFORE the target, so a
        # single bar spanning both is scored as a loss (conservative). Neither
        # is checked on the entry bar: this bar's high/low already happened
        # before the close we filled at.
        if st is not None and i > cx["opened_at"]:
            hi, lo = float(cx["high"][i]), float(cx["low"][i])
            if st["side"] > 0:
                if lo <= st["stop"]:
                    _close(sym, i, st["stop"], "stop")
                    continue
                if st["target"] is not None and hi >= st["target"]:
                    _close(sym, i, st["target"], "target")
                    continue
            else:
                if hi >= st["stop"]:
                    _close(sym, i, st["stop"], "stop")
                    continue
                if st["target"] is not None and lo <= st["target"]:
                    _close(sym, i, st["target"], "target")
                    continue

        if sym in state or tt < VP_ENTRY_T or tt >= VP_EOD or i < cx["first_i"]:
            continue

        # Opening position: an open outside value is an imbalance — take the
        # day-type side at the 09:50 open, stop back at the value-area edge.
        if sym not in opened_day:
            opened_day.add(sym)
            lv = cx["lv"]
            if open_entry and cx["regime"] != "inside":
                side = 1 if cx["regime"] == "above" else -1
                stop = lv["vah"] - lv["buf"] if side > 0 else lv["val"] + lv["buf"]
                _open(sym, i, side, stop, _vp_target(lv, side, "break"), "open",
                      px=float(bb["open"].iloc[i]))
            continue

        if cooldown.get(sym, 0) > i:
            continue
        sig = _vp_setup(cx["close"], cx["high"], cx["low"], i, cx["lv"],
                        cx["regime"], int(confirm), bool(allow_fade),
                        bool(allow_break), bool(allow_reentry))
        if sig is None:
            continue
        side, _fill, stop_px, target, setup = sig
        _open(sym, i, side, stop_px, target, setup)

    # Safety net: force-close anything the stream missed (data holes at EOD).
    for sym in list(state):
        cx = ctxs[sym]
        _close(sym, len(cx["bb"]) - 1, float(cx["close"][-1]), "EOD 15:15")

    trades_df = pd.DataFrame(trades, columns=cols)
    fills_df = pd.DataFrame(fills)
    eq_df = pd.DataFrame(eq_pts)
    return trades_df, fills_df, eq_df, per_symbol


_DUCK_LOCK = threading.Lock()


@st.cache_resource
def _con():
    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    con.execute("SET threads=4")
    return con


def _query(sql: str, params: list | None = None) -> pd.DataFrame:
    """Run one query on the shared connection, serialized.

    Streamlit runs each script rerun in its own thread; two concurrent
    ``con.execute(...).df()`` calls on ONE DuckDB connection can return None
    or garbage (repro: switch the chart radio while a heavy query is in
    flight). The repo's own ``DuckDBCatalog.execution_lock`` exists for the
    same reason — mirror it here. Retry once on IOException for torn parquet
    reads while a concurrent ``ParquetStorage.upsert`` rewrites a partition.
    """
    args = params if params is not None else []
    with _DUCK_LOCK:
        try:
            return _con().execute(sql, args).df()
        except duckdb.IOException:
            return _con().execute(sql, args).df()


def _resample_bars(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate 1m OHLCV to *timeframe* (market-aligned).

    Pandas minute bins align to the clock, so 5m bins start at 09:15,
    09:20, … — matching the session grid the broker serves.
    """
    if timeframe == "1m" or df.empty:
        return df
    rule = {
        "1m": "1min",
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "1h": "1h",
    }[timeframe]
    return (
        df.set_index("timestamp")
        .sort_index()
        .resample(rule)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    	.dropna()
    	.reset_index()
    )


def _spread_volume_matrix(lows, highs, vols, edges) -> np.ndarray:
    """Per-bar volume-by-price mass over FIXED *edges* — row k is bar k's
    volume spread proportionally across every price bin its [low, high]
    range overlaps (TradingView fills the bar's whole range, not just the
    close). Sharing one spreading rule keeps every profile comparable.
    """
    n_bins = len(edges) - 1
    m = np.zeros((len(lows), n_bins))
    for k, (l, h, v) in enumerate(zip(lows, highs, vols)):
        if not np.isfinite(v) or v <= 0:
            continue
        if h <= l:  # zero-range bar: drop all volume into its bin
            i = int(np.clip(np.searchsorted(edges, l, side="right") - 1, 0, n_bins - 1))
            m[k, i] += v
            continue
        i0 = int(np.clip(np.searchsorted(edges, l, side="right") - 1, 0, n_bins - 1))
        i1 = int(np.clip(np.searchsorted(edges, h, side="left"), 0, n_bins - 1))
        for i in range(i0, i1 + 1):  # volume ∝ overlap of [l, h] with bin i
            ov = min(h, edges[i + 1]) - max(l, edges[i])
            if ov > 0:
                m[k, i] += v * ov / (h - l)
    return m


def _volume_profile(df: pd.DataFrame) -> dict | None:
    """TradingView-style volume-by-price profile for session bars *df*.

    Returns the bins plus the POC and the 70% value area (VAH/VAL), expanded
    outward from the POC taking the larger neighbor each step — the TV
    value-area rule.
    """
    if df.empty:
        return None
    lo, hi = float(df["low"].min()), float(df["high"].max())
    if not hi > lo:
        return None
    n_bins = int(min(80, max(24, round(len(df) / 2))))
    edges = np.linspace(lo, hi, n_bins + 1)
    bin_vol = _spread_volume_matrix(
        df["low"].to_numpy(dtype=float),
        df["high"].to_numpy(dtype=float),
        df["volume"].to_numpy(dtype=float),
        edges,
    ).sum(axis=0)
    if bin_vol.sum() <= 0:
        return None
    poc = int(np.argmax(bin_vol))
    # Value area: walk outward from the POC, absorbing the larger neighbor,
    # until 70% of total volume is covered (TradingView default).
    acc = float(bin_vol[poc])
    target = 0.7 * float(bin_vol.sum())
    up, down = poc + 1, poc - 1
    while acc < target and (up < n_bins or down >= 0):
        up_v = float(bin_vol[up]) if up < n_bins else -1.0
        dn_v = float(bin_vol[down]) if down >= 0 else -1.0
        if up_v >= dn_v:
            acc += up_v
            up += 1
        else:
            acc += dn_v
            down -= 1
    va_top = min(up, n_bins) - 1  # highest bin in the value area
    va_bot = max(down + 1, 0)     # lowest bin in the value area
    mids = (edges[:-1] + edges[1:]) / 2
    # HVN / LVN nodes: HVN = bins that are the max of their neighborhood with
    # volume ≥ 1.2× the profile mean (acceptance zones); LVN = strict local
    # minima ≤ 0.5× the mean sandwiched between two higher bins (fast-move
    # zones the auction rejected). Consecutive hits collapse to the dominant
    # bin; at most 3 of each are returned, ranked by prominence.
    k = max(2, n_bins // 20)
    mean_v = float(bin_vol.mean())
    hvn_idx: list[int] = []
    lvn_idx: list[int] = []
    for i in range(n_bins):
        w = bin_vol[max(0, i - k) : min(n_bins, i + k + 1)]
        if bin_vol[i] >= 1.2 * mean_v and bin_vol[i] == w.max():
            hvn_idx.append(i)
        elif (
            0 < i < n_bins - 1
            and bin_vol[i] <= 0.5 * mean_v
            and bin_vol[i - 1] > bin_vol[i] < bin_vol[i + 1]
        ):
            lvn_idx.append(i)

    def _collapse_runs(idxs: list[int]) -> list[int]:
        out: list[int] = []
        for i in idxs:
            if out and out[-1] == i - 1:
                if bin_vol[i] > bin_vol[out[-1]]:
                    out[-1] = i
            else:
                out.append(i)
        return out

    hvn_idx = _collapse_runs(hvn_idx)
    hvn_idx.sort(key=lambda i: bin_vol[i], reverse=True)
    lvn_idx.sort(key=lambda i: bin_vol[i])
    bins = pd.DataFrame(
        {
            "price": mids,
            "volume": bin_vol,
            "in_va": [va_bot <= i <= va_top for i in range(n_bins)],
            "is_poc": [i == poc for i in range(n_bins)],
        }
    ).query("volume > 0")
    return {
        "bins": bins,
        "bin_w": float(edges[1] - edges[0]),
        "poc": float(mids[poc]),
        "vah": float(edges[va_top + 1]),
        "val": float(edges[va_bot]),
        "hvn": [float(mids[i]) for i in hvn_idx[:3]],
        "lvn": [float(mids[i]) for i in lvn_idx[:3]],
    }


def _developing_poc(hist_df: pd.DataFrame, d: date) -> pd.DataFrame | None:
    """Developing POC: the profile's fairest price as it evolved bar by bar.

    Bins are FIXED over the full price range (context days + whole current
    day), so every step's POC is comparable. Step 0 anchors at the window
    start (09:50) with the pre-window profile's POC; each following step adds
    one of today's bars (cumulative-sum over the pre-spread bar mass, so the
    whole series costs one matrix pass) and records the new argmax bin.
    Returns columns ``timestamp`` and ``poc``.
    """
    today = hist_df[hist_df["day"] == d]
    t = today["timestamp"].dt.time
    win_from = pd.Timestamp(GAIN_FROM).time()
    post = today[t > win_from]
    base = hist_df[(hist_df["day"] < d) | ((hist_df["day"] == d) & (t <= win_from))]
    if today.empty or base.empty or len(hist_df) < 2:
        return None
    lo, hi = float(hist_df["low"].min()), float(hist_df["high"].max())
    if not hi > lo:
        return None
    n_bins = int(min(80, max(24, round(len(hist_df) / 2))))
    edges = np.linspace(lo, hi, n_bins + 1)
    mids = (edges[:-1] + edges[1:]) / 2

    mass = _spread_volume_matrix(
        base["low"].to_numpy(dtype=float),
        base["high"].to_numpy(dtype=float),
        base["volume"].to_numpy(dtype=float),
        edges,
    ).sum(axis=0)
    step_mass = _spread_volume_matrix(
        post["low"].to_numpy(dtype=float),
        post["high"].to_numpy(dtype=float),
        post["volume"].to_numpy(dtype=float),
        edges,
    )
    cum = mass + np.vstack(
        [np.zeros((1, n_bins)), np.cumsum(step_mass, axis=0)]
    )  # row 0 = pre-window profile only (the 09:50 anchor)
    poc_idx = np.argmax(cum, axis=1)

    anchor_ts = today.iloc[0]["timestamp"].replace(
        hour=int(GAIN_FROM.split(":")[0]), minute=int(GAIN_FROM.split(":")[1])
    )
    return pd.DataFrame(
        {
            "timestamp": [anchor_ts, *post["timestamp"].tolist()],
            "poc": [float(mids[poc_idx[0]]), *(mids[poc_idx[1:]].tolist())],
        }
    )


def _add_vp_overlay(
    fig: go.Figure,
    vp: dict,
    side: str = "right",
    axis: str = "x3",
    draw_nodes: bool = True,
    name: str = "Volume profile",
) -> None:
    """Draw the TradingView-style profile on *fig*'s price panel (row 1).

    Horizontal bars hug one edge: ``side="right"`` anchors at the right edge
    and grows leftward; ``side="left"`` anchors at the left edge and grows
    rightward. An invisible overlay *axis* is sized so bars occupy at most the
    25% of the panel next to their edge — both charts use subplots whose row-1
    x axis is ``x``, so ``x3``/``x4`` are free as overlay axes.

    ``draw_nodes`` adds the POC/VAH/VAL guides plus HVN (local volume peaks)
    and LVN (low-volume valleys) lines; the opening-range profile passes
    ``False`` to stay bars-only.
    """
    b = vp["bins"]
    vmax = float(b["volume"].max())
    if side == "right":
        base = vmax - b["volume"]  # anchor the right edge, grow leftward
        poc_c, in_va_c, out_c = (
            "#f2c744",
            "rgba(96,141,255,0.55)",
            "rgba(96,141,255,0.22)",
        )
    else:
        base = np.full(len(b), -3.0 * vmax)  # anchor the left edge, grow right
        poc_c, in_va_c, out_c = (
            "#ff9f1c",
            "rgba(255,159,28,0.5)",
            "rgba(255,159,28,0.2)",
        )
    fig.add_trace(
        go.Bar(
            x=b["volume"],
            y=b["price"],
            base=base,
            orientation="h",
            xaxis=axis,
            width=vp["bin_w"],
            marker_color=np.where(b["is_poc"], poc_c, np.where(b["in_va"], in_va_c, out_c)),
            name=name,
            showlegend=False,
            customdata=np.where(b["in_va"] | b["is_poc"], " · in VA", ""),
            hovertemplate="≈ %{y:,.2f} · vol %{x:,.0f}%{customdata}<extra></extra>",
        )
    )
    # Profile zone = 25% of the panel: the overlay axis spans 4×vmax and the
    # bars live in the quarter next to their edge. (Layout keys want the full
    # property name — "xaxis3" — while traces take the axis id "x3".)
    fig.layout[f"xaxis{axis[1:]}"] = dict(
        overlaying="x",
        side="top",
        range=[-3 * vmax, vmax],
        visible=False,
        fixedrange=True,
        showgrid=False,
    )
    if not draw_nodes:
        return
    for yy, txt, colr, wd, dash in (
        (vp["vah"], f" VAH {vp['vah']:,.2f}", "rgba(140,170,255,0.95)", 1, "dot"),
        (vp["poc"], f" POC {vp['poc']:,.2f}", "#f2c744", 1.5, "solid"),
        (vp["val"], f" VAL {vp['val']:,.2f}", "rgba(140,170,255,0.95)", 1, "dot"),
    ):
        fig.add_hline(
            y=yy,
            line=dict(color=colr, width=wd, dash=dash),
            annotation_text=txt,
            annotation_position="top left",
            row=1,
            col=1,
        )
    # HVN: green dashed (acceptance/magnet zones); LVN: red dotted (fast-move
    # zones). Labels sit on the right so they don't pile onto POC/VAH/VAL.
    for p in vp.get("hvn", []):
        fig.add_hline(
            y=p,
            line=dict(color="#22c55e", width=1, dash="dash"),
            annotation_text=f" HVN {p:,.2f}",
            annotation_position="top right",
            row=1,
            col=1,
        )
    for p in vp.get("lvn", []):
        fig.add_hline(
            y=p,
            line=dict(color="#ef4444", width=1, dash="dot"),
            annotation_text=f" LVN {p:,.2f}",
            annotation_position="bottom right",
            row=1,
            col=1,
        )


def _per_stock_view(
    picks: list[str],
    d: date,
    tf: str,
    per_symbol: dict[str, dict],
    trades: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, dict]]:
    """Per-pick view: value-area location, opening side, its own profile.

    For every scan pick: where the 09:50 open sat vs the pre-window value area
    (the regime that decides the day-type side), the opening entry with its
    stop, this symbol's own 09:15→09:50 profile (POC/VAH/VAL plus HVN/LVN
    counts — all knowable at the entry), and the trade stats joined per symbol
    from the portfolio sim. Returns (table, profiles); ``profiles`` feeds the
    mini charts.
    """
    rows: list[dict] = []
    profiles: dict[str, dict] = {}
    for sym in picks:
        info = per_symbol.get(sym)
        bb = bars(sym, d, tf)
        if info is None or bb is None or bb.empty:
            continue
        prof, lv, regime = info["prof"], info["lv"], info["regime"]
        b = bb.reset_index(drop=True)
        first = np.flatnonzero(
            (b["timestamp"].dt.time >= VP_ENTRY_T).to_numpy()
        )
        if not len(first):
            continue
        open_px = float(b["open"].iloc[int(first[0])])
        side = 1 if regime == "above" else (-1 if regime == "below" else 0)
        stop_px = (
            lv["vah"] - lv["buf"] if side > 0
            else (lv["val"] + lv["buf"] if side < 0 else float("nan"))
        )
        risk_pct = (
            100.0 * abs(open_px - stop_px) / open_px if side else float("nan")
        )
        g = trades[trades.symbol == sym] if not trades.empty else pd.DataFrame()
        n_tr = len(g)
        rows.append({
            "symbol": sym,
            "regime": regime.upper(),
            "va_pos": {"above": "above VAH", "below": "below VAL"}.get(
                regime, "inside VA"
            ),
            "open_0950": open_px,
            "side": "—" if side == 0 else ("LONG" if side > 0 else "SHORT"),
            "stop": stop_px,
            "risk_pct": risk_pct,
            "poc": lv["poc"],
            "vah": lv["vah"],
            "val": lv["val"],
            "hvn": len(prof.get("hvn", [])),
            "lvn": len(prof.get("lvn", [])),
            "trades": n_tr,
            "setups": (
                " ".join(f"{k}×{int(v)}" for k, v in g.setup.value_counts().items())
                if n_tr else "—"
            ),
            "win_pct": float((g.pnl > 0).mean() * 100) if n_tr else float("nan"),
            "pnl": float(g.pnl.sum()) if n_tr else 0.0,
            "exits_stop_target_eod": (
                f"{int((g.exit_reason == 'stop').sum())}/"
                f"{int((g.exit_reason == 'target').sum())}/"
                f"{int((g.exit_reason == 'EOD 15:15').sum())}" if n_tr else "0/0/0"
            ),
        })
        profiles[sym] = {"prof": prof, "entry": open_px, "stop": stop_px,
                         "side": side, "state": f"{regime.upper()} VA"}
    return pd.DataFrame(rows), profiles


def _mini_profile_fig(sym: str, p: dict) -> go.Figure:
    """One pick's pre-window profile (09:15→09:50) with POC/VAH/VAL + entry."""
    prof = p["prof"]
    b = prof["bins"]
    fig = go.Figure(
        go.Bar(
            x=b["volume"], y=b["price"], orientation="h", width=prof["bin_w"],
            marker_color=np.where(
                b["is_poc"], "#f2c744",
                np.where(b["in_va"], "rgba(96,141,255,0.6)", "rgba(96,141,255,0.25)"),
            ),
            hovertemplate="≈ %{y:,.2f} · vol %{x:,.0f}<extra></extra>",
            showlegend=False,
        )
    )
    for yy, txt, colr, dash in (
        (prof["vah"], f"VAH {prof['vah']:,.2f}", "#8caaff", "dot"),
        (prof["poc"], f"POC {prof['poc']:,.2f}", "#f2c744", "solid"),
        (prof["val"], f"VAL {prof['val']:,.2f}", "#8caaff", "dot"),
    ):
        fig.add_hline(
            y=yy, line=dict(color=colr, width=1, dash=dash),
            annotation_text=txt, annotation_position="top left",
            annotation_font_size=9,
        )
    if np.isfinite(p["entry"]):
        fig.add_hline(
            y=p["entry"],
            line=dict(color="#2ca02c" if p["side"] > 0 else "#d62728", width=2),
            annotation_text=f" {GAIN_FROM} {p['entry']:,.2f}",
            annotation_position="bottom right", annotation_font_size=10,
        )
    fig.update_layout(
        title=dict(text=f"{sym} · {p['state']} @ {GAIN_FROM}", font=dict(size=13)),
        height=250, margin=dict(l=6, r=6, t=34, b=6), showlegend=False,
        xaxis=dict(showticklabels=False, showgrid=False, title=None),
        yaxis=dict(tickformat=",.2f", side="right", title=None),
    )
    return fig


# ------------------------------------------------------------------ sidebar
days = lake_dates()
if not days:
    st.error("No data found under data/ohlcv — run from the repo root.")
    st.stop()

with st.sidebar:
    st.subheader("Controls")
    default_day = date(2026, 9, 10) if date(2026, 9, 10) in days else days[0]
    d = st.date_input(
        "Trading day",
        value=default_day,
        min_value=days[-1],
        max_value=days[0],
        help="Session to rank and chart. Must be a day present in the lake.",
    )
    top_n = st.number_input("Top N gainers", min_value=3, max_value=50, value=10)
    tf = st.selectbox(
        "Bar timeframe",
        TIMEFRAME_OPTIONS,
        index=TIMEFRAME_OPTIONS.index("1m"),
        help="Resample session candles to this bar size.",
    )
    ctx_days = st.number_input(
        "Context days",
        min_value=1,
        max_value=30,
        value=5,
        help="Prior sessions to show in the context chart.",
    )
    show_scanner = st.toggle(
        "09:45 scanner",
        value=True,
        help=(
            "Rank the universe using only data through 09:45 — before the "
            f"{GAIN_FROM} ranking window opens — to pick stocks ahead of the move."
        ),
    )
    scan_mode = st.selectbox(
        "Scanner ranking",
        (
            "Movers — dips off OR high (targets ≥+2% into 15:15)",
            "Grinders — least off lows (high win rate)",
            "Fade — short the morning spikers",
        ),
        help=(
            "Full-lake look-ahead-free IC study found NO pre-09:45 OHLCV "
            "feature with directional edge — these are exploration lenses."
        ),
    )
    scan_k = st.slider("Scanner picks (k)", 3, 15, 3)
    scan_min_px = st.number_input(
        "Min price at 09:45",
        min_value=0.0,
        value=0.0,
        step=50.0,
        help="Tradability filter only — no filter creates edge (IC study).",
    )
    scan_min_vx = st.slider(
        "Min volume ratio",
        0.0,
        1.0,
        0.0,
        0.1,
        help="Study: volume floor also reduces hit rate. Liquidity guard only.",
    )
    scan_max_dip = st.number_input(
        "Max dip off OR high % (Movers)",
        min_value=1.0,
        value=100.0,
        step=1.0,
        help="Study: deeper dips recover better monotonically — a cap only hurts.",
    )
    show_candles = st.toggle("Session candlestick chart", value=True)
    show_context = st.toggle("Context path chart", value=True)
    dev_poc = st.toggle(
        "Developing POC line",
        value=False,
        help=(
            "Recompute the volume profile after every bar past "
            f"{GAIN_FROM} and trace how the POC migrated through the session."
        ),
    )
    pick_custom = st.toggle(
        "Custom symbol",
        value=False,
        help="Pick any lake symbol instead of the top-N gainers list.",
    )
    if show_scanner:
        st.divider()
        st.markdown("**Volume-profile execution**")
        vp_on = st.toggle(
            "Trade the scan picks (sim)",
            value=False,
            help=(
                "Volume-profile strategy, long & short, on the scan picks: each "
                f"pick trades its own {SESSION_LO}→{GAIN_FROM} value area "
                f"(VAH/VAL/POC + HVN/LVN), everything closed by {GAIN_TO}. "
                "Simulation only."
            ),
        )
        vp_open = st.toggle(
            "Opening position at 09:50 (open outside value)",
            value=True,
            help=(
                f"An open ABOVE the value area is an imbalance → long at the "
                f"{GAIN_FROM} open with the stop back at VAH; an open BELOW VAL "
                "→ short. An open inside value waits for a setup instead."
            ),
        )
        vp_fade = st.toggle(
            "Fade rejected value-area edges", value=True,
            help=(
                "Tag VAH and close back inside it → short back to the POC; tag "
                "VAL and close back inside → long. The edges are the profile's "
                "most-watched support/resistance."
            ),
        )
        vp_break = st.toggle(
            "Trade acceptance breakouts", value=True,
            help=(
                "Consecutive closes beyond an edge → trade with the move, stop "
                "back at that edge (acceptance = the market agreeing on a new "
                "price, vs a rejection that fades)."
            ),
        )
        vp_reentry = st.toggle(
            "Outside-open re-entry (80% rule)", value=True,
            help=(
                "Open outside value and price closes back INSIDE it → trade "
                "across the value area to the far edge."
            ),
        )
        vp_buf = st.slider(
            "Stop buffer (% of VA width)", 0.0, 50.0, float(VP_BUF_PCT), 1.0,
            help=(
                "Stops sit this far beyond the level that defined the setup, "
                "scaled to how wide the morning's value area was."
            ),
        )
        vp_confirm = st.slider(
            "Acceptance closes", 1, 5, int(VP_CONFIRM),
            help="Consecutive closes beyond an edge needed to call it acceptance.",
        )
        vp_target = st.selectbox(
            "Target", VP_TARGET_MODES, index=2,
            help=(
                "Where winning trades exit: the POC / opposite edge (value "
                "rotation), the next HVN outside value (trend continuation), "
                "or nothing at all — ride every trade to the close."
            ),
        )
        vp_tf = st.selectbox(
            "Signal timeframe", ["1m", "2m", "3m", "5m", "15m"], index=3,
            help=(
                "Bar size the setups are evaluated on (independent of the chart "
                f"timeframe). The {SESSION_LO}→{GAIN_FROM} profile itself is "
                "always built from 1m bars."
            ),
        )
        vp_capital = st.number_input(
            "Capital (₹)", min_value=100000.0, value=10_000_000.0, step=1_000_000.0,
            help="Starting equity for the simulated cycle.")
        vp_risk = st.slider("Risk per trade (% equity)", 0.5, 5.0, 2.0, 0.25,
                            help="Position size = equity × risk% ÷ entry-to-stop distance.")
        vp_cost = st.slider(
            "Cost per side (bps)", 0.0, 25.0, float(VP_COST_BPS), 0.5,
            help=(
                "Charged on BOTH entry and exit notional: brokerage + STT + "
                "exchange/GST/stamp (~3-4 bps) plus spread/slippage."
            ),
        )
        st.caption(
            f"Sizing cap: {VP_ALLOC_PCT:.0f}% × {VP_LEVERAGE:.0f}× equity notional; "
            "one position at a time per symbol; equity compounds across trades; "
            f"P&L is NET of {vp_cost:g} bps/side costs."
        )
    else:
        (vp_on, vp_open, vp_fade, vp_break, vp_reentry, vp_buf, vp_confirm,
         vp_target, vp_tf, vp_capital, vp_risk, vp_cost) = (
            False, True, True, True, True, float(VP_BUF_PCT), int(VP_CONFIRM),
            VP_TARGET_EOD, "5m", 10_000_000.0, 2.0, float(VP_COST_BPS),
        )
    st.divider()
    st.markdown(
        f"**Window** — rank: `{GAIN_FROM}` → `{GAIN_TO}`\n\n"
        f"**Session** — candles: `{SESSION_LO}` → `{SESSION_HI}`\n\n"
        f"**Universe** — {len(day_universe(d))} symbols on {d}"
    )
    st.caption("Cache: 10 min · Source: local parquet lake via DuckDB")

if d not in days:
    st.warning(
        f"No lake data for {d} — pick a traded session between {days[-1]} and {days[0]}."
    )
    st.stop()


gainers = window_gainers(d)
top = gainers.head(int(top_n)).reset_index(drop=True)

# ------------------------------------------------------------------ table
# Scanner mode replaces the by-outcome top-gainers view (exclusive).
if not show_scanner:
    left, right = st.columns([5, 3])
    with left:
        st.subheader(f"Top {len(top)} — {d}")
        st.dataframe(
            top.assign(
                px_from=top.px_from.map("{:.2f}".format),
                px_to=top.px_to.map("{:.2f}".format),
                window_gain=top.window_gain_pct.map("{:+.2f}%".format),
                intraday=top.intraday_pct.map("{:+.2f}%".format),
                volume=top.volume.map("{:,.0f}".format),
            )[["symbol", "px_from", "px_to", "window_gain", "intraday", "volume"]],
            width="stretch",
            hide_index=True,
            height=min(320, 38 * len(top) + 40),
        )

    with right:
        st.subheader("Distribution")
        fig = go.Figure(
            go.Histogram(
                x=gainers.window_gain_pct,
                nbinsx=40,
                marker_color="#4c8bf5",
            )
        )
        fig.update_layout(
            xaxis_title="09:50→15:15 gain %",
            yaxis_title="symbols",
            height=320,
            margin=dict(l=10, r=10, t=20, b=10),
        )
        st.plotly_chart(fig, width="stretch")

# ------------------------------------------------------------------ scanner
# Pre-window picks: everything below ranks with 09:45 knowledge only. The
# "actual" column is look-ahead, joined for evaluating the scan — never for
# building it.
scan = scanner_0945(d) if show_scanner else pd.DataFrame()
scan_top5: list[str] = []
if not scan.empty:
    scan = scan.merge(
        gainers[["symbol", "window_gain_pct"]], on="symbol", how="left"
    )
    score_col = {
        "Movers": "score_movers",
        "Grinders": "score_grinders",
        "Fade": "score_fade",
    }[str(scan_mode).split(" ")[0]]
    n_raw = len(scan)
    scan = scan[
        (scan.px_0945 >= float(scan_min_px))
        & (scan.vol_ratio.fillna(0) >= float(scan_min_vx))
        & (scan.off_high_pct <= float(scan_max_dip))
    ]
    scan = scan.sort_values(score_col, ascending=False).reset_index(drop=True)
    scan_top5 = scan.head(int(scan_k)).symbol.tolist()
    picks = scan.head(int(scan_k))
    top50 = set(gainers.head(50).symbol)
    hit_rate = picks.symbol.isin(top50).mean() * 100
    hit2 = (picks.fwd_945_pct >= 2).mean() * 100
    mean_fwd = picks.fwd_945_pct.mean()
    st.divider()
    # Headline shows the PICKS themselves (the actionable output), not the
    # universe/filter counts — those move to the caption as evaluation stats.
    _pick_line = " · ".join(
        f"{r.symbol} {r.fwd_945_pct:+.2f}%" for r in picks.itertuples()
    ) or "none"
    st.subheader(
        f"09:45 scanner — top {len(picks)} picks ({scan_mode.split(' ')[0]}): "
        f"{_pick_line}"
    )
    st.caption(
        f"{scan_mode}. Ranked from 09:45 information only · "
        f"{len(scan)}/{n_raw} symbols pass the filters · "
        f"picks' fwd 09:45→15:15 {mean_fwd:+.2f}% · ≥+2%: {hit2:.0f}% · "
        f"in window top-50: {hit_rate:.0f}% · universe base rate ≈ 9% of "
        "symbol-days. `fwd_945`/`win_950_15` are look-ahead columns for "
        "evaluation only. Full-lake look-ahead-free study: no pre-09:45 "
        "feature predicts direction (all |IC| < 0.05); elevated ≥+2% hit "
        "rates from range/volume rankings are volatility selection — picks "
        "hit −2% just as often. Filters and k do not create edge; use them "
        "for tradability only."
    )
    # Only the picks are listed — the ranking is the picks, not the universe.
    _show = picks
    st.dataframe(
        _show.assign(
            gap=_show.gap_pct.map("{:+.2f}%".format),
            drive=_show.or_move_pct.map("{:+.2f}%".format),
            off_high=_show.off_high_pct.map("{:.2f}%".format),
            off_low=_show.off_low_pct.map("{:.2f}%".format),
            vol_x=_show.vol_ratio.map("{:.2f}×".format),
            score=_show[score_col].map("{:.2f}".format),
            fwd_945=_show.fwd_945_pct.map("{:+.2f}%".format),
            win_950_15=_show.window_gain_pct.map(
                lambda v: f"{v:+.2f}%" if pd.notna(v) else "—"
            ),
        )[
            ["symbol", "gap", "drive", "off_high", "off_low", "vol_x", "score", "fwd_945", "win_950_15"]
        ],
        width="stretch",
        hide_index=True,
        height=min(460, 38 * len(_show) + 40),
    )

# ----------------------------------------------- volume-profile execution sim
# Runs on the scan picks with one shared-capital account. Everything below is
# a backtest-style simulation on lake bars — no broker interaction.
vp_trades = pd.DataFrame()
vp_fills = pd.DataFrame()
vp_equity = pd.DataFrame()
vp_profiles: dict[str, dict] = {}
if show_scanner and vp_on and scan_top5:
    vp_trades, vp_fills, vp_equity, vp_profiles = _simulate_vp(
        scan_top5, d, str(vp_tf), float(vp_buf), int(vp_confirm),
        str(vp_target), float(vp_capital), float(vp_risk),
        allow_fade=bool(vp_fade), allow_break=bool(vp_break),
        allow_reentry=bool(vp_reentry), open_entry=bool(vp_open),
        cost_bps=float(vp_cost),
    )
    if not vp_trades.empty:
        wins = vp_trades[vp_trades.pnl > 0]
        losses = vp_trades[vp_trades.pnl <= 0]
        gross_w = wins.pnl.sum()
        gross_l = -losses.pnl.sum()
        pf = gross_w / gross_l if gross_l > 0 else float("inf")
        m0, m1 = vp_equity.equity.iloc[0], vp_equity.equity.iloc[-1]
        st.divider()
        st.subheader(
            f"Volume-profile trades — {len(vp_trades)} round-trips · "
            f"equity ₹{m0:,.0f} → ₹{m1:,.0f} ({100 * (m1 / m0 - 1):+.2f}%)"
        )
        _enabled = [
            _n for _n, _on in (
                (f"opening day-type entry at {GAIN_FROM}", vp_open),
                ("value-area edge fades", vp_fade),
                ("acceptance breakouts", vp_break),
                ("outside-open re-entry (80% rule)", vp_reentry),
            ) if _on
        ]
        st.caption(
            f"Long & short, on {vp_tf} bars, against each pick's OWN "
            f"{SESSION_LO}→{GAIN_FROM} volume profile. Setups enabled: "
            f"{'; '.join(_enabled) if _enabled else 'none'}. Stops sit "
            f"{vp_buf:g}% of the value-area width beyond the level that made the "
            f"setup; target: {vp_target}; acceptance needs {vp_confirm} close(s) "
            f"beyond an edge; everything is flat by {GAIN_TO}. Sizing: {vp_risk:g}% "
            f"equity risk at the stop, capped at {VP_ALLOC_PCT:.0f}% × "
            f"{VP_LEVERAGE:.0f}× total notional on ONE shared "
            f"₹{float(vp_capital):,.0f} account, equity compounding across trades. "
            "SIMULATION on lake bars — fills at the signal bar's close (the "
            f"{GAIN_FROM} opening entry at that candle's open), the stop is "
            "checked before the target when one bar spans both, and costs are "
            "charged on both sides."
        )
        st.caption(
            "⚠️ Rule attribution, 15 sessions × k=3 (5m, ₹1cr, 4.75×, net of "
            "5 bps/side — poc/research/vp_strategy_backtest.py): the 09:50 opening "
            "day-type entry ALONE was +14.1% on just 12 trades, while the "
            "intraday rules drag: edge fades −11.1%, acceptance breakouts "
            "−62.9%, re-entry −4.0%, and all rules together with no target "
            "+2.5%. Targets hurt outright — the POC/HVN modes were −8%/−73% vs "
            "+2.5% with none. Treat the toggles as an attribution lab: the "
            "opening regime (open outside the morning's value area) is the one "
            "piece with real evidence behind it, and the sample is small."
        )
        k1, k2, k3, k4, k5, k6 = st.columns(6)
        _turnover = float((vp_trades.entry * vp_trades.qty).sum())
        _costs = float(vp_trades.cost.sum())
        _gross = float(vp_trades.pnl.sum()) + _costs
        k1.metric("Win rate (net)", f"{len(wins) / len(vp_trades):.0%}")
        k2.metric("Profit factor (net)", f"{pf:.2f}" if pf != float("inf") else "∞")
        k3.metric(
            "Net P&L", f"₹{vp_trades.pnl.sum():+,.0f}",
            f"gross {_gross:+,.0f} − cost {_costs:,.0f}", delta_color="off",
        )
        k4.metric(
            "Long / Short",
            f"{(vp_trades.side == 'LONG').sum()} / {(vp_trades.side == 'SHORT').sum()}",
        )
        k5.metric(
            "Turnover", f"₹{_turnover / 1e7:,.1f} cr",
            f"{_turnover / max(float(vp_capital), 1e-9):.0f}× capital",
            delta_color="off",
        )
        k6.metric(
            "Exits stop / target / EOD",
            f"{(vp_trades.exit_reason == 'stop').sum()} / "
            f"{(vp_trades.exit_reason == 'target').sum()} / "
            f"{(vp_trades.exit_reason == 'EOD 15:15').sum()}",
        )
        # Which setup actually pays — the read that matters for tuning.
        _by_setup = vp_trades.groupby("setup").agg(
            trades=("pnl", "size"),
            win_pct=("pnl", lambda s: 100.0 * (s > 0).mean()),
            net_pnl=("pnl", "sum"),
        ).reset_index()
        _bc1, _bc2 = st.columns([5, 3])
        with _bc1:
            st.dataframe(
                vp_trades.assign(
                    entry=vp_trades.entry.map("{:.2f}".format),
                    stop=vp_trades.stop.map("{:.2f}".format),
                    target=vp_trades.target.map(
                        lambda v: "—" if v is None or not np.isfinite(v)
                        else f"{v:,.2f}"
                    ),
                    exit=vp_trades.exit.map("{:.2f}".format),
                    qty=vp_trades.qty.map("{:,.0f}".format),
                    pnl=vp_trades.pnl.map("{:+,.0f}".format),
                    cost=vp_trades.cost.map("{:,.0f}".format),
                    ret_pct=vp_trades.ret_pct.map("{:+.3f}%".format),
                    equity_after=vp_trades.equity_after.map("₹{:,.0f}".format),
                    entry_time=vp_trades.entry_time.dt.strftime("%H:%M"),
                    exit_time=vp_trades.exit_time.dt.strftime("%H:%M"),
                )[
                    ["symbol", "setup", "side", "entry_time", "entry", "stop",
                     "target", "exit_time", "exit", "qty", "pnl", "cost",
                     "ret_pct", "equity_after", "exit_reason"]
                ],
                width="stretch",
                hide_index=True,
                height=min(460, 38 * len(vp_trades) + 40),
            )
        with _bc2:
            st.markdown("**By setup**")
            st.dataframe(
                _by_setup.assign(
                    win_pct=_by_setup.win_pct.map("{:.0f}%".format),
                    net_pnl=_by_setup.net_pnl.map("{:+,.0f}".format),
                ),
                width="stretch",
                hide_index=True,
            )
        # Equity cycle across the session's trades.
        if len(vp_equity) > 1:
            eqf = go.Figure(
                go.Scatter(
                    x=vp_equity.timestamp, y=vp_equity.equity,
                    mode="lines+markers", line=dict(color="#9467bd", width=2),
                    name="Equity",
                )
            )
            eqf.update_layout(
                title="Account equity cycle (realized, shared account)",
                yaxis_title="₹", height=280,
                margin=dict(l=10, r=10, t=40, b=10),
            )
            # d3-format has no ₹ symbol: prefix separately ("₹,.0f" warns).
            eqf.update_yaxes(tickprefix="₹", tickformat=",.0f")
            st.plotly_chart(eqf, width="stretch")
    else:
        st.info(
            f"The volume-profile sim produced no round-trips on the "
            f"{len(scan_top5)} scan picks for {d} (no setup triggered, or no data)."
        )

    # ── Per-stock profile: every pick's value-area location + its own
    #    09:15→09:50 volume profile, so the whole basket reads at a glance.
    pv, profs = _per_stock_view(scan_top5, d, str(vp_tf), vp_profiles, vp_trades)
    if not pv.empty:
        st.divider()
        st.subheader(f"Per stock — {GAIN_FROM} location vs value, profile, trades")
        st.caption(
            f"One row per scan pick. `regime` / `va_pos` = where the {GAIN_FROM} "
            "open sat relative to that symbol's own value area; an open OUTSIDE "
            "value takes the day-type side (long above VAH, short below VAL) "
            f"with the stop back at the edge. POC/VAH/VAL are the "
            f"{SESSION_LO}→{GAIN_FROM} profile built from 1m bars strictly "
            "before the entry candle — the same levels the sim traded against."
        )
        st.dataframe(
            pv.assign(
                open_0950=pv.open_0950.map("{:,.2f}".format),
                stop=pv.stop.map(lambda v: f"{v:,.2f}" if np.isfinite(v) else "—"),
                risk_pct=pv.risk_pct.map(
                    lambda v: f"{v:.2f}%" if np.isfinite(v) else "—"
                ),
                poc=pv.poc.map("{:,.2f}".format),
                vah=pv.vah.map("{:,.2f}".format),
                val=pv.val.map("{:,.2f}".format),
                win_pct=pv.win_pct.map(
                    lambda v: f"{v:.0f}%" if np.isfinite(v) else "—"
                ),
                pnl=pv.pnl.map("{:+,.0f}".format),
            )[
                ["symbol", "regime", "va_pos", "open_0950", "side", "stop",
                 "risk_pct", "poc", "vah", "val", "hvn", "lvn", "trades",
                 "setups", "win_pct", "pnl", "exits_stop_target_eod"]
            ],
            width="stretch",
            hide_index=True,
            height=min(320, 38 * len(pv) + 40),
        )
        _pcols = st.columns(min(3, len(profs)))
        for _n, (_sym, _pr) in enumerate(profs.items()):
            with _pcols[_n % len(_pcols)]:
                st.plotly_chart(_mini_profile_fig(_sym, _pr), key=f"prof_{_sym}")

# ------------------------------------------------------------------ charts
st.divider()
if pick_custom:
    pick = st.selectbox(
        "Symbol (custom)",
        all_symbols(),
        help="Any symbol in the lake; charts show its session on the chosen day.",
    )
else:
    # Scanner mode is exclusive: chart options are the scan picks only.
    # Without the scanner (or if the scan came back empty), fall back to the
    # by-outcome top-N list.
    if show_scanner and scan_top5:
        chart_opts = scan_top5
    else:
        chart_opts = top.symbol.tolist()
    scan_rank = {s: i + 1 for i, s in enumerate(scan_top5)}
    later = dict(zip(gainers.symbol, gainers.window_gain_pct))

    def _chart_label(s: str) -> str:
        if s in scan_rank:
            actual = later.get(s)
            tail = f" · later {actual:+.2f}%" if actual is not None else ""
            return f"{s}  (scan #{scan_rank[s]}{tail})"
        actual = later.get(s)
        return f"{s}  ({actual:+.2f}%)" if actual is not None else s

    pick = st.radio(
        "Chart",
        chart_opts,
        horizontal=True,
        format_func=_chart_label,
    )

df = bars(pick, d, tf)
if df.empty:
    st.warning(f"No session bars for {pick} on {d}.")
    st.stop()

# ── Execution view for the SELECTED stock: its transactions table first, then
#    the chart below with the signals (entries/exits), the trailing stop and
#    each trade's P&L drawn on it.
sym_trades = (
    vp_trades[vp_trades.symbol == pick].reset_index(drop=True)
    if (show_scanner and vp_on and not vp_trades.empty) else pd.DataFrame()
)
if not sym_trades.empty:
    st.subheader(f"Trades — {pick}")
    _sym_wins = sym_trades[sym_trades.pnl > 0]
    _sym_pnl = float(sym_trades.pnl.sum())
    _c1, _c2, _c3, _c4, _c5 = st.columns(5)
    _c1.metric("Round-trips", f"{len(sym_trades)}")
    _c2.metric("Win rate", f"{len(_sym_wins) / len(sym_trades):.0%}")
    _c3.metric(
        "P&L (this stock)", f"₹{_sym_pnl:+,.0f}",
        f"{100.0 * _sym_pnl / max(float(vp_capital), 1e-9):+.2f}% of capital",
    )
    _c4.metric(
        "Long / Short",
        f"{(sym_trades.side == 'LONG').sum()} / {(sym_trades.side == 'SHORT').sum()}",
    )
    _c5.metric(
        "Exits stop / target / EOD",
        f"{(sym_trades.exit_reason == 'stop').sum()} / "
        f"{(sym_trades.exit_reason == 'target').sum()} / "
        f"{(sym_trades.exit_reason == 'EOD 15:15').sum()}",
    )
    st.dataframe(
        sym_trades.assign(
            entry=sym_trades.entry.map("{:.2f}".format),
            stop=sym_trades.stop.map("{:.2f}".format),
            target=sym_trades.target.map(
                lambda v: "—" if v is None or not np.isfinite(v) else f"{v:,.2f}"
            ),
            exit=sym_trades.exit.map("{:.2f}".format),
            qty=sym_trades.qty.map("{:,.0f}".format),
            pnl=sym_trades.pnl.map("{:+,.0f}".format),
            cost=sym_trades.cost.map("{:,.0f}".format),
            ret_pct=sym_trades.ret_pct.map("{:+.3f}%".format),
            equity_after=sym_trades.equity_after.map("₹{:,.0f}".format),
            entry_time=sym_trades.entry_time.dt.strftime("%H:%M"),
            exit_time=sym_trades.exit_time.dt.strftime("%H:%M"),
        )[
            ["setup", "side", "entry_time", "entry", "stop", "target",
             "exit_time", "exit", "qty", "pnl", "cost", "ret_pct",
             "equity_after", "exit_reason"]
        ],
        width="stretch",
        hide_index=True,
        height=min(400, 38 * len(sym_trades) + 40),
    )
    st.caption(
        "Chart below carries the same signals against the value area it traded: "
        "▲ long entry, ▼ short entry, × exit (labelled with that trade's P&L), "
        "and the dotted/solid guides are VAH / POC / VAL of the "
        f"{SESSION_LO}→{GAIN_FROM} profile. All positions flat by {GAIN_TO}."
    )

# ------------------------------------------- context data + volume profile
# Prior sessions + current day stitched into one frame; it feeds BOTH the
# context chart and the volume profile drawn on the main chart. `days` is
# descending — take the N most-recent prior days, sort ascending, current
# day last.
hist_days = sorted([x for x in days if x < d][:ctx_days])
hist_days.append(d)
hist_df = pd.concat(
    [bars(pick, p, tf).assign(day=p) for p in hist_days],
    ignore_index=True,
)
if not hist_df.empty:
    hist_df = hist_df.sort_values(["day", "timestamp"], kind="stable").reset_index(drop=True)
    # Each session sits on a 0-based bar position so days are stitched end to
    # end — no dead gap between one day's 15:29 close and the next's 09:15 open.
    day_starts: dict[date, int] = {}
    pos = 0
    for p in hist_days:  # ascending: oldest -> newest, matching sorted order
        day_starts[p] = pos
        pos += int((hist_df.day == p).sum())
    hist_df["x"] = range(len(hist_df))  # continuous session index
    ts_labels = hist_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M")
    # Profile input: context days (full sessions) + current day up to the
    # window start (09:50) — the pre-window distribution the ranking builds on.
    vp_mask = (hist_df["day"] < d) | (
        (hist_df["day"] == d)
        & (hist_df["timestamp"].dt.time <= pd.Timestamp(GAIN_FROM).time())
    )
    vp = _volume_profile(hist_df[vp_mask])
else:
    day_starts, ts_labels, vp = {}, None, None


fig = make_subplots(
    rows=2,
    cols=1,
    shared_xaxes=True,
    row_heights=[0.75, 0.25],
    vertical_spacing=0.02,
)

fig.add_trace(
    go.Candlestick(
        x=df.timestamp,
        open=df.open,
        high=df.high,
        low=df.low,
        close=df.close,
        name=pick,
        increasing_line_color="#2ca02c",
        decreasing_line_color="#d62728",
    ),
    row=1,
    col=1,
)
fig.add_trace(
    go.Bar(
        x=df.timestamp,
        y=df.volume,
        marker_color="#9ecae1",
        name="Volume",
        opacity=0.85,
    ),
    row=2,
    col=1,
)

# Mark the ranking-window endpoints on the candle panel.
w_lo = df.timestamp.iloc[0].replace(
    hour=int(GAIN_FROM.split(":")[0]), minute=int(GAIN_FROM.split(":")[1])
)
w_hi = df.timestamp.iloc[0].replace(
    hour=int(GAIN_TO.split(":")[0]), minute=int(GAIN_TO.split(":")[1])
)
if (top.symbol == pick).any():
    row = top.loc[top.symbol == pick].iloc[0]
else:
    # Custom pick outside the ranked table — derive the window stats from bars.
    _t = df.timestamp.dt.time
    _pre = df[_t <= pd.Timestamp(GAIN_FROM).time()]
    _pre_to = df[_t <= pd.Timestamp(GAIN_TO).time()]
    _pf = float(_pre["close"].iloc[-1]) if not _pre.empty else float("nan")
    _pt = float(_pre_to["close"].iloc[-1]) if not _pre_to.empty else float("nan")
    _o = float(df["open"].iloc[0])
    row = pd.Series(
        {
            "symbol": pick,
            "px_from": _pf,
            "px_to": _pt,
            "window_gain_pct": (_pt / _pf - 1) * 100 if _pf == _pf and _pf else float("nan"),
            "intraday_pct": (
                (float(df["close"].iloc[-1]) / _o - 1) * 100 if _o else float("nan")
            ),
            "volume": float(df["volume"].sum()),
        }
    )
for ts, label, color in (
    (w_lo, GAIN_FROM, "#1f77b4"),
    (w_hi, GAIN_TO, "#ff7f0e"),
):
    fig.add_vline(
        x=ts,
        line=dict(color=color, width=1, dash="dot"),
        annotation_text=f" {label}",
        annotation_position="top left",
        row=1,
    )
# Horizontal guides at the window prices used for the ranking.
fig.add_hline(y=row.px_from, line=dict(color="#1f77b4", width=1, dash="dot"), row=1, col=1)
fig.add_hline(y=row.px_to, line=dict(color="#ff7f0e", width=1, dash="dot"), row=1, col=1)

# Volume-profile overlay for the selected pick: the value area the strategy
# traded against, drawn as level guides above the session chart.
_vp_info = vp_profiles.get(pick) if (show_scanner and vp_on) else None
if _vp_info:
    _lv = _vp_info["lv"]
    for _y, _lab, _col, _dash in (
        (_lv["vah"], f"VAH {_lv['vah']:,.2f}", "#8caaff", "dot"),
        (_lv["poc"], f"POC {_lv['poc']:,.2f}", "#f2c744", "solid"),
        (_lv["val"], f"VAL {_lv['val']:,.2f}", "#8caaff", "dot"),
    ):
        fig.add_hline(
            y=_y, line=dict(color=_col, width=1.5, dash=_dash),
            annotation_text=f" {_lab}", annotation_position="right",
            annotation_font_size=9, row=1, col=1,
        )
# Two traces so entries label above the candle and exits (with their P&L)
# label below it, instead of stacking all text on one side.
_mk = (
    vp_fills[vp_fills.symbol == pick]
    if (not vp_fills.empty and "symbol" in vp_fills) else pd.DataFrame()
)
if not _mk.empty:
    _ent = _mk[_mk.kind != "exit"]
    _ext = _mk[_mk.kind == "exit"]
    if not _ent.empty:
        fig.add_trace(
            go.Scatter(
                x=_ent.timestamp, y=_ent.price, mode="markers+text",
                marker=dict(
                    size=12,
                    color=np.where(_ent.side == "LONG", "#2ca02c", "#d62728"),
                    symbol=np.where(_ent.side == "LONG", "triangle-up", "triangle-down"),
                    line=dict(width=1, color="white"),
                ),
                # Compact per-fill label: side initial + setup code + fill time.
                text=[
                    f"{s[0]}{VP_SETUP_CODE.get(k, '')} {t:%H:%M}"
                    for s, k, t in zip(_ent.side, _ent.setup, _ent.timestamp)
                ],
                textposition="top center",
                textfont=dict(size=9),
                name="Entries",
                hovertemplate="%{text} @ %{y:.2f}<br>%{x|%H:%M}<extra></extra>",
            ), row=1, col=1,
        )
    if not _ext.empty:
        # Each exit marker carries the round-trip's realised P&L.
        _pnl_txt: list[str] = []
        for _, _fl in _ext.iterrows():
            _hit = sym_trades[sym_trades.exit_time == _fl["timestamp"]]
            _pnl_txt.append(f"× {_hit.pnl.iloc[0]:+,.0f}" if len(_hit) else "× exit")
        fig.add_trace(
            go.Scatter(
                x=_ext.timestamp, y=_ext.price, mode="markers+text",
                marker=dict(
                    size=12, color="#6b7280", symbol="x",
                    line=dict(width=1, color="white"),
                ),
                text=_pnl_txt,
                textposition="bottom center",
                textfont=dict(size=9),
                name="Exits (P&L)",
                hovertemplate="%{text} · %{x|%H:%M} @ %{y:.2f}<extra></extra>",
            ), row=1, col=1,
        )

# ── TradingView-style volume profile overlay (shared with the context chart).
if vp is not None and not vp["bins"].empty:
    _add_vp_overlay(fig, vp)

    # ── Opening-range profile on the LEFT edge: current-day bars from session
    #    open to 09:45, bars-only (no node guides). Distinct orange so it reads
    #    as a separate scale from the blue context profile on the right.
    open_cut = pd.Timestamp(OPEN_PROFILE_TO).time()
    vp_open = _volume_profile(
        hist_df[(hist_df["day"] == d) & (hist_df["timestamp"].dt.time <= open_cut)]
    )
    if vp_open is not None and not vp_open["bins"].empty:
        _add_vp_overlay(
            fig,
            vp_open,
            side="left",
            axis="x4",
            draw_nodes=False,
            name=f"Open {SESSION_LO}→{OPEN_PROFILE_TO}",
        )

# ── Developing POC: how the profile's fairest price migrated bar by bar.
if dev_poc and vp is not None:
    dev = _developing_poc(hist_df, d)
    if dev is not None and len(dev) > 1:
        fig.add_trace(
            go.Scatter(
                x=dev.timestamp,
                y=dev.poc,
                mode="lines",
                line=dict(color="#b366ff", width=2, shape="hv"),
                name="Developing POC",
                customdata=dev.timestamp.dt.strftime("%H:%M"),
                hovertemplate="%{customdata} · developing POC %{y:,.2f}<extra></extra>",
            ),
            row=1,
            col=1,
        )

# Rangebreak: drop non-trading minutes so the axis hugs the session.
# Targeted per subplot — a bare update_xaxes() would also hit the profile
# overlay axis (x3, linear), where session hour-breaks make no sense.
for _r, _c in ((1, 1), (2, 1)):
    fig.update_xaxes(
        rangebreaks=[dict(bounds=["16:00", "09:00"], pattern="hour")],
        row=_r,
        col=_c,
    )
fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
fig.update_yaxes(title_text="Price", row=1, col=1)
fig.update_yaxes(title_text="Volume", row=2, col=1)

fig.update_layout(
    height=640,
    title=(
        f"{pick} — {d} ({tf} candles, full session) · "
        f"profile R: context + today →{GAIN_FROM} · L: open →{OPEN_PROFILE_TO} "
        f"(POC/VAH/VAL · HVN/LVN)" + (" · developing POC" if dev_poc else "")
    ),
    xaxis2_rangeslider_visible=False,
    legend=dict(orientation="h", y=1.06),
    margin=dict(l=10, r=10, t=60, b=10),
)
if show_candles:
    st.plotly_chart(fig, width="stretch")

# Window summary strip under the chart.
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric(f"Close@{GAIN_FROM}", f"{row.px_from:,.2f}")
c2.metric(f"Close@{GAIN_TO}", f"{row.px_to:,.2f}")
c3.metric("Window gain", f"{row.window_gain_pct:+.2f}%")
c4.metric("Day O→C", f"{row.intraday_pct:+.2f}%")
c5.metric("Volume", f"{row.volume:,.0f}")

# ------------------------------------------------------- history + current day
# Continuous-axis context chart: prior sessions + current day stitched end to
# end (hist_df and the volume profile were computed above the main chart).
hist_days = sorted([x for x in days if x < d][:ctx_days])
hist_days.append(d)  # current day last (newest on the right)
if hist_days and show_context:
    st.divider()
    st.subheader(
        f"Path (context — {len(hist_days) - 1} prior + current, {tf}) "
        f"· vol profile to {GAIN_FROM}"
    )
    # Sessions were stitched and the volume profile computed above the main
    # chart (both figures share that data).
    if not hist_df.empty:

        fig2 = make_subplots(
            rows=2,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.03,
            row_heights=[0.74, 0.26],
        )

        # One combined candlestick trace spanning every session.
        fig2.add_trace(
            go.Candlestick(
                x=hist_df["x"],
                open=hist_df["open"],
                high=hist_df["high"],
                low=hist_df["low"],
                close=hist_df["close"],
                name=pick,
                customdata=ts_labels,
                hovertemplate=(
                    "Time: %{customdata}<br>"
                    "O: %{open}<br>H: %{high}<br>L: %{low}<br>C: %{close}<extra></extra>"
                ),
                increasing_line_color="#2ca02c",
                decreasing_line_color="#d62728",
                whiskerwidth=0.25,
            ),
            row=1,
            col=1,
        )
        fig2.add_trace(
            go.Bar(
                x=hist_df["x"],
                y=hist_df["volume"],
                customdata=ts_labels,
                hovertemplate="Time: %{customdata}<br>Volume: %{y:,.0f}<extra></extra>",
                marker_color="#9ecae1",
                name="Volume",
                opacity=0.85,
            ),
            row=2,
            col=1,
        )

        # Same TradingView-style volume profile (context days + today →09:50)
        # as on the main chart — bars hug the right edge, POC/VAH/VAL cross.
        if vp is not None and not vp["bins"].empty:
            _add_vp_overlay(fig2, vp)

        # Day-boundary separators: faint dotted lines; the current day gets a
        # brighter solid marker so the eye finds today on the continuous axis.
        for p in hist_days:
            fig2.add_vline(
                x=day_starts[p],
                line=dict(
                    color="#1f77b4" if p == d else "rgba(128,128,128,0.35)",
                    width=1.5 if p == d else 1,
                    dash="solid" if p == d else "dot",
                ),
                row=1,
                col=1,
            )

        # X ticks: one per session start (day + month abbreviation).
        xticks = []
        for p in hist_days:
            hd = hist_df[hist_df.day == p]
            if not hd.empty:
                xticks.append((int(hd.x.iloc[0]), f"{p.day:02d} {p.strftime('%b')}"))
        fig2.update_xaxes(
            tickvals=[t[0] for t in xticks],
            ticktext=[t[1] for t in xticks],
            tickangle=-45,
            row=2,
            col=1,
        )
        fig2.update_yaxes(title_text="Price", row=1, col=1)
        fig2.update_yaxes(title_text="Volume", row=2, col=1)
        fig2.update_xaxes(rangeslider_visible=False, row=1, col=1)
        fig2.update_layout(
            height=520,
            title=(
                f"{pick} — candles, {len(hist_days) - 1} prior + current session ({tf}) "
                f"· vol profile: context days + today →{GAIN_FROM} (POC/VAH/VAL · HVN/LVN)"
            ),
            margin=dict(l=10, r=10, t=50, b=30),
            showlegend=False,
            hovermode="x unified",
        )
        if show_context:
            st.plotly_chart(fig2, width="stretch")
    else:
        st.info(f"No bars available for {pick} across the {len(hist_days) - 1} prior + current sessions.")
