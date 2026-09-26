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

import ast
from pathlib import Path

import pytest


def _repo_root() -> Path:
    """Repo root as the interface package itself computes it."""
    from tradex_trading.interface import routes

    pkg_dir = Path(routes.__file__).resolve().parent
    # interface/routes -> interface -> tradex_trading -> src -> trading -> repo
    return pkg_dir.parents[4]


def _callee_name(func: ast.expr) -> str:
    """Bare or attribute callee name, e.g. ``ParquetStorage`` from ``x.P(...)``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


#: Constructors that take a datalake root as their first argument.
_LAKE_CONSTRUCTORS = ("ParquetStorage", "ParquetBacktestLoader")


def _second_root_definitions(tree: ast.AST) -> set[str]:
    """Lake-root literals this module *defines in code*.

    Parsed, not regex-scanned. The first version searched the raw text and
    failed on ``cli.py``'s own explanatory comment quoting the retired
    ``ROOT/"data"`` literal — a comment describing a bug is documentation, not a
    second definition. Only expressions count: a division producing a path, a
    ``default=`` keyword, or a lake constructor handed a bare relative string.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and isinstance(node.left, ast.Name)
            and node.left.id == "ROOT"
            and isinstance(node.right, ast.Constant)
            and node.right.value in ("data", "data/")
        ):
            found.add('ROOT / "data"')

        if (
            isinstance(node, ast.keyword)
            and node.arg == "default"
            and isinstance(node.value, ast.Constant)
            and node.value.value in ("data", "data/")
        ):
            found.add('default="data"')

        if (
            isinstance(node, ast.Call)
            and _callee_name(node.func) in _LAKE_CONSTRUCTORS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in ("data", "data/")
        ):
            found.add(f'{_callee_name(node.func)}("data")')
    return found


class TestDatalakeRoot:
    def test_root_is_repo_root_not_cwd(self):
        """Default datalake root points at <repo>/data regardless of cwd."""
        from tradex_market_data.paths import datalake_root

        root = Path(datalake_root())
        assert root == _repo_root() / "data"
        assert root.is_dir(), (
            f"default datalake root {root} does not exist; serve would "
            "return source:none with zero bars"
        )

    def test_root_is_absolute(self):
        """Anchor is absolute so a chdir during the process can't move it."""
        from tradex_market_data.paths import datalake_root

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
            "tradex_interfaces.routes.chart",
            "tradex_interfaces.routes.stream",
        ],
    )
    def test_no_cwd_relative_data_literals_in_interface(self, modpath):
        """Interface routes must not hand ParquetStorage a bare relative path.

        Any ``"data/"`` literal in a route re-introduces the cwd fragility
        (serve from trading/ -> empty chart). The only sanctioned default is
        ``paths.DATALAKE_ROOT``, so a route resolves its handle through
        ``datalake_root()`` rather than spelling the path itself.

        Parsed, not text-matched: the module must stay free to *describe* the
        retired literal in a comment without tripping its own check.
        """
        import importlib

        mod = importlib.import_module(modpath)
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        found = _second_root_definitions(tree)
        assert not found, (
            f"{modpath} defines the lake root itself ({sorted(found)}) instead of "
            "resolving it through tradex_trading.datalake.paths.datalake_root()"
        )

    def test_no_module_in_interface_defines_the_lake_root(self):
        """No module in the interface package may define the lake root itself.

        The route-scoped guard above covers only ``chart`` and ``stream``, and
        it matches one exact literal. That left ``tradex sync`` in ``cli.py``
        free to hardcode ``ROOT / "data"`` (phase 4B) — a *writer* in the
        production CLI that ignored ``$TRADEX_DATALAKE_ROOT`` while the *reader*
        routes honoured it. With the variable set, bars landed in a lake the
        server never read: the same empty-chart failure as the 2026-09-02
        ``trading/data/`` shadow lake, reached from the other side.

        So this scans every module under ``interface/``, recursively, for any
        second *definition* of the root, and it does so on the *parse tree*:
        the first attempt searched raw text and failed on ``cli.py``'s own
        explanatory comment quoting the retired literal. A comment describing a
        bug is documentation, not a second definition, and only a syntax-aware
        scan can tell the two apart.
        """
        import tradex_trading.interface as pkg

        pkg_dir = Path(pkg.__file__).resolve().parent
        offenders: list[str] = []
        scanned = 0
        for path in sorted(pkg_dir.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            scanned += 1
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            found = _second_root_definitions(tree)
            if found:
                offenders.append(f"{path.relative_to(pkg_dir)}: {sorted(found)}")

        assert scanned, f"no modules found under {pkg_dir}"
        assert not offenders, (
            "these interface modules define the datalake root themselves instead "
            "of calling tradex_trading.datalake.paths.datalake_root(), so "
            "$TRADEX_DATALAKE_ROOT cannot move their reads or writes:\n"
            + "\n".join(offenders)
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
