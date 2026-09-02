"""Datalake root resolution — serve must find data/ regardless of process cwd.

Regression guard for the 2026-09-02 integration breakage: `tradex serve` was
launched from `trading/` (one level below the repo root) and every
``ParquetStorage("data/")`` literal in the interface routes resolved to a
nonexistent ``trading/data/`` — the chart served ``source: "none"`` with zero
bars, so the frontend rendered an empty shellbar-only page and replay could
never open ("no bars to replay").

The contract under test: the default datalake root is anchored to the repo
root via package location (same convention as ``_UI_DIST_DIR``), NOT to the
process working directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _repo_root() -> Path:
    """Repo root as the interface package itself computes it."""
    from tradex_trading.interface import routes

    pkg_dir = Path(routes.__file__).resolve().parent
    # interface/routes -> interface -> tradex_trading -> src -> trading -> repo
    return pkg_dir.parents[4]


class TestDatalakeRoot:
    def test_root_is_repo_root_not_cwd(self):
        """Default datalake root points at <repo>/data regardless of cwd."""
        from tradex_trading.datalake.paths import datalake_root

        root = Path(datalake_root())
        assert root == _repo_root() / "data"
        assert root.is_dir(), (
            f"default datalake root {root} does not exist; serve would "
            "return source:none with zero bars"
        )

    def test_root_is_absolute(self):
        """Anchor is absolute so a chdir during the process can't move it."""
        from tradex_trading.datalake.paths import datalake_root

        assert Path(datalake_root()).is_absolute()

    def test_chart_routes_use_anchored_root(self, monkeypatch, tmp_path):
        """The default store handle resolves to the anchored root, not cwd.

        Simulates the original bug: chdir to a directory with no data/ and
        confirm the chart history datalake read still finds real bars.
        """
        from tradex_trading.datalake import paths
        from tradex_trading.interface.routes import chart

        monkeypatch.chdir(tmp_path)
        store = chart._get_store(paths.DATALAKE_ROOT)
        # ParquetStorage nests <root>/ohlcv; both sides must be repo-anchored.
        assert Path(store._ohlcv_root) == Path(paths.DATALAKE_ROOT) / "ohlcv"
        assert Path(paths.DATALAKE_ROOT).is_dir()

    @pytest.mark.parametrize(
        "modpath",
        [
            "tradex_trading.interface.routes.chart",
            "tradex_trading.interface.routes.stream",
        ],
    )
    def test_no_cwd_relative_data_literals_in_interface(self, modpath):
        """Interface routes must not hand ParquetStorage a bare relative path.

        Grep-level contract: any literal "data/" inside the interface package
        re-introduces the cwd fragility (serve from trading/ -> empty chart).
        The only sanctioned default is paths._DATALAKE_ROOT.
        """
        import importlib
        import inspect

        mod = importlib.import_module(modpath)
        src = inspect.getsource(mod)
        assert 'ParquetStorage("data/")' not in src, (
            f"{modpath} still constructs ParquetStorage from a cwd-relative "
            'literal "data/" — use tradex_trading.datalake.paths.datalake_root()'
        )

    def test_serve_finds_bars_from_foreign_cwd(self, monkeypatch, tmp_path):
        """End-to-end regression: history endpoint returns datalake bars even
        when the process cwd has no data/ (the exact 2026-09-02 breakage)."""
        fastapi = pytest.importorskip("fastapi")
        pytest.importorskip("pandas")
        from fastapi.testclient import TestClient

        from tradex_trading.interface.fastapi_app import create_app

        assert fastapi is not None
        monkeypatch.chdir(tmp_path)  # cwd without data/, like trading/ on 09-02
        client = TestClient(create_app(session=None))
        resp = client.get("/api/charts/history/NSE:RELIANCE", params={"interval": "5m"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "datalake", (
            f"serve from foreign cwd lost the datalake (source={body['source']}); "
            "frontend charts render empty and replay fails"
        )
        assert len(body["bars"]) > 0
