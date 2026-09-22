"""Exhaustive screener optimization over ALL lake history.

One feature matrix (37,987 symbol-days, 76 sessions) feeding a threshold-grid
search. Every candidate config is evaluated at the 09:45 decision point with
the target = same-day top-gainer rank (09:45 -> 15:15).

Honest protocol:
- TRAIN May-Jul selects the best config (objective: max mean intraday gain
  subject to >=0.7 signals/day and P(top10) above unconditional baseline).
- TEST Aug is touched once, for the chosen config + runners-up.

Run: .venv/bin/python services/duckdb-analytics/research/screener_optimize_study.py
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

FEATURE_SQL = """
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
feat AS (
    SELECT symbol, d, o,
        round((o / NULLIF(lag(c) OVER wd, 0) - 1) * 100, 3) AS gap_pct,
        round((px45 / NULLIF(o, 0) - 1) * 100, 3) AS pre30_ret,
        round(v30 / NULLIF(avg(v30) OVER (PARTITION BY symbol ORDER BY d
            ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING), 0), 2) AS v30x,
        round((c_win / NULLIF(o_win, 0) - 1) * 100, 3) AS gain_target,
        lag(round((px45 / NULLIF(o, 0) - 1) * 100, 3))
            OVER (PARTITION BY symbol ORDER BY d) AS prev_pre30_ret,
        row_number() OVER (PARTITION BY symbol ORDER BY d DESC) AS rn_day
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
        CASE WHEN (h - ph) > (pl - l) AND (h - ph) > 0
             THEN h - ph ELSE 0 END AS pdm_raw,
        CASE WHEN (pl - l) > (h - ph) AND (pl - l) > 0
             THEN pl - l ELSE 0 END AS mdm_raw
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
    SELECT symbol, d,
        100 * abs(pdi - mdi) / NULLIF(pdi + mdi, 0) AS dx_val
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
)
SELECT f.symbol, f.d, f.o AS open_px, f.gap_pct, f.pre30_ret, f.v30x,
       f.prev_pre30_ret, a.adx14, a.adx_prev,
       f.gain_target,
       row_number() OVER (PARTITION BY f.d
           ORDER BY f.gain_target DESC) AS rk_gainer
FROM feat f
JOIN adxp a USING (symbol, d)
WHERE f.gain_target IS NOT NULL AND f.v30x IS NOT NULL
  AND f.prev_pre30_ret IS NOT NULL AND f.gap_pct IS NOT NULL
  AND a.adx14 IS NOT NULL AND a.adx_prev IS NOT NULL
"""


def load_master(svc: QueryService) -> pd.DataFrame:
    res = svc.execute(FEATURE_SQL, limit=50_000)
    df = pd.DataFrame(res.rows, columns=[
        "symbol", "d", "open_px", "gap_pct", "pre30_ret", "v30x",
        "prev_pre30_ret", "adx14", "adx_prev", "gain_target", "rk_gainer",
    ])
    df["d"] = pd.to_datetime(df["d"]).dt.strftime("%Y-%m-%d")
    nb = load_n_bars(svc, sorted(df["symbol"].unique()))
    rs_map = rs_scores_per_day(nb, sorted(df["d"].unique()))
    df = df.merge(rs_map, left_on=["symbol", "d"],
                  right_on=["symbol", "next_d"], how="left").drop(columns=["next_d"])
    df["is_top10"] = df["rk_gainer"] <= 10
    df["is_top20"] = df["rk_gainer"] <= 20
    df["month"] = df["d"].str[:7]
    return df


GRID = {
    "gap_min": [None, 0.5, 1.0, 1.5],
    "vol_mult": [None, 1.5, 2.0, 3.0],
    "pre30_min": [None, 0.3, 0.5],
    "use_rs": [False, True],
    "use_adx": [False, True],
    "exhaust_cap": [None, 3.0],
}


def mask_for(frame: pd.DataFrame, cfg: dict) -> pd.Series:
    m = frame["open_px"] >= 100          # penny exclusion always on
    if cfg["gap_min"] is not None:
        m &= frame["gap_pct"] >= cfg["gap_min"]
    if cfg["vol_mult"] is not None:
        m &= frame["v30x"] >= cfg["vol_mult"]
    if cfg["pre30_min"] is not None:
        m &= frame["pre30_ret"] >= cfg["pre30_min"]
    if cfg["exhaust_cap"] is not None:
        m &= frame["prev_pre30_ret"] <= cfg["exhaust_cap"]
    if cfg["use_adx"]:
        med_ok = frame["adx14"] >= 20
        rising = frame["adx14"] > frame["adx_prev"]
        m &= med_ok & rising
    if cfg["use_rs"]:
        rs_med = frame.groupby("d")["rs_score"].transform("median")
        m &= frame["rs_score"].notna() & (frame["rs_score"] > rs_med)
    return m


def score(frame: pd.DataFrame, m: pd.Series, days: int) -> dict | None:
    sig = frame[m]
    n = len(sig)
    per_day = n / max(days, 1)
    if n < 10 or per_day < 0.7:
        return None
    p10 = sig["is_top10"].mean()
    base_p10 = 10 / frame.groupby("d").size().median()
    if p10 <= base_p10:
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
    parts = []
    for k, v in cfg.items():
        if v is None or v is False:
            continue
        parts.append(f"{k}={v}" if not isinstance(v, bool) else k)
    return ",".join(parts) or "(none)"


def main() -> None:
    cfg_db = AnalyticsConfig(base_path=Path("data/ohlcv"))
    cat = DuckDBCatalog(cfg_db)
    svc = QueryService(cat, cfg_db)
    master = load_master(svc)
    cat.close()

    train = master[master["month"] != "2026-08"]
    test = master[master["month"] == "2026-08"]

    keys = list(GRID)
    results = []
    for combo in itertools.product(*(GRID[k] for k in keys)):
        cfg = dict(zip(keys, combo, strict=True))
        s = score(train, mask_for(train, cfg), train["d"].nunique())
        if s is None:
            continue
        results.append({"cfg": cfg, **s})

    results.sort(key=lambda r: r["mean"], reverse=True)
    print(f"train-eligible configs: {len(results)} "
          f"(of {len(list(itertools.product(*GRID.values())))})")
    print("\ntop 8 by TRAIN expectancy (May-Jul) with TEST outcome:")
    print(f"{'config':<52}{'tr sig/d':>9}{'tr P10':>8}{'tr mean':>9}"
          f"{'te sig/d':>9}{'te P10':>8}{'te mean':>9}")
    for r in results[:8]:
        te = score(test, mask_for(test, r["cfg"]), test["d"].nunique())
        te_str = (f"{te['per_day']:>9}{te['p10']:>8.1%}{te['mean']:>+9.2f}"
                  if te else f"{'—':>26}")
        print(f"{fmt_cfg(r['cfg']):<52}{r['per_day']:>9}{r['p10']:>8.1%}"
              f"{r['mean']:>+9.2f}{te_str}")

    best = results[0]
    print(f"\nWINNER: {fmt_cfg(best['cfg'])}")
    # full-period view of the winner
    m_all = mask_for(master, best["cfg"])
    full = score(master[m_all.apply(lambda x: x)], m_all, master["d"].nunique())
    if full:
        print(f"FULL PERIOD: sig/day={full['per_day']}  P(top10)={full['p10']:.1%}  "
              f"P(top20)={full['p20']:.1%}  med={full['med']:+.2f}%  "
              f"mean={full['mean']:+.2f}%  win={full['win']:.0%}")
    return None


if __name__ == "__main__":
    main()
