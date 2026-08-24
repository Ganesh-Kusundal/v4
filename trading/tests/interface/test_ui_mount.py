"""Tests for the /ui static mount — opt-in by presence of frontend/dist."""

from __future__ import annotations

from unittest.mock import patch

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from tradex_trading.interface.fastapi_app import create_app  # noqa: E402


def _make_client(tmp_path, has_dist: bool) -> TestClient:
    dist = tmp_path / "dist"
    if has_dist:
        dist.mkdir()
        (dist / "index.html").write_text(
            "<!doctype html><html><head><title>TradeX Terminal</title></head>"
            "<body><div id=app></div></body></html>",
            encoding="utf-8",
        )
    with patch("tradex_trading.interface.fastapi_app._UI_DIST_DIR", dist):
        return TestClient(create_app(session=None))


class TestUiMount:
    def test_no_dist_means_no_mount(self, tmp_path):
        """A source checkout without a built frontend is API-only."""
        client = _make_client(tmp_path, has_dist=False)
        resp = client.get("/ui/")
        assert resp.status_code == 404

    def test_dist_is_served_at_ui(self, tmp_path):
        """When dist exists, /ui/ serves the built index.html."""
        client = _make_client(tmp_path, has_dist=True)
        resp = client.get("/ui/")
        assert resp.status_code == 200
        assert "TradeX Terminal" in resp.text

    def test_api_routes_unaffected(self, tmp_path):
        """The mount adds /ui without touching API behavior."""
        client = _make_client(tmp_path, has_dist=True)
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
