"""Tests for the /ui static mount, and for the artifact it serves.

Two halves, deliberately separate:

* the mount's own behavior, against a synthetic ``dist`` — whatever is on the
  developer's disk, these say the wiring is right;
* the *real* ``frontend/dist``, when one is present, against the build stamp it
  was built with. Without that second half, the only thing ever checked about the
  served UI is that it is 200 and not empty, which a stale bundle satisfies.

The second half skips on a checkout with no frontend build (CI's Python job, a
fresh clone), which is what the mount already does: absent dist means API-only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from tradex_trading.interface.fastapi_app import create_app  # noqa: E402

# The build the mount reads, and the stamp `npm run build` writes beside it.
REAL_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"
BUILD_STAMP = REAL_DIST / "BUILD_STAMP.json"


def sha256(raw: bytes) -> str:
    """`sha256:<hex>` — the form frontend/scripts/artifact-stamp.mjs records."""
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


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


@pytest.mark.skipif(
    not BUILD_STAMP.exists(),
    reason="no stamped frontend/dist on this checkout (the mount is API-only here)",
)
class TestServedArtifact:
    """The real dist, unpatched, must serve exactly the bytes the stamp records.

    The stamp says which build is current; this says the mount is serving *that*
    build and not an older one still sitting in the directory. It is also the
    check that catches the mount's html entry point drifting from the artifact —
    `GET /` rewrites index.html to inject an API key, so only `/ui/` is the
    untouched file, and only a byte comparison can tell the two apart.
    """

    def test_mount_serves_every_stamped_file(self) -> None:
        stamp = json.loads(BUILD_STAMP.read_text())
        files = stamp["files"]
        assert files, "the stamp names no files, so it cannot vouch for the build"
        client = TestClient(create_app(session=None))
        for rel, digest in sorted(files.items()):
            resp = client.get(f"/ui/{rel}")
            assert resp.status_code == 200, f"/ui/{rel} is not served"
            assert sha256(resp.content) == digest, f"/ui/{rel} is not the built file"

    def test_mount_entry_point_is_the_built_index(self) -> None:
        stamp = json.loads(BUILD_STAMP.read_text())
        client = TestClient(create_app(session=None))
        resp = client.get("/ui/")
        assert resp.status_code == 200
        assert sha256(resp.content) == stamp["files"]["index.html"]
