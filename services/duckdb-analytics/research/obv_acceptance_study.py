"""Does OBV + price-acceptance (close_pos45) add value?

Decision at 09:45. Evaluates the tuned 7-block winner plus two new blocks:
  - close_pos45 >= 0.9  (acceptance: first-30m bar closes near its high, on volume)
  - obv > sma20 & rising (daily OBV above its 20-day MA)

All features strictly point-in-time. RS is 15m bars through yesterday; ADX/ATR
are daily through yesterday; v30x uses prior 14 days.

Run: .venv/bin/python services/duckdb-analytics/research/obv_acceptance_study.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rs_dtw_topgainer_study import load_n_bars, rs_scores_per_day  # noqa

from duck_analytics.catalog import DuckDBCatalog
from duck_analytics.config import AnalyticsConfig
from duck_analytics.query import QueryService

DAILY_SQL = """
SELECT symbol, ts::DATE AS d,
    first(open ORDER BY timestamp) AS o,
    last(close ORDER BY timestamp) AS c,
    max(high) AS h, min(low) AS l,
    sum(volume) AS v_day,
    sum(CASE WHEN CAST(timestamp AS TIME) < TIME '09:45' THEN volume ELSE 0 END) AS v30,
    last(close ORDER BY timestamp) FILTER (WHERE CAST(timestamp AS TIME) <= TIME '09:45') AS px45,
    max(high) FILTER (WHERE CAST(timestamp AS TIME) <= TIME '09:45') AS hi45,
    min(low) FILTER (WHERE CAST(timestamp AS TIME) <= TIME '09:45') AS lo45,
    first(open ORDER BY timestamp) FILTER (WHERE CAST(timestamp AS TIME) >= TIME '09:45') AS o_win,
    last(close ORDER BY timestamp) FILTER (WHERE CAST(timestamp AS TIME) <= TIME '15:15') AS c_win
FROM ohlcv
WHERE year(ts::DATE)=2026 AND month(ts::DATE) BETWEEN 5 AND 8
GROUP BY symbol, d
"""


def load_daily(svc: QueryService) -> pd.DataFrame:
    res = svc.execute(DAILY_SQL, limit=50_000)
    df = pd.DataFrame(res.rows, columns=[
        "symbol","d","o","c","h","l","v_day","v30","px45","hi45","lo45","o_win","c_win"
    ])
    df["d"] = pd.to_datetime(df["d"])
    df = df.sort_values(["symbol","d"])
    # point-in-time derived
    df["gap_pct"] = (df["o"] / df.groupby("symbol")["c"].shift(1) - 1) * 100
    df["pre30_ret"] = (df["px45"] / df["o"] - 1) * 100
    df["v30x_14d"] = df["v30"] / df.groupby("symbol")["v30"].transform(
        lambda s: s.shift(1).rolling(14, min_periods=6).mean()
    )
    df["close_pos45"] = (df["px45"] - df["lo45"]) / (df["hi45"] - df["lo45"])
    df["prev_pre30_ret"] = df.groupby("symbol")["pre30_ret"].shift(1)
    # OBV daily
    df["sgn"] = np.sign(df["c"] - df.groupby("symbol")["c"].shift(1)).fillna(0)
    df["obv"] = (df["sgn"] * df["v_day"]).groupby(df["symbol"]).cumsum()
    df["sma_obv20"] = df.groupby("symbol")["obv"].transform(
        lambda s: s.rolling(20, min_periods=20).mean()
    )
    df["obv_rising"] = df["obv"] > df.groupby("symbol")["obv"].shift(5)
    df["obv_ok"] = (df["obv"] > df["sma_obv20"]) & df["obv_rising"]
    # ADX(14) Cutler (SMA of DM/TR) and ATR% daily
    df["prev_c"] = df.groupby("symbol")["c"].shift(1)
    df["prev_h"] = df.groupby("symbol")["h"].shift(1)
    df["prev_l"] = df.groupby("symbol")["l"].shift(1)
    df["tr"] = np.maximum.reduce([
        df["h"] - df["l"],
        (df["h"] - df["prev_c"]).abs(),
        (df["l"] - df["prev_c"]).abs(),
    ])
    up = df["h"] - df["prev_h"]
    dn = df["prev_l"] - df["l"]
    df["pdm_raw"] = np.where((up > dn) & (up > 0), up, 0.0)
    df["mdm_raw"] = np.where((dn > up) & (dn > 0), dn, 0.0)
    df["pdm_s"] = df.groupby("symbol")["pdm_raw"].transform(
        lambda s: s.rolling(14, min_periods=14).mean())
    df["mdm_s"] = df.groupby("symbol")["mdm_raw"].transform(
        lambda s: s.rolling(14, min_periods=14).mean())
    df["atr_s"] = df.groupby("symbol")["tr"].transform(
        lambda s: s.rolling(14, min_periods=14).mean())
    df["pdi"] = 100 * df["pdm_s"] / df["atr_s"]
    df["mdi"] = 100 * df["mdm_s"] / df["atr_s"]
    df["dx"] = 100 * (df["pdi"] - df["mdi"]).abs() / (df["pdi"] + df["mdi"])
    df["adx14"] = df.groupby("symbol")["dx"].transform(
        lambda s: s.rolling(14, min_periods=14).mean())
    df["adx_prev"] = df.groupby("symbol")["adx14"].shift(1)
    df["atr_pct"] = df["atr_s"] * 100 / df["c"]
    df["gain_target"] = (df["c_win"] / df["o_win"] - 1) * 100
    df["rk_gainer"] = df.groupby("d")["gain_target"].rank(ascending=False, method="min")
    df["is_top10"] = df["rk_gainer"] <= 10
    df["is_top20"] = df["rk_gainer"] <= 20
    # RS
    try:
        nb = load_n_bars(svc, sorted(df["symbol"].unique()))
        rs_map = rs_scores_per_day(nb, sorted(df["d"].dt.strftime("%Y-%m-%d").unique()))
        rs_map["next_d"] = pd.to_datetime(rs_map["next_d"])
        df = df.merge(rs_map[["symbol","next_d","rs_score"]],
                      left_on=["symbol","d"], right_on=["symbol","next_d"], how="left")
        df["rs_ok"] = df["rs_score"] > df.groupby("d")["rs_score"].transform("median")
    except Exception as e:
        print(f"RS load failed ({e}), rs_ok=False")
        df["rs_score"] = np.nan
        df["rs_ok"] = False
    df["month"] = df["d"].dt.strftime("%Y-%m")
    return df


def evaluate(frame: pd.DataFrame, cond) -> dict | None:
    sig = frame[cond] if callable(cond) else frame.query(cond, engine="python") if isinstance(cond, str) else frame[cond]
    n = len(sig)
    days = sig["d"].nunique() if n else 1
    per_day = n / max(frame["d"].nunique(), 1)
    # require at least 0.7/day and 10 signals overall and above baseline
    if n < 10 or per_day < 0.7:
        return None
    p10 = sig["is_top10"].mean()
    base_p10 = 10 / frame.groupby("d").size().median()
    if p10 <= base_p10:
        return None
    return {
        "per_day": round(n / frame["d"].nunique(), 1),
        "n": n,
        "p10": p10,
        "p20": sig["is_top20"].mean(),
        "med": sig["gain_target"].median(),
        "mean": sig["gain_target"].mean(),
        "win": (sig["gain_target"] > 0).mean(),
    }


def main() -> None:
    cfg = AnalyticsConfig(base_path=Path("data/ohlcv"))
    cat = DuckDBCatalog(cfg)
    svc = QueryService(cat, cfg)
    df = load_daily(svc)
    cat.close()
    print(f"feature matrix: {len(df)} rows, RS coverage {df['rs_score'].notna().mean():.0%}")

    train = df[df["month"] != "2026-08"]
    test = df[df["month"] == "2026-08"]

    # winner from v2: gap>=0.5 vol>=3(14d) pre30>=0.3 rs>med adx>=20 rising atr>=1.2
    base = (
        (df["gap_pct"] >= 0.5) & (df["v30x_14d"] >= 3.0) & (df["pre30_ret"] >= 0.3) &
        df["rs_ok"] & (df["adx14"] >= 20) & (df["adx14"] > df["adx_prev"]) &
        (df["atr_pct"] >= 1.2) & (df["prev_pre30_ret"] <= 3.0) & (df["o"] >= 100)
    )
    # single-block lifts
    blocks = {
        "baseline (open>=100 only)": df["o"] >= 100,
        "C_close_pos>=0.9 only": (df["close_pos45"] >= 0.9) & (df["o"] >= 100),
        "C_obv_ok only": df["obv_ok"] & (df["o"] >= 100),
        "winner (v2)": base,
        "winner + close_pos>=0.9": base & (df["close_pos45"] >= 0.9),
        "winner + obv_ok": base & df["obv_ok"],
        "winner + close_pos + obv": base & (df["close_pos45"] >= 0.9) & df["obv_ok"],
        "winner minus RS": base & ~df["rs_ok"] | (base & (df["close_pos45"] >= 0.9)),
    }
    # cleaner: evaluate each explicitly
    evals = [
        ("baseline", df["o"] >= 100),
        ("close_pos>=0.9 only", (df["close_pos45"] >= 0.9) & (df["o"] >= 100)),
        ("obv_ok only", df["obv_ok"] & (df["o"] >= 100)),
        ("close_pos & obv", (df["close_pos45"] >= 0.9) & df["obv_ok"] & (df["o"] >= 100)),
        ("winner", base),
        ("winner + close_pos", base & (df["close_pos45"] >= 0.9)),
        ("winner + obv_ok", base & df["obv_ok"]),
        ("winner + both", base & (df["close_pos45"] >= 0.9) & df["obv_ok"]),
        ("winner minus RS (no RS gate)", (
            (df["gap_pct"] >= 0.5) & (df["v30x_14d"] >= 3.0) & (df["pre30_ret"] >= 0.3) &
            (df["adx14"] >= 20) & (df["adx14"] > df["adx_prev"]) &
            (df["atr_pct"] >= 1.2) & (df["prev_pre30_ret"] <= 3.0) & (df["o"] >= 100)
        )),
    ]
    for title, cond in evals:
        tr = evaluate(train, cond)
        te = evaluate(test, cond)
        def fmt(r):
            return f"n={r['n']:4} {r['per_day']:4.1f}/d P10={r['p10']:4.0%} med={r['med']:+.2f} mean={r['mean']:+.2f} win={r['win']:.0%}" if r else "— no sig / no lift"
        print(f"{title:<26} TRAIN {fmt(tr)}  |  TEST {fmt(te)}")


if __name__ == "__main__":
    main()
