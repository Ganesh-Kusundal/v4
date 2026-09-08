"""POST /api/charts/seasonality — heatmap table over a datalake bar window.

Offline only: the app is built session-less, so bars come purely from the
parquet datalake (no broker, no network). Tests never fail on empty data:
a missing symbol yields a well-formed empty table, and the data-backed
assertions degrade to shape checks when the lake has nothing in-window.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from tradex_trading.analytics.seasonality import (  # noqa: E402
    normalize_seasonality_params,
)
from tradex_trading.interface.fastapi_app import create_app  # noqa: E402


def _client() -> TestClient:
    return TestClient(create_app(session=None))


def _utc(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp())


# Wide window covering the lake's intraday history for real symbols.
FROM = _utc(2026, 1, 1)
TO = _utc(2026, 9, 8)


def test_unknown_symbol_returns_empty_table_not_500() -> None:
    client = _client()
    r = client.post(
        "/api/charts/seasonality",
        json={"exchange": "NSE", "symbol": "NO_SUCH_SYMBOL_XYZ", "interval": "D"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"table", "meta"}
    assert body["table"]["rows"] == []
    assert body["table"]["options"] == {}
    assert body["meta"]["source"] == "none"


def test_bad_body_param_string_cutoff_422() -> None:
    client = _client()
    r = client.post(
        "/api/charts/seasonality",
        json={
            "exchange": "NSE",
            "symbol": "RELIANCE",
            "interval": "D",
            "params": {"cutoffPercent": "lots"},
        },
    )
    assert r.status_code == 422, r.text


def test_bad_query_param_422() -> None:
    client = _client()
    r = client.post(
        "/api/charts/seasonality?cutoffPercent=abc",
        json={"exchange": "NSE", "symbol": "RELIANCE", "interval": "D"},
    )
    assert r.status_code == 422, r.text


def test_response_shape_table_meta_with_or_without_data() -> None:
    client = _client()
    r = client.post(
        "/api/charts/seasonality",
        json={
            "exchange": "NSE",
            "symbol": "RELIANCE",
            "interval": "D",
            "from": FROM,
            "to": TO,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"table", "meta"}
    assert set(body["table"]) >= {"rows", "options"}
    assert body["meta"]["source"] in {"datalake", "none"}
    rows = body["table"]["rows"]
    if not rows:
        # Empty lake window: well-formed empty table, never a failure.
        assert body["table"]["options"] == {}
        return
    assert rows[0][0]["text"] == "Year"
    assert body["table"]["options"]["position"] in {
        "bottom-left",
        "bottom-center",
        "bottom-right",
    }


def test_camelcase_body_params_mapped_to_snake_in_meta() -> None:
    client = _client()
    r = client.post(
        "/api/charts/seasonality",
        json={
            "exchange": "NSE",
            "symbol": "NO_SUCH_SYMBOL_XYZ",
            "interval": "D",
            "params": {"startYear": 2020, "showAvg": False},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["meta"]["params"] == {"start_year": 2020, "show_avg": False}


def test_query_params_override_body_params() -> None:
    client = _client()
    r = client.post(
        "/api/charts/seasonality?cutoffPercent=7",
        json={
            "exchange": "NSE",
            "symbol": "NO_SUCH_SYMBOL_XYZ",
            "interval": "D",
            "params": {"cutoffPercent": 5},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["meta"]["params"] == {"cutoff_percent": 7.0}


def test_normalize_accepts_snake_case_ignores_unknown_rejects_mistyped() -> None:
    assert normalize_seasonality_params({"start_year": 2019, "showPos": True}) == {
        "start_year": 2019,
        "show_pos": True,
    }
    # Unknown (future engine) keys never break the seam.
    assert normalize_seasonality_params({"timezone": "Asia/Kolkata"}) == {}
    with pytest.raises(ValueError):
        normalize_seasonality_params({"cutoff_percent": "10"})
    with pytest.raises(ValueError):
        normalize_seasonality_params({"showAvg": 1})
    with pytest.raises(ValueError):
        normalize_seasonality_params({"startYear": "2020"})
