"""Canonical OHLCV resampling SQL — the one blessed aggregation.

The datalake stores M1 bars. Every higher timeframe derives from them via
this template, using DuckDB ordered aggregates so open/close follow
timestamp order even when rows arrive unordered:

    first(open ORDER BY timestamp), last(close ORDER BY timestamp)

Bucket labels are the bucket START (``time_bucket`` semantics), matching how
pandas ``resample(...).agg(...)`` labels bins by default.
"""

from __future__ import annotations

RESAMPLE_SQL = """
SELECT
    symbol,
    time_bucket(INTERVAL '{interval}', ts) AS bucket,
    first(open  ORDER BY timestamp) AS open,
    max(high)                        AS high,
    min(low)                         AS low,
    last(close ORDER BY timestamp)   AS close,
    sum(volume)                      AS volume,
    count(*)                         AS n_bars
FROM ohlcv
WHERE timestamp >= TIMESTAMP '{start}'
  AND timestamp <= TIMESTAMP '{end}'
  {symbols_clause}
GROUP BY symbol, bucket
ORDER BY symbol, bucket
"""

TF_TO_INTERVAL: dict[str, str] = {
    "1m": "1 minute",
    "5m": "5 minutes",
    "15m": "15 minutes",
    "30m": "30 minutes",
    "1h": "1 hour",
    "1d": "1 day",
}


def resample_sql(
    timeframe: str,
    start: str,
    end: str,
    symbols: list[str] | None = None,
) -> str:
    """Render RESAMPLE_SQL for a timeframe over an inclusive window.

    *timeframe* must be a key of ``TF_TO_INTERVAL`` (session-stripped M1 in,
    so a '1d' bucket equals one NSE trading day). When *symbols* is given it
    is expanded into an IN-list of quoted literals — values come from our own
    symbol list / universe CSVs, never free user text.
    """
    interval = TF_TO_INTERVAL.get(timeframe)
    if interval is None:
        raise ValueError(f"unsupported timeframe {timeframe!r}; expected {sorted(TF_TO_INTERVAL)}")
    symbols_clause = ""
    if symbols is not None:
        if not symbols:
            # Empty selection means no data, not everything.
            symbols_clause = "AND false"
        else:
            quoted = ", ".join("'" + s.replace("'", "''") + "'" for s in symbols)
            symbols_clause = f"AND symbol IN ({quoted})"
    return RESAMPLE_SQL.format(
        interval=interval,
        start=start,
        end=end,
        symbols_clause=symbols_clause,
    )


__all__ = ["RESAMPLE_SQL", "TF_TO_INTERVAL", "resample_sql"]
