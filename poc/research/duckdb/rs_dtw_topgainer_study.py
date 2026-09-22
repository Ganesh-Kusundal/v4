"""Can RS-score + DTW pattern context improve top-gainer selection?

Decision point 09:45 on day T. Base filter R1 (gap>=1% & first-30m vol >=2x)
from opening_drive_study. New signals, both point-in-time safe:

- rs_prev : RS score (MA-structure / ATR, exponential recency weights) on
            15m bars through day T-1 close. Momentum-structure context.
- dtw_fwd : median forward 5-day return of the 10 nearest historical
            neighbors (banded-DTW on z-normalized 20-day close shapes,
            Euclidean-prefiltered). Only windows whose forward window
            COMPLETED before T are eligible — zero leakage.

Evaluated on Jul (validation) and Aug (test) separately.

Run: .venv/bin/python services/duckdb-analytics/research/rs_dtw_topgainer_study.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "services" / "duckdb-analytics" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from opening_drive_study import FEATURE_SQL, RULES  # noqa: E402

from duck_analytics.catalog import DuckDBCatalog  # noqa: E402
from duck_analytics.config import AnalyticsConfig  # noqa: E402
from duck_analytics.patterns import dtw_distance, znorm  # noqa: E402
from duck_analytics.query import QueryService  # noqa: E402

WINDOW = 20      # DTW shape length (daily closes)
HORIZON = 5      # forward outcome horizon (days)
BARS_RS = 500    # 20 days x 25 bars/day on 15m
RHO = np.exp(-np.log(4.0) / BARS_RS)

N_BARS_SQL = """
WITH bars15 AS (
    SELECT symbol,
        time_bucket(INTERVAL '15 minutes', ts) AS bucket,
        max(high) AS high,
        min(low) AS low,
        last(close ORDER BY timestamp) AS close
    FROM ohlcv
    WHERE year(ts::DATE) = 2026 AND month(ts::DATE) BETWEEN 6 AND 8
    GROUP BY symbol, bucket
),
seq AS (
    SELECT *,
        lag(close) OVER w AS prev_close,
        row_number() OVER (PARTITION BY symbol ORDER BY bucket) AS i
    FROM bars15
    WINDOW w AS (PARTITION BY symbol ORDER BY bucket)
),
tr AS (
    SELECT *,
        CASE WHEN prev_close IS NULL THEN high - low
             ELSE greatest(high - low,
                           abs(high - prev_close),
                           abs(low - prev_close)) END AS tr_rng
    FROM seq
)
SELECT symbol,
       bucket AS ts,
       round(((close - avg(close) OVER (PARTITION BY symbol ORDER BY i
                ROWS BETWEEN 29 PRECEDING AND CURRENT ROW))
            + (close - avg(close) OVER (PARTITION BY symbol ORDER BY i
                ROWS BETWEEN 44 PRECEDING AND CURRENT ROW))
            + (close - avg(close) OVER (PARTITION BY symbol ORDER BY i
                ROWS BETWEEN 59 PRECEDING AND CURRENT ROW))
            + (avg(close) OVER (PARTITION BY symbol ORDER BY i
                ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)
              - avg(close) OVER (PARTITION BY symbol ORDER BY i
                ROWS BETWEEN 44 PRECEDING AND CURRENT ROW))
            + (avg(close) OVER (PARTITION BY symbol ORDER BY i
                ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)
              - avg(close) OVER (PARTITION BY symbol ORDER BY i
                ROWS BETWEEN 59 PRECEDING AND CURRENT ROW))
            + (avg(close) OVER (PARTITION BY symbol ORDER BY i
                ROWS BETWEEN 44 PRECEDING AND CURRENT ROW)
              - avg(close) OVER (PARTITION BY symbol ORDER BY i
                ROWS BETWEEN 59 PRECEDING AND CURRENT ROW))
            ) / NULLIF(avg(tr_rng) OVER (PARTITION BY symbol ORDER BY i
                ROWS BETWEEN 13 PRECEDING AND CURRENT ROW), 0), 6) AS n_raw
FROM tr
WHERE symbol IN ({syms})
"""


def load_features(svc: QueryService) -> pd.DataFrame:
    res = svc.execute(FEATURE_SQL, limit=50_000)
    df = pd.DataFrame(res.rows, columns=[
        "symbol", "d", "gap_pct", "pre30_ret", "v30x", "close_pos45",
        "gain_target", "rk_gainer",
    ])
    df["d"] = pd.to_datetime(df["d"]).dt.strftime("%Y-%m-%d")
    df["is_top10"] = df["rk_gainer"] <= 10
    df["r1_pass"] = df.eval(RULES["R1_gap_and_go"], engine="python")
    return df


def load_n_bars(svc: QueryService, symbols: list[str]) -> pd.DataFrame:
    """Chunked by symbol to stay under the service row cap."""
    frames = []
    for i in range(0, len(symbols), 30):
        syms = ", ".join("'" + s.replace("'", "''") + "'" for s in symbols[i:i + 30])
        res = svc.execute(N_BARS_SQL.format(syms=syms), limit=50_000)
        frames.append(pd.DataFrame(res.rows, columns=["symbol", "ts", "n_raw"]))
    out = pd.concat(frames, ignore_index=True)
    print(f"N-bars pulled: {len(out)} rows across {out['symbol'].nunique()} symbols")
    return out


def rs_scores_per_day(nb: pd.DataFrame, feat_dates: list[str]) -> pd.DataFrame:
    """RS score at every day close: trailing-500-bar weighted mean of N."""
    nb = nb.sort_values(["symbol", "ts"])
    out_rows = []
    w_full = np.power(4.0, np.arange(BARS_RS) / BARS_RS)[::-1]  # newest heaviest
    for sym, g in nb.groupby("symbol", sort=False):
        n = g["n_raw"].to_numpy(dtype=float)
        if len(n) < BARS_RS:
            continue
        ts = g["ts"].to_numpy()
        # trailing weighted sum via sliding windows
        sw = np.lib.stride_tricks.sliding_window_view(n, BARS_RS)
        num = sw @ w_full
        den = w_full.sum()
        scores = num / den
        s_ts = ts[BARS_RS - 1:]
        s_dates = pd.to_datetime(s_ts).strftime("%Y-%m-%d")
        last_per_day = (
            pd.DataFrame({"d": s_dates, "rs": scores})
            .groupby("d", as_index=False).last()
        )
        last_per_day["symbol"] = sym
        out_rows.append(last_per_day)
    out = pd.concat(out_rows, ignore_index=True)
    # decision on day T uses RS through T-1
    out["next_d"] = out.groupby("symbol")["d"].shift(-1)
    return out.rename(columns={"d": "rs_asof", "rs": "rs_score"})[
        ["symbol", "next_d", "rs_score"]
    ]


class ShapeMatcher:
    """Euclidean-prefiltered, DTW-reranked historical shape matcher."""

    def __init__(self, svc: QueryService, end: str = "2026-08-21"):
        res = svc.execute(
            f"""
            SELECT symbol, ts::DATE AS d,
                last(close ORDER BY timestamp) AS close
            FROM ohlcv
            WHERE ts::DATE <= DATE '{end}'
            GROUP BY symbol, d
            ORDER BY symbol, d
            """,
            limit=50_000,
        )
        px_df = pd.DataFrame(res.rows, columns=["symbol", "d", "close"])
        self.dates = sorted(px_df["d"].astype(str).unique())
        self.date_ix = {d: i for i, d in enumerate(self.dates)}
        self.px: dict[str, np.ndarray] = {}
        for sym, g in px_df.groupby("symbol", sort=False):
            arr = np.full(len(self.dates), np.nan)
            ix = [self.date_ix[str(d)] for d in g["d"].astype(str)]
            arr[ix] = g["close"].to_numpy(float)
            self.px[sym] = arr

        # all windows: (symbol, end_ix) with complete shape + forward return
        syms, end_ixs, vecs, fwds = [], [], [], []
        for sym, arr in self.px.items():
            for e in range(WINDOW - 1, len(self.dates)):
                w = arr[e - WINDOW + 1 : e + 1]
                if np.isnan(w).any():
                    continue
                f = np.nan
                if e + HORIZON < len(self.dates) and arr[e] > 0:
                    h = arr[e + HORIZON]
                    if not np.isnan(h):
                        f = (h / arr[e] - 1) * 100
                syms.append(sym)
                end_ixs.append(e)
                vecs.append(znorm(w))
                fwds.append(f)
        self.W = np.asarray(vecs, dtype=np.float32)
        self.meta = pd.DataFrame({
            "symbol": syms, "end_ix": end_ixs, "fwd": fwds,
        })
        # precompute gram pieces for euclidean distances
        self.W_sq = (self.W ** 2).sum(axis=1)

    def match(self, q_vec: np.ndarray, cutoff_ix: int,
              k_euclid: int = 80, k_final: int = 10) -> float | None:
        """Median forward return of top-k neighbors with end_ix < cutoff."""
        ok = self.meta["end_ix"].to_numpy() < cutoff_ix - HORIZON
        idx = np.flatnonzero(ok)
        if len(idx) == 0:
            return None
        d2 = self.W_sq[idx] - 2.0 * (self.W[idx] @ q_vec)  # +|q|^2 const
        cand = idx[np.argsort(d2)[:k_euclid]]
        scored = sorted(
            (
                (dtw_distance(q_vec, self.W[c], band_frac=0.2), c)
                for c in cand
            ),
            key=lambda t: t[0],
        )[:k_final]
        fwds = [float(self.meta.iloc[c]["fwd"]) for _, c in scored]
        fwds = [f for f in fwds if np.isfinite(f)]
        return float(np.median(fwds)) if fwds else None


def main() -> None:
    cfg = AnalyticsConfig(base_path=Path("data/ohlcv"))
    cat = DuckDBCatalog(cfg)
    svc = QueryService(cat, cfg)

    feat = load_features(svc)
    print(f"features: {len(feat)} symbol-days")

    nb = load_n_bars(svc, sorted(feat["symbol"].unique()))
    rs_map = rs_scores_per_day(nb, sorted(feat["d"].unique()))
    feat = feat.merge(
        rs_map, left_on=["symbol", "d"], right_on=["symbol", "next_d"],
        how="left",
    ).drop(columns=["next_d"])
    have_rs = feat["rs_score"].notna().mean()
    print(f"RS coverage: {have_rs:.0%}")

    print("building shape index (all historical windows)...")
    matcher = ShapeMatcher(svc)
    cat.close()

    # dtw_fwd per candidate symbol-day (only R1-passing rows need it)
    need_mask = feat["r1_pass"] & feat["d"].between("2026-07-01", "2026-08-21")
    dtw_vals: dict[tuple[str, str], float] = {}
    for (sym, d) in feat.loc[need_mask, ["symbol", "d"]].itertuples(index=False):
        arr = matcher.px.get(sym)
        if arr is None:
            continue
        ix = matcher.date_ix.get(d)
        if ix is None or ix < WINDOW:
            continue
        q = znorm(arr[ix - WINDOW + 1 : ix + 1])
        val = matcher.match(q, cutoff_ix=ix)  # windows end strictly before T-h
        if val is not None:
            dtw_vals[(sym, d)] = val
    feat["dtw_fwd"] = [
        dtw_vals.get((s, d)) for s, d in zip(feat["symbol"], feat["d"], strict=True)
    ]
    cov = feat.loc[need_mask, "dtw_fwd"].notna().mean()
    print(f"DTW coverage on R1 candidates: {cov:.0%}")

    rs_med_by_day = feat.groupby("d")["rs_score"].transform("median")
    feat["rs_high"] = feat["rs_score"] > rs_med_by_day
    feat["dtw_pos"] = feat["dtw_fwd"] > 0
    feat["month"] = feat["d"].str[:7]

    RULE_SETS = {
        "R1 base":                        "r1_pass",
        "R1 + RS>med(day)":               "r1_pass and rs_high",
        "R1 + DTW-fwd>0":                 "r1_pass and dtw_pos",
        "R1 + RS + DTW (both)":           "r1_pass and rs_high and dtw_pos",
    }

    for tag, sub in [
        ("VALIDATION Jul", feat[feat["month"] == "2026-07"]),
        ("TEST Aug", feat[feat["month"] == "2026-08"]),
    ]:
        print(f"\n— {tag}: {len(sub)} rows, {sub['d'].nunique()} days "
              f"(baseline P(top10)={sub['is_top10'].mean():.1%})")
        for name, cond in RULE_SETS.items():
            sig = sub.query(cond, engine="python")
            if sig.empty:
                print(f"  {name:<24} no signals")
                continue
            per_day = len(sig) / sig["d"].nunique()
            print(f"  {name:<24} sig/day={per_day:5.1f}  "
                  f"P(top10)={sig['is_top10'].mean():5.1%}  "
                  f"P(top20)={sig['rk_gainer'].le(20).mean():5.1%}  "
                  f"recall={sig['is_top10'].sum() / max(sub['is_top10'].sum(), 1):5.1%}  "
                  f"med={sig['gain_target'].median():+.2f}%  "
                  f"mean={sig['gain_target'].mean():+.2f}%  "
                  f"win={(sig['gain_target'] > 0).mean():.0%}")


if __name__ == "__main__":
    main()
