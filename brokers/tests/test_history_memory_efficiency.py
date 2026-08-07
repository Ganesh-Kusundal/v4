"""Tests for Dhan history() memory-efficient single-pass implementation (Step 3.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import Candle, HistoricalSeries
from tradex_domain.value_objects import InstrumentId
from tradex_domain.wire import InstrumentRegistry

from tradex_brokers.dhan.client import DhanApiClient


def _make_registry() -> InstrumentRegistry:
    reg = InstrumentRegistry()
    iid = InstrumentId.equity("NSE", "RELIANCE")
    reg.register(iid, {"key": "2885", "asset_class": "EQUITY"})
    reg.add_alias("2885", iid)
    return reg


def _equity() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _make_client(
    responses: list[dict] | None = None,
) -> tuple[DhanApiClient, MagicMock, InstrumentRegistry]:
    http = MagicMock()
    call_count = 0
    response_list = responses or [{}]

    def side_effect(*args: Any, **kwargs: Any) -> dict:
        nonlocal call_count
        resp = response_list[min(call_count, len(response_list) - 1)]
        call_count += 1
        return resp

    http.request.side_effect = side_effect
    http.submit_mutation.side_effect = side_effect
    registry = _make_registry()
    client = DhanApiClient(http=http, registry=registry, client_id="TEST123")
    return client, http, registry


def test_history_returns_correct_candles() -> None:
    response = {
        "data": {
            "open": [100.0, 101.0],
            "high": [105.0, 106.0],
            "low": [99.0, 100.0],
            "close": [104.0, 105.0],
            "volume": [1000, 1200],
            "timestamp": ["2026-08-01", "2026-08-02"],
        }
    }
    client, _, _ = _make_client([response])
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 5, tzinfo=UTC)
    series = client.history(_equity(), Timeframe.D1, start, end)
    assert isinstance(series, HistoricalSeries)
    assert len(series.candles) == 2

    candle0 = series.candles[0]
    assert isinstance(candle0, Candle)
    assert candle0.ohlc.open.value == Decimal("100")
    assert candle0.ohlc.high.value == Decimal("105")
    assert candle0.ohlc.low.value == Decimal("99")
    assert candle0.ohlc.close.value == Decimal("104")
    assert candle0.volume.value == Decimal("1000")

    candle1 = series.candles[1]
    assert candle1.ohlc.open.value == Decimal("101")
    assert candle1.volume.value == Decimal("1200")


def test_history_empty_response() -> None:
    response = {"data": {}}
    client, _, _ = _make_client([response])
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 5, tzinfo=UTC)
    series = client.history(_equity(), Timeframe.D1, start, end)
    assert isinstance(series, HistoricalSeries)
    assert len(series.candles) == 0


def test_history_mismatched_column_lengths_truncates() -> None:
    """When columns have different lengths, row_count = min, so no IndexError."""
    response = {
        "data": {
            "open": [100.0, 101.0, 102.0],
            "high": [105.0, 106.0],
            "low": [99.0, 100.0],
            "close": [104.0, 105.0],
            "volume": [1000, 1200],
            "timestamp": ["2026-08-01", "2026-08-02"],
        }
    }
    client, _, _ = _make_client([response])
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 5, tzinfo=UTC)
    series = client.history(_equity(), Timeframe.D1, start, end)
    # min(3, 2, 2, 2, 2, 2) = 2
    assert len(series.candles) == 2
