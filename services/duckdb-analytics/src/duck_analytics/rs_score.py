"""Relative Strength (RS) score — MA-structure momentum, ATR-normalized.

Per bar i over a trailing ``bars``-long window::

    W_i = exp(2 * ln(2) * i / bars)          # W(midpoint) = W(end)/2 exactly
    N_i = (P-MA30 + P-MA45 + P-MA60 +
           MA30-MA45 + MA30-MA60 + MA45-MA60) / ATR_i
    Score = sum(N_i * W_i) / sum(W_i)

All six pairwise MA spreads share the sign of trend strength, ATR normalizes
across symbols, and the exponential weight tilts the average toward the most
recent bars. ``bars`` derives from the timeframe: pass ``days`` and
``bars_per_day`` (NSE 15m = 25 bars/day; the classic 96 = 4x24 is crypto).

Computed entirely in SQL (window aggregates), one score per symbol. MAs are
simple rolling means and ATR is a rolling mean of true range (length
configurable) — this is a fresh indicator definition, so no parity claim to
``analytics/indicators.py`` is implied.
"""

from __future__ import annotations

# ln(4) == 2 * ln(2); W_i = 4 ** (j / bars) with j in [1..bars]
_W_BASE = "exp(ln(4.0) * ({}::DOUBLE / {}))"



def rs_score_ctes(
    *,
    end: str,
    days: int = 20,
    bars_per_day: int = 25,
    interval: str = "15 minutes",
    ma_lens: tuple[int, int, int] = (30, 45, 60),
    atr_len: int = 14,
) -> str:
    """Render the RS computation as a chain of ``rs_``-prefixed CTEs.

    Ends with ``rs_scored(symbol, i, n_raw, wgt)`` — one row per scored bar.
    Composable into larger scans that need RS context inline.
    """
    ma_s, ma_m, ma_l = (int(x) for x in ma_lens)
    bars = int(days) * int(bars_per_day)
    need = bars + ma_l + 1          # MA warmup + one extra bar for prev close
    start_j = need - bars           # scored rows have j = i - start_j + 1
    return f"""rs_bars_px AS (
    SELECT symbol,
        time_bucket(INTERVAL '{interval}', ts) AS bucket,
        max(high) AS high,
        min(low) AS low,
        last(close ORDER BY timestamp) AS close
    FROM ohlcv
    WHERE timestamp <= TIMESTAMP '{end}'
    GROUP BY symbol, bucket
    HAVING count(*) >= {max(ma_s // 2, 2)}
),
rs_ranked AS (
    SELECT *,
        row_number() OVER (PARTITION BY symbol ORDER BY bucket DESC) AS rn_desc
    FROM rs_bars_px
),
rs_tail AS (
    SELECT symbol, bucket, high, low, close,
        (count(*) OVER (PARTITION BY symbol)) - rn_desc AS i
    FROM rs_ranked
    WHERE rn_desc <= {need}
),
rs_tr AS (
    SELECT *,
        CASE WHEN lag(close) OVER (PARTITION BY symbol ORDER BY i) IS NULL
             THEN high - low
             ELSE greatest(
                  high - low,
                  abs(high - lag(close) OVER (PARTITION BY symbol ORDER BY i)),
                  abs(low  - lag(close) OVER (PARTITION BY symbol ORDER BY i)))
        END AS tr_rng
    FROM rs_tail
),
rs_ind AS (
    SELECT *,
        avg(close) OVER (PARTITION BY symbol ORDER BY i
            ROWS BETWEEN {ma_s - 1} PRECEDING AND CURRENT ROW) AS ma_s,
        avg(close) OVER (PARTITION BY symbol ORDER BY i
            ROWS BETWEEN {ma_m - 1} PRECEDING AND CURRENT ROW) AS ma_m,
        avg(close) OVER (PARTITION BY symbol ORDER BY i
            ROWS BETWEEN {ma_l - 1} PRECEDING AND CURRENT ROW) AS ma_l,
        avg(tr_rng) OVER (PARTITION BY symbol ORDER BY i
            ROWS BETWEEN {int(atr_len) - 1} PRECEDING AND CURRENT ROW) AS atr
    FROM rs_tr
),
rs_scored AS (
    SELECT symbol, i,
        ((close - ma_s) + (close - ma_m) + (close - ma_l)
         + (ma_s - ma_m) + (ma_s - ma_l) + (ma_m - ma_l))
            / NULLIF(atr, 0) AS n_raw,
        {_W_BASE.format(f"(i - {start_j} + 1)", bars)} AS wgt
    FROM rs_ind
    WHERE i >= {start_j}
      AND ma_l IS NOT NULL AND atr IS NOT NULL AND atr > 0
)"""



def rs_score_sql(
    *,
    end: str,
    days: int = 20,
    bars_per_day: int = 25,
    interval: str = "15 minutes",
    ma_lens: tuple[int, int, int] = (30, 45, 60),
    atr_len: int = 14,
    symbols: list[str] | None = None,
) -> str:
    """Render the RS-score query. ``end`` is a hard point-in-time bound."""
    bars = int(days) * int(bars_per_day)

    ctes = rs_score_ctes(end=end, days=days, bars_per_day=bars_per_day,
                         interval=interval, ma_lens=ma_lens, atr_len=atr_len)
    if symbols is None:
        sym_filter = ""
    elif not symbols:
        sym_filter = "AND false"
    else:
        quoted = ", ".join("'" + x.replace("'", "''") + "'" for x in symbols)
        sym_filter = f"AND symbol IN ({quoted})"
    return f"""
WITH {ctes}
SELECT symbol,
    round(sum(n_raw * wgt) / NULLIF(sum(wgt), 0), 4) AS rs_score,
    count(*) AS n_scored
FROM rs_scored
WHERE 1 = 1 {sym_filter}
GROUP BY symbol
HAVING count(*) = {bars}
ORDER BY rs_score DESC
"""


__all__ = ["rs_score_ctes", "rs_score_sql"]
