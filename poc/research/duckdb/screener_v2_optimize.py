"""Grid optimization with the new blocks: 14-day rel-volume, daily ADX momentum,
and daily ATR% volatility band.

Exactly mirrors screener_optimize_study.py's walk-forward (May-Jul train, Aug test)
but replaces the 6-day vol feature with a 14-day baseline and adds two ATR bounds
to size each stock's plausible intraday range.

Run: .venv/bin/python services/duckdb-analytics/research/screener_v2_optimize.py
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "services" / "duckdb-analytics" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rs_dtw_topgainer_study import load_n_bars, rs_scores_per_day  # noqa: E402

from duck_analytics.catalog import DuckDBCatalog  # noqa: E402
from duck_analytics.config import AnalyticsConfig  # noqa: E402
from duck_analytics.query import QueryService  # noqa: E402

FEATURE_SQL_V2 = """
WITH daily AS (
    SELECT symbol,
        ts::DATE AS d,
        first(open ORDER BY timestamp) AS o,
        last(close ORDER BY timestamp) AS c,
        max(high) AS h,
        min(low) AS l,
        sum(CASE WHEN CAST(timestamp AS TIME) < TIME '09:45'
                 THEN volume ELSE 0 END) AS v30,
        last(close ORDER BY timestamp) FILTER (
            WHERE CAST(timestamp AS TIME) <= TIME '09:45') AS px45,
        first(open ORDER BY timestamp) FILTER (
            WHERE CAST(timestamp AS TIME) >= TIME '09:45') AS o_win,
        last(close ORDER BY timestamp) FILTER (
            WHERE CAST(timestamp AS TIME) <= TIME '15:15') AS c_win
    FROM ohlcv
    WHERE year(ts::DATE) = 2026 AND month(ts::DATE) BETWEEN 5 AND 8
    GROUP BY symbol, d
),
feat14 AS (
    SELECT symbol, d, o,
        round((o / NULLIF(lag(c) OVER wd, 0) - 1) * 100, 3) AS gap_pct,
        round((px45 / NULLIF(o, 0) - 1) * 100, 3) AS pre30_ret,
        round(v30 / NULLIF(avg(v30) OVER (PARTITION BY symbol ORDER BY d
            ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING), 0), 2) AS v30x_14d,
        round((c_win / NULLIF(o_win, 0) - 1) * 100, 3) AS gain_target,
        lag(round((px45 / NULLIF(o, 0) - 1) * 100, 3))
            OVER (PARTITION BY symbol ORDER BY d) AS prev_pre30_ret
    FROM daily
    WINDOW wd AS (PARTITION BY symbol ORDER BY d)
),
dm_inner AS (
    SELECT symbol, d, h, l,
        lag(c) OVER w AS pc, lag(h) OVER w AS ph, lag(l) OVER w AS pl
    FROM daily
    WINDOW w AS (PARTITION BY symbol ORDER BY d)
),
dm AS (
    SELECT symbol, d,
        greatest(h - l, abs(h - pc), abs(l - pc)) AS tr,
        CASE WHEN (h - ph) > (pl - l) AND (h - ph) > 0 THEN h - ph ELSE 0 END AS pdm_raw,
        CASE WHEN (pl - l) > (h - ph) AND (pl - l) > 0 THEN pl - l ELSE 0 END AS mdm_raw
    FROM dm_inner
),
smooth AS (
    SELECT *,
        avg(pdm_raw) OVER (PARTITION BY symbol ORDER BY d
            ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS pdm_s,
        avg(mdm_raw) OVER (PARTITION BY symbol ORDER BY d
            ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS mdm_s,
        avg(tr) OVER (PARTITION BY symbol ORDER BY d
            ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS atr_s
    FROM dm
),
di AS (
    SELECT symbol, d,
        100 * pdm_s / NULLIF(atr_s, 0) AS pdi,
        100 * mdm_s / NULLIF(atr_s, 0) AS mdi
    FROM smooth
),
dxr AS (
    SELECT symbol, d, 100 * abs(pdi - mdi) / NULLIF(pdi + mdi, 0) AS dx_val
    FROM di
),
adxc AS (
    SELECT symbol, d,
        avg(dx_val) OVER (PARTITION BY symbol ORDER BY d
            ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS adx14
    FROM dxr
),
adxp AS (
    SELECT symbol, d, adx14,
        lag(adx14) OVER (PARTITION BY symbol ORDER BY d) AS adx_prev
    FROM adxc
),
atr_latest AS (
    SELECT s.symbol, s.d, s.atr_s AS atr_cur,
        s.atr_s * 100.0 / NULLIF(d.c, 0) AS atr_pct
    FROM smooth s JOIN daily d USING (symbol, d)
)
SELECT f.symbol, f.d, f.o AS open_px, f.gap_pct, f.pre30_ret, f.v30x_14d,
       f.prev_pre30_ret,
       a.adx14, a.adx_prev, t.atr_cur, t.atr_pct,
       f.gain_target,
       row_number() OVER (PARTITION BY f.d
           ORDER BY f.gain_target DESC) AS rk_gainer
FROM feat14 f
JOIN adxp a USING (symbol, d)
JOIN atr_latest t USING (symbol, d)
WHERE f.gain_target IS NOT NULL AND f.v30x_14d IS NOT NULL
  AND f.prev_pre30_ret IS NOT NULL AND f.gap_pct IS NOT NULL
  AND a.adx14 IS NOT NULL AND a.adx_prev IS NOT NULL
  AND t.atr_pct IS NOT NULL
"""


def load_master_v2(svc: QueryService) -> pd.DataFrame:
    res = svc.execute(FEATURE_SQL_V2, limit=50_000)
    df = pd.DataFrame(res.rows, columns=[
        "symbol", "d", "open_px", "gap_pct", "pre30_ret", "v30x_14d",
        "prev_pre30_ret", "adx14", "adx_prev", "atr_cur", "atr_pct",
        "gain_target", "rk_gainer",
    ])
    df["d"] = pd.to_datetime(df["d"]).dt.strftime("%Y-%m-%d")
    if not df.empty:
        nb = load_n_bars(svc, sorted(df["symbol"].unique()))
        rs_map = rs_scores_per_day(nb, sorted(df["d"].unique()))
        df = df.merge(rs_map, left_on=["symbol", "d"],
                      right_on=["symbol", "next_d"], how="left").drop(columns=["next_d"])
    df["is_top10"] = df["rk_gainer"] <= 10
    df["is_top20"] = df["rk_gainer"] <= 20
    df["month"] = df["d"].str[:7]
    return df


GRID_V2: dict[str, list] = {
    "gap_min": [None, 0.5, 1.0],
    "vol_mult": [None, 2.0, 3.0],
    "pre30_min": [None, 0.3, 0.5],
    "use_rs": [False, True],
    "use_adx": [False, True],
    "atr_min": [None, 0.8, 1.2],
    "atr_max": [None, 5.0, 6.0],
}


def mask_for_v2(frame: pd.DataFrame, cfg: dict) -> pd.Series:
    m = frame["open_px"] >= 100
    if cfg["gap_min"] is not None:
        m &= frame["gap_pct"] >= cfg["gap_min"]
    if cfg["vol_mult"] is not None:
        m &= frame["v30x_14d"] >= cfg["vol_mult"]
    if cfg["pre30_min"] is not None:
        m &= frame["pre30_ret"] >= cfg["pre30_min"]
    if cfg["use_rs"]:
        rs_med = frame.groupby("d")["rs_score"].transform("median")
        m &= frame["rs_score"].notna() & (frame["rs_score"] > rs_med)
    if cfg["use_adx"]:
        m &= (frame["adx14"] >= 20) & (frame["adx14"] > frame["adx_prev"])
    if cfg["atr_min"] is not None:
        m &= frame["atr_pct"] >= cfg["atr_min"]
    if cfg["atr_max"] is not None:
        m &= frame["atr_pct"] <= cfg["atr_max"]
    m &= frame["prev_pre30_ret"] <= 3.0
    return m


def score_v2(frame: pd.DataFrame, m: pd.Series, days: int) -> dict | None:
    sig = frame[m]
    n = len(sig)
    per_day = n / max(days, 1)
    if n < 10 or per_day < 0.7:
        return None
    p10 = sig["is_top10"].mean()
    if p10 <= 10 / frame.groupby("d").size().median():
        return None
    return {
        "per_day": round(per_day, 1),
        "n": n,
        "p10": p10,
        "p20": sig["is_top20"].mean(),
        "med": sig["gain_target"].median(),
        "mean": sig["gain_target"].mean(),
        "win": (sig["gain_target"] > 0).mean(),
    }


def fmt_cfg(cfg: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in cfg.items() if v not in (None, False)) or "pennies-only"


def main() -> None:
    cfg_db = AnalyticsConfig(base_path=Path("data/ohlcv"))
    cat = DuckDBCatalog(cfg_db)
    svc = QueryService(cat, cfg_db)
    master = load_master_v2(svc)
    cat.close()

    print(f"feature matrix: {len(master)} symbol-days, "
          f"RS coverage {(master['rs_score'].notna().mean()):.0%}")

    train = master[master["month"] != "2026-08"]
    test = master[master["month"] == "2026-08"]
    keys = list(GRID_V2)
    results: list[dict] = []
    for combo in itertools.product(*(GRID_V2[k] for k in keys)):
        cfg = dict(zip(keys, combo, strict=True))
        s = score_v2(train, mask_for_v2(train, cfg), train["d"].nunique())
        if s is None:
            continue
        results.append({"cfg": cfg, **s})

    results.sort(key=lambda r: r["mean"], reverse=True)
    print(f"train-eligible: {len(results)} / "
          f"{len(list(itertools.product(*GRID_V2.values())))}")
    print("\ntop 10 by TRAIN expectancy (May-Jul) -> TEST outcome:")
    print(f"{'config':<56}{'tr/d':>6}{'tr P10':>7}{'tr mean':>8}"
          f"{'te/d':>6}{'te P10':>7}{'te mean':>8}")
    for r in results[:10]:
        te = score_v2(test, mask_for_v2(test, r["cfg"]), test["d"].nunique())
        te_str = (f"{te['per_day']:>6}{te['p10']:>7.1%}{te['mean']:>+8.2f} "
                  if te else f"{'  —':>20} ")
        print(f"{fmt_cfg(r['cfg']):<56}{r['per_day']:>6}{r['p10']:>7.1%}"
              f"{r['mean']:>+8.2f}{te_str}")

    if results:
        print(f"\nWINNER: {fmt_cfg(results[0]['cfg'])}")


if __name__ == "__main__":
    main()
