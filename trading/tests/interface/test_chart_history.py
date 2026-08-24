"""Chart history endpoint tests — datalake precedence, resample parity, IST edge."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from tradex_trading.interface.fastapi_app import create_app  # noqa: E402

pytest.importorskip("pandas")


def _client() -> TestClient:
    return TestClient(create_app(session=None))


class TestHistoryContract:
    def test_unsupported_interval_is_422(self):
        client = _client()
        resp = client.get("/api/charts/history/NSE:RELIANCE", params={"interval": "M"})
        assert resp.status_code == 422
        assert "unsupported interval" in resp.json()["detail"]

    def test_unknown_symbol_empty_bars_not_error(self):
        """A symbol the datalake never saw is an empty series, not a 500."""
        client = _client()
        resp = client.get(
            "/api/charts/history/NSE:NOSUCHSTOCK",
            params={"interval": "5m", "from": 1700000000, "to": 1700100000},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["bars"] == []
        assert body["source"] == "none"
        assert body["last_closed_time"] is None

    def test_resample_parity_with_direct_series(self):
        """API M5 bars equal HistoricalSeries.resample(M5) over the same window.

        This is the golden-parity gate: the endpoint must serve exactly what
        the canonical single-sourced builders produce, not its own math.
        """
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        from tradex_brokers.common.market_builders import candles_from_dataframe
        from tradex_domain.enums import Timeframe
        from tradex_domain.instruments import Equity
        from tradex_domain.market import HistoricalSeries
        from tradex_trading.datalake.parquet_storage import ParquetStorage

        store = ParquetStorage("data/")
        symbols = store.symbols()
        if not symbols:
            pytest.skip("empty datalake")
        symbol = sorted(symbols)[0]
        lo, hi = store.date_range(symbol)
        if lo is None or hi is None:
            pytest.skip("symbol has no data")

        ist = ZoneInfo("Asia/Kolkata")
        start_naive, end_naive = lo, min(hi, lo + timedelta(days=3))
        df = store.read(symbols=[symbol], start=start_naive, end=end_naive)
        if df.empty:
            pytest.skip("window has no data")

        instrument = Equity.of("NSE", symbol)
        direct = HistoricalSeries(
            instrument=instrument,
            timeframe=Timeframe.M1,
            candles=candles_from_dataframe(instrument, df, timeframe=Timeframe.M1),
            start=start_naive,
            end=end_naive,
        ).resample(Timeframe.M5)

        expected = [_bar(c) for c in direct.candles]

        from_utc = int(start_naive.replace(tzinfo=ist).timestamp())
        to_utc = int(end_naive.replace(tzinfo=ist).timestamp())
        with patch(
            "tradex_trading.interface.chart_api._get_store", return_value=store
        ):
            client = _client()
            resp = client.get(
                f"/api/charts/history/NSE:{symbol}",
                params={"interval": "5m", "from": from_utc, "to": to_utc},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "datalake"
        # The endpoint truncates to a tail; the fixture window is small enough
        # to fit under any sane limit, so full-list equality must hold.
        assert len(expected) < 5000
        assert body["bars"] == expected
        assert body["last_closed_time"] == expected[-1]["time"]

    def test_timestamps_are_utc_seconds_of_ist_wall_clock(self):
        """A bar's chart time is the UTC epoch of the same wall clock read as IST."""
        from zoneinfo import ZoneInfo

        from tradex_trading.interface.chart_api import _ist_to_utc_seconds

        ist = ZoneInfo("Asia/Kolkata")
        # 09:15 IST == 03:45 UTC (IST is UTC+5:30, no DST).
        expected = int(datetime(2026, 7, 15, 3, 45, tzinfo=UTC).timestamp())
        assert _ist_to_utc_seconds(datetime(2026, 7, 15, 9, 15)) == expected
        assert int(
            datetime(2026, 7, 15, 9, 15).replace(tzinfo=ist).timestamp()
        ) == expected


# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------


def _bar(candle) -> dict:
    from tradex_trading.interface.chart_api import _ist_to_utc_seconds

    return {
        "time": _ist_to_utc_seconds(candle.timestamp),
        "open": float(candle.ohlc.open.value),
        "high": float(candle.ohlc.high.value),
        "low": float(candle.ohlc.low.value),
        "close": float(candle.ohlc.close.value),
        "volume": float(candle.volume.value),
    }
