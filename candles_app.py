"""Simple Streamlit candlestick viewer over the OHLCV datalake.

Date -> Scanner (09:45) -> Stocks dropdown (screener / top-gainers / direct) -> Candle plot.

    .venv/bin/streamlit run candles_app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent / "services" / "duckdb-analytics" / "src"))

from duck_analytics.catalog import DuckDBCatalog, default_config_for  # noqa: E402
from duck_analytics.query import QueryService  # noqa: E402
from duck_analytics.scanners import scan_screener  # noqa: E402

REPO = Path(__file__).resolve().parent

st.set_page_config(page_title="TradeX candles", layout="wide")
st.title("TradeX — Candlestick Viewer")

cfg = default_config_for(REPO)
cat = DuckDBCatalog(cfg)
svc = QueryService(cat, cfg)


@st.cache_data(ttl=600)
def get_screener(date_str: str):
    q = scan_screener(f"{date_str} 09:45:00")
    res = svc.execute(q.sql, point_in_time_safe=True)
    return res.rows, res.columns


@st.cache_data
def get_top_gainers(date_str: str, limit: int = 20):
    """Top gainers 09:45 -> 15:15 for a date (no screener blocks)."""
    res = svc.execute(
        f"""
        WITH day AS (
            SELECT symbol,
                first(open ORDER BY timestamp) FILTER (
                    WHERE CAST(timestamp AS TIME) >= TIME '09:45') AS o_win,
                last(close ORDER BY timestamp) FILTER (
                    WHERE CAST(timestamp AS TIME) <= TIME '15:15') AS c_win
            FROM ohlcv
            WHERE ts::DATE = DATE '{date_str}'
              AND CAST(timestamp AS TIME) >= TIME '09:45'
              AND CAST(timestamp AS TIME) <= TIME '15:15'
            GROUP BY symbol
        )
        SELECT symbol, round(o_win,2) AS o_0945, round(c_win,2) AS c_1515,
               round((c_win/o_win-1)*100,2) AS gain_pct
        FROM day WHERE o_win > 0 AND c_win IS NOT NULL
        ORDER BY gain_pct DESC LIMIT {int(limit)}
        """,
        limit=50,
    )
    return res.rows, res.columns


@st.cache_data
def list_all_symbols() -> list[str]:
    return cat.list_symbols()


@st.cache_data
def load_day(symbol: str, date_str: str, timeframe: str) -> pd.DataFrame:
    rule = {
        "1m": None, "5m": "5min", "15m": "15min",
        "30m": "30min", "1h": "1h",
    }[timeframe]
    res = svc.execute(
        f"SELECT ts, open, high, low, close, volume FROM ohlcv "
        f"WHERE symbol = '{symbol.replace(chr(39), chr(39)+chr(39))}' "
        f"AND ts::DATE = DATE '{date_str}'",
        limit=50_000,
    )
    df = pd.DataFrame(res.rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    if df.empty:
        return df
    df = df.sort_values("timestamp").reset_index(drop=True)
    if rule:
        df = (
            df.set_index("timestamp")
            .resample(rule)
            .agg({"open": "first", "high": "max", "low": "min",
                  "close": "last", "volume": "sum"})
            .dropna(subset=["open"])
            .reset_index()
        )
    return df


# ---- sidebar ----
with st.sidebar:
    st.subheader("1) Select date")
    date = st.date_input("Date", value=pd.Timestamp("2026-08-21").date())

    st.subheader("2) Timeframe")
    timeframe = st.selectbox("Timeframe", ["1m", "5m", "15m", "30m", "1h"], index=0)

    st.divider()
    st.caption("Screener blocks (09:45 decision):")
    st.table({
        "Block": [
            "Overnight gap", "First-30m rel-volume", "Opening drive",
            "RS score", "ADX(14)", "ATR(14)%", "Exhaustion guard", "Penny filter",
        ],
        "Threshold": [
            "≥ 0.5%", "≥ 3× prior-14d avg", "≥ +0.3% (09:15→09:45)",
            "> cross-symbol median (20d @ 15m)", "≥ 20 and rising",
            "1.2% ≤ ATR ≤ —", "prev-day first-30m ≤ 3%", "open ≥ ₹100",
        ],
    })

    st.divider()
    st.subheader("3) Pick stocks")
    mode = st.radio(
        "Source",
        ["Screener (7 blocks)", "Top gainers 09:45→15:15", "Direct (all symbols)"],
        index=0,
    )

    selected: str | None = None
    screener_rows: list = []
    screener_cols: list = []

    if mode == "Screener (7 blocks)":
        if st.button("Run screener for this date", width="stretch", type="primary", key="run_screener"):
            st.session_state["screener_date"] = date.isoformat()
        screener_date = st.session_state.get("screener_date")
        if screener_date == date.isoformat():
            with st.spinner("Scanning..."):
                screener_rows, screener_cols = get_screener(date.isoformat())
                stocks = [r[0] for r in screener_rows]
            if not stocks:
                st.warning(f"No signals for {date.isoformat()}.")
            else:
                selected = st.selectbox("Stocks from screener", stocks, help=f"{len(stocks)} signals")
                st.caption(f"{len(stocks)} stocks found")
                with st.expander("Screener details", expanded=False):
                    st.dataframe(
                        pd.DataFrame(screener_rows, columns=screener_cols),
                        width="stretch", height=220,
                    )
        elif screener_date and screener_date != date.isoformat():
            st.info("Date changed — click 'Run screener' again.")
        else:
            st.caption("Click **Run screener** to populate.")
            st.selectbox("Stocks from screener", ["— run screener —"], disabled=True)

    elif mode == "Top gainers 09:45→15:15":
        top_rows, top_cols = get_top_gainers(date.isoformat(), limit=5)
        if not top_rows:
            st.warning(f"No gainers for {date.isoformat()} (no data?).")
        else:
            labels = [f"{r[0]}  ({r[3]:+.2f}%)" for r in top_rows]
            pick = st.selectbox("Top gainers", labels, help=f"Top {len(top_rows)} by 09:45→15:15")
            selected = pick.split(" ")[0] if pick else None
            with st.expander("Top gainers table", expanded=False):
                st.dataframe(
                    pd.DataFrame(top_rows, columns=top_cols),
                    width="stretch", height=280,
                )

    else:  # Direct
        all_syms = list_all_symbols()
        selected = st.selectbox("All symbols", all_syms, index=all_syms.index("RELIANCE") if "RELIANCE" in all_syms else 0)

# ---- main: candle plot ----
if not selected:
    st.info("👈 Pick a date, choose a source, and select a stock to view its candlestick.")
    st.stop()

df = load_day(selected, date.isoformat(), timeframe)

if df.empty:
    st.warning(f"No data for {selected} on {date.isoformat()}.")
    st.stop()

# 09:45 -> 15:15 window for title + metric
ts_0945 = pd.Timestamp(f"{date.isoformat()} 09:45:00")
ts_1515 = pd.Timestamp(f"{date.isoformat()} 15:15:00")
win = df[(df["timestamp"] >= ts_0945) & (df["timestamp"] <= ts_1515)]
if len(win) >= 1:
    o_win, c_win = win["open"].iloc[0], win["close"].iloc[-1]
    ret_win = (c_win / o_win - 1) * 100
    label_win = f"{ret_win:+.2f}%"
    delta_win = f"{o_win:,.2f} → {c_win:,.2f}"
else:
    ret_win, label_win, delta_win = None, "n/a", "n/a"

title_suffix = f"  |  09:45→15:15 {label_win} ({delta_win})" if label_win != "n/a" else ""
fig = make_subplots(
    rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06,
    row_heights=[0.75, 0.25],
    subplot_titles=[f"{selected} {timeframe} — {date.isoformat()}{title_suffix}", "Volume"],
)
fig.add_trace(go.Candlestick(
    x=df["timestamp"], open=df["open"], high=df["high"],
    low=df["low"], close=df["close"], name=selected,
), row=1, col=1)
fig.add_trace(go.Bar(x=df["timestamp"], y=df["volume"], name="Volume",
                     marker_color="rgba(100,100,150,0.4)"), row=2, col=1)
# --- decision lines ---
for ts_str, label, color in [
    (f"{date.isoformat()} 09:45:00", "09:45", "#1f77b4"),
    (f"{date.isoformat()} 09:50:00", "09:50", "#ff7f0e"),
]:
    ts = pd.Timestamp(ts_str)
    for r in (1, 2):
        fig.add_vline(
            x=ts, line_width=1.2, line_dash="dash", line_color=color,
            annotation_text=label if r == 1 else "",
            annotation_position="top left" if r == 1 else "top",
            row=r, col=1,
        )

fig.update_layout(
    xaxis_rangeslider_visible=False,
    height=650, margin=dict(l=40, r=20, t=60, b=40),
    hovermode="x unified",
)
st.plotly_chart(fig, width="stretch")

o, h, l, c = df["open"].iloc[0], df["high"].max(), df["low"].min(), df["close"].iloc[-1]

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Open", f"{o:,.2f}")
c2.metric("High", f"{h:,.2f}")
c3.metric("Low", f"{l:,.2f}")
c4.metric("Close", f"{c:,.2f}", f"{(c/o-1)*100:+.2f}%")
c5.metric("09:45→15:15", label_win, delta_win)
c6.metric("Bars", len(df))

with st.expander("Raw data"):
    st.dataframe(df, width="stretch", height=300)
