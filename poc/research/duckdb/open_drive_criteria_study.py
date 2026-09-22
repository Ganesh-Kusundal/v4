"""Validate the 6-criteria opening-drive screener against top-gainer outcomes.

Criteria (decision at 09:45 of day T):
  1. rel-volume (first-30m vs prior-6d avg) >= 2x
  2. first-30m change >= +0.5%
  3. daily ADX(14) > 20 and rising
  4. prev day's own first-30m move <= 3% (exhaustion guard)
  5. open >= 100 (no pennies)
Target: rank in the day's top gainers measured 09:45 -> 15:15.

Reports each criterion alone, the full combo, and combo variants across a
Jul validation / Aug test split. ADX here is Cutler-style SMA smoothing.

Run: .venv/bin/python services/duckdb-analytics/research/open_drive_criteria_study.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "services" / "duckdb-analytics" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
        round((px45 / NULLIF(o, 0) - 1) * 100, 3) AS pre30_ret,
        round(v30 / NULLIF(avg(v30) OVER (PARTITION BY symbol ORDER BY d
            ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING), 0), 2) AS v30x,
        round((c_win / NULLIF(o_win, 0) - 1) * 100, 3) AS gain_target,
        lag(round((px45 / NULLIF(o, 0) - 1) * 100, 3))
            OVER (PARTITION BY symbol ORDER BY d) AS prev_pre30_ret
    FROM daily
),
dm_inner AS (
    SELECT symbol, d, h, l,
        lag(c) OVER w AS pc,
        lag(h) OVER w AS ph,
        lag(l) OVER w AS pl
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
        avg(pdm_raw) OVER (
            PARTITION BY symbol ORDER BY d
            ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS pdm_s,
        avg(mdm_raw) OVER (
            PARTITION BY symbol ORDER BY d
            ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS mdm_s,
        avg(tr) OVER (
            PARTITION BY symbol ORDER BY d
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
SELECT f.symbol,
    f.d,
    f.pre30_ret,
    f.v30x,
    f.prev_pre30_ret,
    f.o AS open_px,
    a.adx14,
    a.adx_prev,
    f.gain_target,
    row_number() OVER (PARTITION BY f.d
        ORDER BY f.gain_target DESC) AS rk_gainer
FROM feat f
JOIN adxp a USING (symbol, d)
WHERE f.gain_target IS NOT NULL
  AND f.v30x IS NOT NULL
  AND f.prev_pre30_ret IS NOT NULL
  AND a.adx14 IS NOT NULL
"""


def main() -> None:
    cfg = AnalyticsConfig(base_path=Path("data/ohlcv"))
    cat = DuckDBCatalog(cfg)
    svc = QueryService(cat, cfg)
    res = svc.execute(FEATURE_SQL, limit=50_000)
    cat.close()

    df = pd.DataFrame(res.rows, columns=[
        "symbol", "d", "pre30_ret", "v30x", "prev_pre30_ret", "open_px",
        "adx14", "adx_prev", "gain_target", "rk_gainer",
    ])
    df["d"] = pd.to_datetime(df["d"]).dt.strftime("%Y-%m-%d")
    df["is_top10"] = df["rk_gainer"] <= 10
    df["month"] = df["d"].str[:7]
    df["adx_rising"] = df["adx14"] > df["adx_prev"]

    c_vol = "v30x >= 2.0"
    c_mom = "pre30_ret >= 0.5"
    c_adx = "adx14 >= 20 and adx_rising"
    c_exh = "prev_pre30_ret <= 3.0"
    c_pen = "open_px >= 100"

    RULES = {
        "baseline (all)":            "open_px > 0",
        "C1 vol>=2x only":           c_vol,
        "C2 mom>=0.5% only":         c_mom,
        "C3 adx>20 rising only":     c_adx,
        "C4 no-exhaustion only":     c_exh,
        "C5 price>=100 only":        c_pen,
        "FULL combo C1-C5":          f"{c_vol} and {c_mom} and {c_adx} and {c_exh} and {c_pen}",
        "combo minus ADX":           f"{c_vol} and {c_mom} and {c_exh} and {c_pen}",
        "combo minus exhaustion":    f"{c_vol} and {c_mom} and {c_adx} and {c_pen}",
        "combo minus penny-filt":    f"{c_vol} and {c_mom} and {c_adx} and {c_exh}",
    }

    for tag, sub in [
        ("VALIDATION Jul", df[df["month"] == "2026-07"]),
        ("TEST Aug", df[df["month"] == "2026-08"]),
    ]:
        base_p10 = sub["is_top10"].mean()
        print(f"\n— {tag}: {len(sub)} rows, {sub['d'].nunique()} days "
              f"(baseline P(top10)={base_p10:.1%}, "
              f"median gain={sub['gain_target'].median():+.2f}%)")
        for name, cond in RULES.items():
            sig = sub.query(cond, engine="python")
            if sig.empty:
                print(f"  {name:<26} no signals")
                continue
            per_day = len(sig) / sig["d"].nunique()
            lift = sig["is_top10"].mean() / max(base_p10, 1e-9)
            print(f"  {name:<26} sig/day={per_day:6.1f}  "
                  f"P(top10)={sig['is_top10'].mean():5.1%} (x{lift:4.1f})  "
                  f"P(top20)={sig['rk_gainer'].le(20).mean():5.1%}  "
                  f"med={sig['gain_target'].median():+.2f}%  "
                  f"mean={sig['gain_target'].mean():+.2f}%  "
                  f"win={(sig['gain_target'] > 0).mean():.0%}")


if __name__ == "__main__":
    main()
