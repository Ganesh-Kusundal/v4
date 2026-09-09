"""Workspace persistence — opaque-blob chart-state CRUD (Tier: D).

Covers docs/superpowers/specs/2026-09-09-workspace-persistence-design.md:
round-trip fidelity, optimistic-concurrency 409, force override, metadata
list without blobs, idempotent delete, 404 on absent, 422 on bad id.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tradex_trading.interface.fastapi_app import create_app


def _client():
    # :memory: workspace (default when TRADEX_WORKSPACE_DB unset — tests set
    # the env to keep runs hermetic).
    app = create_app(session=None)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _hermetic_workspace_db(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADEX_WORKSPACE_DB", str(tmp_path / "workspace.sqlite"))


def test_put_get_roundtrip_preserves_blob():
    client = _client()
    blob = {"drawings": [{"type": "trend-line", "points": [[1, 2], [3, 4]]}], "panels": ["rsi"]}
    put = client.put("/api/charts/workspace/NSE:RELIANCE_1m_default", json={"data": blob})
    assert put.status_code == 200
    assert put.json()["revision"] == 1
    got = client.get("/api/charts/workspace/NSE:RELIANCE_1m_default")
    assert got.status_code == 200
    body = got.json()
    assert body["data"] == blob
    assert body["revision"] == 1
    assert body["updated_at"]


def test_revision_conflict_409_and_force():
    client = _client()
    client.put("/api/charts/workspace/lay", json={"data": {"v": 1}})
    # stale revision (1 vs stored 1 → equal is fine; store bumps to 2)
    ok = client.put("/api/charts/workspace/lay", json={"data": {"v": 2}, "revision": 1})
    assert ok.status_code == 200
    assert ok.json()["revision"] == 2
    # now revision 1 is stale
    conflict = client.put("/api/charts/workspace/lay", json={"data": {"v": 3}, "revision": 1})
    assert conflict.status_code == 409
    detail = conflict.json()["detail"] if "detail" in conflict.json() else conflict.json()["error"]["message"]
    if isinstance(detail, dict):
        assert detail["stored_revision"] == 2
    else:
        assert "2" in str(conflict.json())
    forced = client.put("/api/charts/workspace/lay", json={"data": {"v": 3}, "revision": 1, "force": True})
    assert forced.status_code == 200


def test_list_returns_metadata_without_blobs():
    client = _client()
    client.put("/api/charts/workspace/a", json={"data": {"big": "x" * 100}})
    listing = client.get("/api/charts/workspace")
    assert listing.status_code == 200
    entries = listing.json()
    assert len(entries) == 1
    assert entries[0]["layout_id"] == "a"
    assert "data" not in entries[0]


def test_delete_idempotent_and_404():
    client = _client()
    client.put("/api/charts/workspace/gone", json={"data": {}})
    assert client.delete("/api/charts/workspace/gone").status_code == 204
    assert client.delete("/api/charts/workspace/gone").status_code == 204
    assert client.get("/api/charts/workspace/gone").status_code == 404


def test_bad_layout_id_422():
    client = _client()
    bad = "x" * 257
    assert client.put(f"/api/charts/workspace/{bad}", json={"data": {}}).status_code == 422
    assert client.get("/api/charts/workspace/").status_code in (200, 404)
