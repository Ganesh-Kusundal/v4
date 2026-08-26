"""POST /api/charts/profiles/{id} — stateless profile/seasonality endpoint.

The route is stateless: bars come in the body, so no datalake/session is
needed — these tests never gate on ``datalake_or_skip``. The seasonality
branch dispatches inside the endpoint to ``compute_seasonality``; everything
else routes to ``compute_profile``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from tradex_trading.analytics.profiles import compute_profile  # noqa: E402
from tradex_trading.interface.fastapi_app import create_app  # noqa: E402

FIXTURES = json.loads(
    (Path(__file__).parents[1] / "analytics" / "goldens" / "fixtures.json").read_text()
)
BARS = [
    {"time": t, "open": o, "high": h, "low": low, "close": c, "volume": v}
    for t, o, h, low, c, v in FIXTURES["bars"]
]


def _seasonality_bars() -> list[dict]:
    # Deterministic 13-month series mirroring the golden-parity test
    # (Jan 2025..Jan 2026): one bar per month, close = 100 + 3*month_index.
    bars = []
    for m in range(13):
        year, month = (2025 + m // 12, m % 12 + 1)
        ts = int(datetime(year, month, 1, tzinfo=UTC).timestamp())
        close = 100.0 + 3 * m
        bars.append(
            {
                "time": ts,
                "open": close - 1,
                "high": close + 1,
                "low": close - 2,
                "close": close,
                "volume": 1000,
            }
        )
    return bars


def _client() -> TestClient:
    return TestClient(create_app(session=None))


def test_profiles_endpoint_volume_profile() -> None:
    client = _client()
    resp = client.post(
        "/api/charts/profiles/volume-profile",
        json={"id": "volume-profile", "params": {}, "bars": BARS},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "volume-profile"
    assert body["result"] == compute_profile("volume-profile", BARS, {})


def test_profiles_endpoint_seasonality() -> None:
    client = _client()
    resp = client.post(
        "/api/charts/profiles/seasonality",
        json={"id": "seasonality", "params": {"start_year": 2015}, "bars": _seasonality_bars()},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "seasonality"
    assert body["result"]["rows"][0][0]["text"] == "Year"


def test_profiles_endpoint_unknown_id_422() -> None:
    client = _client()
    resp = client.post(
        "/api/charts/profiles/no-such",
        json={"id": "no-such", "params": {}, "bars": BARS},
    )
    assert resp.status_code == 422
