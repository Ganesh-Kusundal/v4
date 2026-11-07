"""History cache hit/miss response shape parity."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tradex_interfaces.fastapi_app import create_app  # noqa: E402


def test_history_cache_shape_parity():
    client = TestClient(create_app(session=None))
    r1 = client.get("/api/charts/history/NSE:RELIANCE", params={"interval": "5m"})
    r2 = client.get("/api/charts/history/NSE:RELIANCE", params={"interval": "5m"})
    # Both responses should have the same top-level keys (HistoryResponse fields)
    data1 = r1.json()
    data2 = r2.json()
    expected_keys = {"symbol", "exchange", "interval", "source", "timeframe_resampled", "last_closed_time", "bars"}
    assert set(data1.keys()) == expected_keys
    assert set(data2.keys()) == expected_keys
    assert set(data1.keys()) == set(data2.keys())