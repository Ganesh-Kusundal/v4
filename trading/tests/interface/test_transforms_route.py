"""POST /api/charts/transforms/{id} — stateless transform endpoint.

The route is stateless: bars come in the body, so no datalake/session is
needed — these tests never gate on ``datalake_or_skip``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from tradex_trading.analytics.transforms import compute_transform  # noqa: E402
from tradex_trading.interface.fastapi_app import create_app  # noqa: E402

FIXTURES = json.loads(
    (Path(__file__).parents[1] / "analytics" / "goldens" / "fixtures.json").read_text()
)
BARS = [
    {"time": t, "open": o, "high": h, "low": low, "close": c, "volume": v}
    for t, o, h, low, c, v in FIXTURES["bars"]
]


def _client() -> TestClient:
    return TestClient(create_app(session=None))


def test_transforms_endpoint_matches_compute_transform() -> None:
    client = _client()
    resp = client.post(
        "/api/charts/transforms/heikin-ashi",
        json={"id": "heikin-ashi", "params": {}, "bars": BARS},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "heikin-ashi"
    assert body["bars"] == compute_transform("heikin-ashi", BARS, {})


def test_transforms_endpoint_accepts_params() -> None:
    client = _client()
    resp = client.post(
        "/api/charts/transforms/renko",
        json={"id": "renko", "params": {"box_size": 4}, "bars": BARS},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["bars"]) == len(compute_transform("renko", BARS, {"box_size": 4}))


def test_transforms_endpoint_unknown_id_422() -> None:
    client = _client()
    resp = client.post(
        "/api/charts/transforms/no-such",
        json={"id": "no-such", "params": {}, "bars": BARS},
    )
    assert resp.status_code == 422
