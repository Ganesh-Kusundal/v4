"""Canned scanner templates — parameterized, point-in-time bounded SQL.

Every template takes a required ``as_of`` bound and injects
``timestamp <= TIMESTAMP '<as_of>'`` so no scan can see future bars. Values
interpolated into SQL are limited to our own enums/numbers; user strings are
expanded only through quoted IN-lists built from validated symbol names.

Templates operate on the session-stripped ``ohlcv`` view and derive higher
timeframes with the canonical resample (see ``resample.py``).

Indicator parity:
- SMA is a plain rolling mean — identical to ``analytics/indicators.sma``.
- RSI implements EXACT Wilder smoothing. ``analytics/indicators.rsi``
  recurses ``A_i = (A_{i-1}*(n-1) + g_i)/n`` seeded with the mean of the
  first *n* changes. Unrolled, the recurrence has a closed form:

      A_i = S * d^(i-n) + (d^i / n) * (T_i - T_n),   d = (n-1)/n

  where ``T_i`` is the running sum of ``g_j * d^-j`` over change indices j
  and ``S`` is the seed mean. Every term is a per-row expression plus one
  cumulative sum, so DuckDB evaluates it vectorized — algebraically equal to
  the Python recursion, gated by golden tests. Guard: keep scanned bar
  counts below ~9000/symbol (the decay powers overflow float64 beyond that;
  the lake's ~3-month M1 history resamples far under it).
"""

from __future__ import annotations

from dataclasses import dataclass

from duck_analytics.resample import resample_sql
from duck_analytics.rs_score import rs_score_ctes


def _in_list(symbols: list[str]) -> str:
    if not symbols:
        return "AND false"
    return "AND symbol IN (" + ", ".join("'" + s.replace("'", "''") + "'" for s in symbols) + ")"


@dataclass(frozen=True, slots=True)
class ScanQuery:
    """A ready-to-execute scanner query plus its description."""

    name: str
    sql: str
    description: str


def _technical_sql(
    bars_sql: str,
    *,
    sma_fast: int,
    sma_slow: int,
    rsi_length: int,
    where: str,
) -> str:
    """Latest-bar technical screen: SMAs + exact-Wilder RSI per symbol."""
    n = int(rsi_length)
    fast = int(sma_fast)
    slow = int(sma_slow)
    return f"""
WITH bars AS (
{bars_sql}
),
sma1 AS (
    SELECT symbol, bucket, close,
        avg(close) OVER (PARTITION BY symbol ORDER BY bucket
            ROWS BETWEEN {fast - 1} PRECEDING AND CURRENT ROW) AS sma_fast,
        avg(close) OVER (PARTITION BY symbol ORDER BY bucket
            ROWS BETWEEN {slow - 1} PRECEDING AND CURRENT ROW) AS sma_slow,
        count(*) OVER (PARTITION BY symbol ORDER BY bucket
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS bars_seen
    FROM bars
),
sma2 AS (
    SELECT symbol, bucket, close, bars_seen, sma_fast, sma_slow,
        lag(sma_fast) OVER (PARTITION BY symbol ORDER BY bucket) AS prev_sma_fast,
        lag(sma_slow) OVER (PARTITION BY symbol ORDER BY bucket) AS prev_sma_slow
    FROM sma1
),
chg AS (
    SELECT symbol, bucket, close,
        row_number() OVER (PARTITION BY symbol ORDER BY bucket) AS rn,
        close - lag(close) OVER (PARTITION BY symbol ORDER BY bucket) AS chg
    FROM bars
),
ci AS (
    SELECT symbol, bucket, rn - 1 AS idx,
        CASE WHEN chg >= 0 THEN chg ELSE 0 END AS gain,
        CASE WHEN chg <  0 THEN -chg ELSE 0 END AS loss
    FROM chg
    WHERE chg IS NOT NULL
),
decayed AS (
    SELECT symbol, bucket, idx, gain, loss,
        gain * power({n}.0 / ({n}.0 - 1), idx) AS gw,
        loss * power({n}.0 / ({n}.0 - 1), idx) AS lw
    FROM ci
),
runs AS (
    SELECT symbol, bucket, idx,
        sum(gw) OVER (PARTITION BY symbol ORDER BY idx) AS t_gain,
        sum(lw) OVER (PARTITION BY symbol ORDER BY idx) AS t_loss,
        gain, loss
    FROM decayed
),
seeded AS (
    SELECT symbol, bucket, idx, t_gain, t_loss,
        sum(gain) FILTER (WHERE idx <= {n}) OVER (PARTITION BY symbol) / {n}.0 AS s_gain,
        sum(loss) FILTER (WHERE idx <= {n}) OVER (PARTITION BY symbol) / {n}.0 AS s_loss,
        max(CASE WHEN idx = {n} THEN t_gain END) OVER (PARTITION BY symbol) AS tn_gain,
        max(CASE WHEN idx = {n} THEN t_loss END) OVER (PARTITION BY symbol) AS tn_loss
    FROM runs
),
-- Exact Wilder, divided through by d^idx (same row ⇒ cancels in the ratio):
-- A_i/d^i = S*d^-n + (U_i - U_n)/n,  U_i = running sum of g_idx * d^-idx.
rsi_c AS (
    SELECT symbol, bucket, idx,
        CASE WHEN s_loss <= 0 THEN 100.0
             ELSE 100.0 / (1 + (s_gain * power({n}.0 / ({n}.0 - 1), {n})
                                + (t_gain - tn_gain) / {n}.0)
                              / (s_loss * power({n}.0 / ({n}.0 - 1), {n})
                                 + (t_loss - tn_loss) / {n}.0))
        END AS rsi
    FROM seeded
    WHERE idx >= {n}
),
joined AS (
    SELECT b.symbol, b.bucket, b.close, b.sma_fast, b.prev_sma_fast,
           b.sma_slow, b.prev_sma_slow, r.rsi
    FROM sma2 b
    JOIN rsi_c r USING (symbol, bucket)
),
latest AS (
    SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY bucket DESC) AS newest
    FROM joined
)
SELECT symbol, bucket, close, sma_fast, sma_slow, rsi
FROM latest
WHERE newest = 1 AND ({where})
ORDER BY rsi ASC
"""


def scan_screener(
    as_of: str,
    *,
    gap_min_pct: float = 0.5,
    vol_multiple: float = 3.0,
    vol_lookback_days: int = 14,
    pre30_min_pct: float = 0.3,
    adx_len: int = 14,
    adx_min: float = 20.0,
    atr_min_pct: float | None = 1.2,
    atr_max_pct: float | None = None,
    prev_pre30_max_pct: float = 3.0,
    min_open: float = 100.0,
    strict: bool = False,
    days: int = 20,
    bars_per_day: int = 25,
    ma_lens: tuple[int, int, int] = (30, 45, 60),
    atr_len: int = 14,
    symbols: list[str] | None = None,
) -> ScanQuery:
    """THE screener — single entry point, tuned on the full lake.

    Walk-forward tuned (May-Jul train, Aug holdout) with 14-day rel-volume,
    daily ADX momentum, and daily ATR (% ) volatility sizing.

    Blocks (all evaluated strictly at the 09:45 decision point):
      - overnight gap >= ``gap_min_pct``%
      - first-30-min rel-volume >= ``vol_multiple`` x prior-14d avg
      - opening drive 09:15->09:45 >= ``pre30_min_pct``%
      - RS score (MA-structure/ATR, recency-weighted, 15m bars through
        yesterday) above the cross-symbol median
      - daily ADX(adx_len) >= ``adx_min`` and rising (momentum)
      - daily ATR% in [``atr_min_pct``%, ``atr_max_pct``%] (enough room,
        not an illiquid spike) — None disables the bound
      - previous day's own first-30m move <= ``prev_pre30_max_pct``%
      - open >= ``min_open`` (penny exclusion)
      - strict=True adds: price acceptance (close_pos45 >=0.9) + OBV
        confirmation (OBV > SMA20 & rising) — validated +0.88%/signal
        mean vs +0.30% without, P(top10) 15% vs 11% at 0.5 sig/day.
    """
    day = as_of.split(" ")[0]
    # RS through yesterday close only — midnight boundary avoids
    # leaking even the 09:15 bar of the signal day.
    prev_bound = f"{day} 00:00"
    rs_ctes = rs_score_ctes(
        end=prev_bound, days=days, bars_per_day=bars_per_day,
        interval="15 minutes", ma_lens=ma_lens, atr_len=atr_len,
    )
    n = int(adx_len)
    lb = int(vol_lookback_days)
    symbols_clause = _in_list(symbols) if symbols is not None else ""
    sql = f"""
WITH {rs_ctes},
rs_final AS (
    SELECT symbol, sum(n_raw * wgt) / NULLIF(sum(wgt), 0) AS rs_score
    FROM rs_scored
    GROUP BY symbol
    HAVING count(*) = {int(days) * int(bars_per_day)}
),
daily AS (
    SELECT symbol,
        ts::DATE AS d,
        first(open ORDER BY timestamp) AS o,
        last(close ORDER BY timestamp) AS c,
        max(high) AS h,
        min(low) AS l,
        sum(volume) AS v_day,
        sum(CASE WHEN CAST(timestamp AS TIME) < TIME '09:45'
                 THEN volume ELSE 0 END) AS v30,
        last(close ORDER BY timestamp) FILTER (
            WHERE CAST(timestamp AS TIME) <= TIME '09:45') AS px45,
        max(high) FILTER (WHERE CAST(timestamp AS TIME) <= TIME '09:45') AS hi45,
        min(low) FILTER (WHERE CAST(timestamp AS TIME) <= TIME '09:45') AS lo45
    FROM ohlcv
    WHERE timestamp <= TIMESTAMP '{as_of}' {symbols_clause}
    GROUP BY symbol, d
),
obv_base AS (
    SELECT symbol, d, c, v_day,
        CASE WHEN c > lag(c) OVER w THEN 1 WHEN c < lag(c) OVER w THEN -1 ELSE 0 END AS sgn
    FROM daily WINDOW w AS (PARTITION BY symbol ORDER BY d)
),
obv AS (
    SELECT symbol, d, sum(sgn * v_day) OVER (PARTITION BY symbol ORDER BY d) AS obv
    FROM obv_base
),
obv_flag AS (
    SELECT symbol, d,
        (obv > avg(obv) OVER (
            PARTITION BY symbol ORDER BY d
            ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
         AND obv > lag(obv, 5) OVER (PARTITION BY symbol ORDER BY d)) AS obv_ok
    FROM obv
),
pre AS (
    SELECT symbol, d, o, c, v30, px45, hi45, lo45,
        (px45 / NULLIF(o, 0) - 1) * 100 AS pre30_raw
    FROM daily
),
feat AS (
    SELECT symbol, d, o,
        round((o / NULLIF(lag(c) OVER wd, 0) - 1) * 100, 3) AS gap_pct,
        round(pre30_raw, 3) AS pre30_ret,
        round((px45 - lo45) / NULLIF(hi45 - lo45, 0), 3) AS close_pos45,
        round(v30 / NULLIF(avg(v30) OVER wv, 0), 2) AS v30x,
        lag(round(pre30_raw, 3)) OVER wd AS prev_pre30_ret,
        row_number() OVER (PARTITION BY symbol ORDER BY d DESC) AS rn_day
    FROM pre
    WINDOW wd AS (PARTITION BY symbol ORDER BY d),
           wv AS (PARTITION BY symbol ORDER BY d
               ROWS BETWEEN {lb} PRECEDING AND 1 PRECEDING)
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
    WHERE d < (SELECT max(d) FROM daily)
),
smooth AS (
    SELECT *,
        avg(pdm_raw) OVER (
            PARTITION BY symbol ORDER BY d
            ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS pdm_s,
        avg(mdm_raw) OVER (
            PARTITION BY symbol ORDER BY d
            ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS mdm_s,
        avg(tr) OVER (
            PARTITION BY symbol ORDER BY d
            ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS atr_s
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
            ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS adx14
    FROM dxr
),
adx_ranked AS (
    SELECT symbol, adx14,
        lag(adx14, 1) OVER (PARTITION BY symbol ORDER BY d) AS adx_ref,
        row_number() OVER (PARTITION BY symbol ORDER BY d DESC) AS rn
    FROM adxc
    WHERE adx14 IS NOT NULL
),
latest AS (
    SELECT f.*, r.rs_score, o.obv_ok
    FROM (SELECT * FROM feat WHERE rn_day = 1) f
    LEFT JOIN (SELECT symbol, rs_score FROM rs_final) r USING (symbol)
    LEFT JOIN obv_flag o USING (symbol, d)
),
atr_latest AS (
    SELECT s.symbol, s.atr_s AS atr_cur, s.atr_s * 100.0 / NULLIF(d.c, 0) AS atr_pct
    FROM smooth s JOIN daily d USING (symbol, d)
    WHERE s.d = (SELECT max(d) FROM smooth)
)
SELECT l.symbol, l.d AS day, l.o AS open_px, l.gap_pct,
       l.pre30_ret, l.v30x, round(l.rs_score, 3) AS rs_score,
       round(a.atr_cur, 2) AS atr, round(a.atr_pct, 2) AS atr_pct
FROM latest l
LEFT JOIN atr_latest a USING (symbol)
WHERE l.gap_pct >= {float(gap_min_pct)}
  AND l.v30x >= {float(vol_multiple)}
  AND l.pre30_ret >= {float(pre30_min_pct)}
  AND COALESCE(
        l.rs_score > (SELECT median(rs_score) FROM rs_final), false)
  AND EXISTS (
        SELECT 1 FROM adx_ranked ar
        WHERE ar.symbol = l.symbol AND ar.rn = 1
          AND ar.adx14 >= {float(adx_min)}
          AND ar.adx14 > ar.adx_ref)
  AND ({f"a.atr_pct >= {float(atr_min_pct)}" if atr_min_pct is not None else "TRUE"})
  AND ({f"a.atr_pct <= {float(atr_max_pct)}" if atr_max_pct is not None else "TRUE"})
  AND l.prev_pre30_ret IS NOT NULL
  AND l.prev_pre30_ret <= {float(prev_pre30_max_pct)}
  AND l.o >= {float(min_open)}
  {"AND l.close_pos45 >= 0.9 AND l.obv_ok" if strict else ""}
ORDER BY l.v30x DESC
"""
    return ScanQuery(
        name="scan_screener",
        sql=sql,
        description=(
            f"screener gap>={gap_min_pct}% vol>={vol_multiple}x(14d) "
            f"pre30>={pre30_min_pct}% RS>med ADX({adx_len})>={adx_min}rising "
            f"ATR%[{atr_min_pct},{atr_max_pct}] prevPre30<={prev_pre30_max_pct}% open>={min_open} "
            f"as_of={as_of}"
        ),
    )


def breadth(as_of: str, *, dma: int = 20, timeframe: str = "1d") -> ScanQuery:
    """Market breadth per day: % of symbols closing above their N-bar DMA."""
    bars = resample_sql(timeframe, start="1900-01-01", end=as_of)
    sql = f"""
WITH bars AS (
{bars}
),
with_dma AS (
    SELECT *,
        avg(close) OVER (
            PARTITION BY symbol ORDER BY bucket
            ROWS BETWEEN {int(dma) - 1} PRECEDING AND CURRENT ROW
        ) AS dma,
        lag(close) OVER (PARTITION BY symbol ORDER BY bucket) AS prev_close
    FROM bars
),
per_day AS (
    SELECT bucket,
        count(*) AS n_symbols,
        sum(CASE WHEN close > dma THEN 1 ELSE 0 END) AS above_dma,
        sum(CASE WHEN close > prev_close THEN 1 ELSE 0 END) AS advances,
        sum(CASE WHEN close < prev_close THEN 1 ELSE 0 END) AS declines
    FROM with_dma
    GROUP BY bucket
)
SELECT bucket, n_symbols, above_dma,
       round(100.0 * above_dma / NULLIF(n_symbols, 0), 2) AS pct_above_dma,
       advances, declines
FROM per_day
ORDER BY bucket DESC
"""
    return ScanQuery(
        name="breadth",
        sql=sql,
        description=f"% above {dma}-bar DMA + adv/dec per day, as_of={as_of}",
    )


__all__ = [
    "ScanQuery",
    "breadth",
    "scan_screener",
]
