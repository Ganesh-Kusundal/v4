"""DTW pattern matching — find historical windows shaped like "right now".

Dynamic Time Warping aligns two sequences under monotone time shifts, so a
20-day pattern that unfolded over 18 or 22 days still matches. Pipeline:

1. Pull daily closes for the whole universe from the ``ohlcv`` view (SQL).
2. Query vector = z-normalized last ``window`` closes of the query symbol.
3. Scan every (symbol, end-date) window in history, z-normed; rank by
   banded DTW distance (Sakoe-Chiba constraint keeps it O(n*w) fast).
4. Attach each match's forward return after ``horizon`` days — the empirical
   "what usually happened next" distribution.

Pure numpy here; SQL does the bulk extraction. This is research tooling:
distances are descriptive, not trade signals by themselves.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def znorm(x: np.ndarray) -> np.ndarray:
    """Z-normalize; constant series map to zeros (never NaN)."""
    x = np.asarray(x, dtype=float)
    sd = x.std()
    if not np.isfinite(sd) or sd < 1e-12:
        return np.zeros_like(x)
    return (x - x.mean()) / sd


def dtw_distance(a: np.ndarray, b: np.ndarray, band_frac: float = 0.2) -> float:
    """Banded DTW between two 1-D sequences (z-normalize inputs yourself).

    Returns path-sum normalized by (n + m) so different lengths compare
    fairly. ``band_frac`` limits |i - j| to that fraction of max(n, m)
    (Sakoe-Chiba band) — both a speedup and a guard against degenerate
    warpings.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return float("inf")
    band = int(np.ceil(band_frac * max(n, m))) + abs(n - m)
    inf = float("inf")
    prev = np.full(m + 1, inf)
    curr = np.full(m + 1, inf)
    prev[0] = 0.0
    for i in range(1, n + 1):
        lo = max(1, i - band)
        hi = min(m, i + band)
        curr[:] = inf
        curr[0] = inf if i > band else prev[0]  # start-cell reachability
        ai = a[i - 1]
        for j in range(lo, hi + 1):
            cost = abs(ai - b[j - 1])
            curr[j] = cost + min(prev[j], curr[j - 1], prev[j - 1])
        prev, curr = curr, prev
    return float(prev[m] / (n + m))


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Top matches plus aggregate forward-outcome stats."""

    matches: pd.DataFrame          # symbol, end_date, dist, fwd_ret_pct
    baseline_median_fwd: float     # all-windows median forward return
    query_symbol: str
    window: int
    horizon: int


def load_daily_closes(
    svc,
    *,
    end: str,
    min_history: int,
    symbols: list[str] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Per-symbol daily close series ending at ``end`` (point-in-time safe)."""
    sym_clause = ""
    if symbols is not None:
        quoted = ", ".join("'" + s.replace("'", "''") + "'" for s in symbols)
        sym_clause = f"AND symbol IN ({quoted})"
    res = svc.execute(
        f"""
        SELECT symbol, ts::DATE AS d,
            last(close ORDER BY timestamp) AS close
        FROM ohlcv
        WHERE ts::DATE <= DATE '{end}' {sym_clause}
        GROUP BY symbol, d
        ORDER BY symbol, d
        """,
        limit=50_000,
    )
    df = pd.DataFrame(res.rows, columns=["symbol", "d", "close"])
    dates = sorted(df["d"].unique())
    if len(dates) < min_history:
        raise ValueError(f"only {len(dates)} distinct dates — need {min_history}")
    return df, [str(d) for d in dates]


def _matrix(df: pd.DataFrame, dates: list[str]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for sym, g in df.groupby("symbol", sort=False):
        out[sym] = dict(zip(g["d"].astype(str), g["close"].astype(float), strict=True))
    return out


def find_similar(
    svc,
    *,
    query_symbol: str,
    end: str,
    window: int = 20,
    horizon: int = 5,
    top_k: int = 10,
    band_frac: float = 0.2,
    exclude_overlap_days: int = 0,
) -> MatchResult:
    """Rank historical windows by DTW distance to the query's latest window.

    ``exclude_overlap_days`` drops same-symbol matches whose window ends
    within that many days of another kept match from the SAME symbol, so one
    long trend doesn't flood the top-k with near-duplicates.
    """
    need = window + horizon + 1
    df, dates = load_daily_closes(svc, end=end, min_history=need)
    px = _matrix(df, dates)

    if query_symbol not in px:
        raise ValueError(f"unknown query symbol {query_symbol!r}")
    q_dates_all = [d for d in dates if d in px[query_symbol]]
    if len(q_dates_all) < window:
        raise ValueError(f"{query_symbol} has {len(q_dates_all)} days < window {window}")
    q_dates = q_dates_all[-window:]
    query_vec = znorm([px[query_symbol][d] for d in q_dates])

    date_ix = {d: i for i, d in enumerate(dates)}
    rows: list[dict] = []
    fwd_rets_all: list[float] = []
    for sym, series in px.items():
        sdates = sorted(series)
        if len(sdates) < need and sym != query_symbol:
            continue
        arr = np.array([series.get(d, np.nan) for d in dates], dtype=float)
        for end_i in range(window - 1, len(dates)):
            w = arr[end_i - window + 1 : end_i + 1]
            if np.isnan(w).any():
                continue
            fwd = np.nan
            if end_i + horizon < len(dates):
                e_px, h_px = arr[end_i], arr[end_i + horizon]
                if not (np.isnan(e_px) or np.isnan(h_px)) and e_px > 0:
                    fwd = (h_px / e_px - 1) * 100
                    fwd_rets_all.append(fwd)
            dist = dtw_distance(query_vec, znorm(w), band_frac=band_frac)
            rows.append({
                "symbol": sym,
                "end_date": dates[end_i],
                "dist": round(dist, 5),
                "fwd_ret_pct": None if np.isnan(fwd) else round(fwd, 3),
            })

    cand = pd.DataFrame(rows)
    # Never include the query window itself (distance ~0 is self-match).
    cand = cand[~((cand["symbol"] == query_symbol)
                  & (cand["end_date"] == q_dates[-1]))]

    if exclude_overlap_days > 0:
        kept: list[pd.Series] = []
        used: dict[str, list[int]] = {}
        cand = cand.sort_values("dist")
        for r in cand.itertuples(index=False):
            ix = date_ix[r.end_date]
            if any(abs(ix - u) <= exclude_overlap_days
                   for u in used.get(r.symbol, [])):
                continue
            kept.append({
                "symbol": r.symbol,
                "end_date": r.end_date,
                "dist": r.dist,
                "fwd_ret_pct": r.fwd_ret_pct,
            })
            used.setdefault(r.symbol, []).append(ix)
            if len(kept) >= top_k:
                break
        top = pd.DataFrame(kept)
    else:
        top = cand.nsmallest(top_k, "dist")

    baseline = float(np.median(fwd_rets_all)) if fwd_rets_all else float("nan")
    return MatchResult(
        matches=top.reset_index(drop=True),
        baseline_median_fwd=baseline,
        query_symbol=query_symbol,
        window=window,
        horizon=horizon,
    )


__all__ = [
    "MatchResult",
    "dtw_distance",
    "find_similar",
    "load_daily_closes",
    "znorm",
]
