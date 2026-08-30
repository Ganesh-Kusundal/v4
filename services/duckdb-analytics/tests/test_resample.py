"""Resample template parity vs pandas resample on the synthetic lake."""

from __future__ import annotations

import pandas as pd

from duck_analytics.resample import resample_sql


def _pandas_resample(service, symbol: str, rule: str,
                     start: str, end: str) -> pd.DataFrame:
    rows = service.execute(
        f"SELECT ts, open, high, low, close, volume FROM ohlcv "
        f"WHERE symbol='{symbol}' AND ts >= TIMESTAMP '{start}' "
        f"AND ts <= TIMESTAMP '{end}' ORDER BY ts"
    )
    df = pd.DataFrame(rows.rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    return (
        df.set_index("timestamp")
        .resample(rule)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open"])
    )


class TestResampleParity:
    def test_5min_matches_pandas(self, service):
        start, end = "2026-08-05 00:00:00", "2026-08-06 23:59:59"
        sql = resample_sql("5m", start, end, symbols=["TCS"])
        res = service.execute(sql)
        duck = res.rows
        pdf = _pandas_resample(service, "TCS", "5min", start, end)

        assert len(duck) == len(pdf)
        for (sym, bucket, o, hi, lo, c, v, n), (ts, prow) in zip(
            duck, pdf.iterrows(), strict=False
        ):
            assert sym == "TCS"
            assert bucket == ts
            assert abs(o - prow["open"]) < 1e-9
            assert abs(hi - prow["high"]) < 1e-9
            assert abs(lo - prow["low"]) < 1e-9
            assert abs(c - prow["close"]) < 1e-9
            assert v == prow["volume"]
            assert n == 5  # every 5m bucket here is full (30 contiguous minutes)

    def test_daily_bucket_is_one_trading_day(self, service):
        from duck_analytics.testing import DAYS

        sql = resample_sql("1d", "2026-08-05", "2026-08-12 23:59:59")
        res = service.execute(sql)
        per_symbol: dict[str, int] = {}
        for sym, *_rest in res.rows:
            per_symbol[sym] = per_symbol.get(sym, 0) + 1
        # Session-stripped M1 → one bucket per trading day.
        assert per_symbol == {"RELIANCE": len(DAYS), "TCS": len(DAYS)}

    def test_empty_symbols_selection_returns_nothing(self, service):
        sql = resample_sql("5m", "2026-08-05", "2026-08-06", symbols=[])
        assert service.execute(sql).rows == []

    def test_unsupported_timeframe_rejected(self):
        import pytest
        with pytest.raises(ValueError):
            resample_sql("7m", "2026-08-05", "2026-08-06")
