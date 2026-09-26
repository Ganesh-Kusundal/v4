"""Every datalake consumer must resolve to the same anchored root.

Phase 4B anchored the lake through ``tradex_market_data.paths``, but four
constructor defaults kept their own literals: ``"data/"`` in
``backtest_loader.py`` and twice in ``market_provider.py``, and ``"data/lake"``
in ``catalog.py``. A default argument is a *relative* path, so it resolved
against the process cwd — exactly the 2026-09-02 incident, where ``tradex
serve`` launched from ``trading/`` opened a nonexistent ``trading/data/`` and
the chart API served ``source: "none"`` with zero bars.

The contract under test, for all four call sites:

- omitted path  -> the repo-anchored root, identical across call sites
- ``os.chdir``  -> does not move the resolved root (the actual bug)
- explicit path -> honoured unchanged (backwards compatibility)
"""

from __future__ import annotations

import ast
import os
from datetime import datetime
from pathlib import Path

import pytest

from tradex_market_data.backtest_loader import ParquetBacktestLoader
from tradex_market_data.catalog import DataCatalog
from tradex_market_data.market_provider import (
    BulkPrefetchMarketProvider,
    ParquetMarketProvider,
)
from tradex_market_data.paths import DATALAKE_ROOT, datalake_root

#: ``ParquetStorage`` nests the OHLCV partitions under ``<root>/ohlcv``.
_OHLCV = "ohlcv"

_START = datetime(2026, 7, 1)
_END = datetime(2026, 7, 2)


def _build_bulk(tmp_path: Path) -> BulkPrefetchMarketProvider:
    """BulkPrefetchMarketProvider with an isolated (empty) base path."""
    return BulkPrefetchMarketProvider(
        symbols=["RELIANCE"],
        start=_START,
        end=_END,
        base_path=str(tmp_path / "bulk"),
    )


def _bulk_default_root() -> str:
    """Default base path of BulkPrefetchMarketProvider, without side effects.

    ``__init__`` reads the parquet store, so the default is read straight off
    the signature rather than by constructing one against the real lake.
    """
    import inspect

    param = inspect.signature(BulkPrefetchMarketProvider.__init__).parameters[
        "base_path"
    ]
    assert param.default is None, (
        f"base_path default drifted to {param.default!r}; the anchored resolver "
        "belongs in the body, not the signature"
    )
    return datalake_root()


def _catalog_default_root() -> str:
    """Default root of DataCatalog, resolved without writing to the lake."""
    import inspect

    param = inspect.signature(DataCatalog.__init__).parameters["root"]
    assert param.default is None, (
        f"root default drifted to {param.default!r}; the anchored resolver "
        "belongs in the body, not the signature"
    )
    return datalake_root()


class TestAnchoredDefaults:
    def test_every_call_site_resolves_to_the_same_absolute_path(self):
        """All four defaults agree with each other and with the seam.

        ``catalog.py`` used ``"data/lake"`` while the rest used ``"data/"``,
        so the two could never name the same directory even after anchoring.
        """
        roots = {
            "backtest_loader": Path(ParquetBacktestLoader().provider.store.base_path),
            "market_provider": Path(ParquetMarketProvider().store.base_path),
            "bulk_prefetch_provider": Path(_bulk_default_root()),
            "catalog": Path(_catalog_default_root()),
        }
        distinct = {name: str(path) for name, path in roots.items()}
        assert len(set(distinct.values())) == 1, (
            f"datalake defaults diverged: {distinct}"
        )
        assert Path(next(iter(roots.values()))) == Path(datalake_root())
        assert Path(datalake_root()) == Path(DATALAKE_ROOT)

    def test_resolved_paths_are_absolute(self):
        """A relative default is what made the root follow the process cwd."""
        roots = (
            ParquetMarketProvider().store.base_path,
            ParquetBacktestLoader().provider.store.base_path,
        )
        for base in roots:
            assert Path(base).is_absolute(), f"{base!r} is still cwd-relative"

    def test_chdir_does_not_move_the_resolved_root(self, monkeypatch, tmp_path):
        """The actual 2026-09-02 bug: a chdir must not relocate the lake.

        ``tmp_path`` has no ``data/`` child, so the old relative defaults
        would have resolved to a directory that does not exist.
        """
        before = {
            "market_provider": Path(ParquetMarketProvider().store.base_path),
            "backtest_loader": Path(
                ParquetBacktestLoader().provider.store.base_path
            ),
        }

        monkeypatch.chdir(tmp_path)
        assert not (tmp_path / "data").exists()
        assert not (tmp_path / "data" / "lake").exists()

        after = {
            "market_provider": Path(ParquetMarketProvider().store.base_path),
            "backtest_loader": Path(
                ParquetBacktestLoader().provider.store.base_path
            ),
        }
        assert before == after, (
            f"datalake root moved with the process cwd: {before} -> {after}"
        )
        assert after["market_provider"] == Path(datalake_root())
        assert after["backtest_loader"] == Path(datalake_root())

    def test_chdir_leaves_the_ohlcv_partition_root_anchored(
        self, monkeypatch, tmp_path
    ):
        """The store's partition dir stays under the repo lake after a chdir."""
        monkeypatch.chdir(tmp_path)
        store = ParquetMarketProvider().store
        assert Path(store._ohlcv_root) == Path(datalake_root()) / _OHLCV
        assert Path(store._ohlcv_root).is_absolute()

    def test_default_root_is_the_repo_data_dir_not_cwd_data(self, tmp_path):
        """The anchored default is ``<repo>/data``; ``tmp_path/data`` is not."""
        import tradex_market_data.paths as paths

        repo_root = Path(paths.__file__).resolve().parents[3]
        assert Path(datalake_root()) == repo_root / "data"
        assert Path(datalake_root()) != tmp_path / "data"


class TestExplicitPathsStillHonoured:
    """Backwards compatibility: an explicit path must win over the default."""

    def test_market_provider_honours_explicit_base_path(self, tmp_path):
        target = tmp_path / "explicit" / "lake"
        provider = ParquetMarketProvider(base_path=str(target))
        assert Path(provider.store.base_path) == target

    def test_bulk_prefetch_provider_honours_explicit_base_path(self, tmp_path):
        provider = _build_bulk(tmp_path)
        expected = tmp_path / "bulk"
        # No public ``store`` property on the bulk provider; ``_store`` is the
        # only handle it keeps, so assert on the resolved base_path.
        assert Path(provider._store.base_path) == expected

    def test_backtest_loader_honours_explicit_base_path(self, tmp_path):
        target = tmp_path / "explicit" / "lake"
        loader = ParquetBacktestLoader(base_path=str(target))
        assert Path(loader.provider.store.base_path) == target

    def test_catalog_honours_explicit_root(self, tmp_path):
        target = tmp_path / "catalog-root"
        assert Path(DataCatalog(target)._root) == target
        assert Path(DataCatalog(root=target)._root) == target
        assert target.is_dir(), "explicit catalog root must still be created"

    def test_explicit_path_survives_a_chdir(self, monkeypatch, tmp_path):
        """An explicit *relative* path keeps its old cwd-relative semantics.

        Only the default is anchored. A caller that passes ``"data/"`` still
        gets that path, resolved against whatever cwd it is running in — the
        documented behaviour, not a silent rewrite of the caller's argument.
        """
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        provider = ParquetMarketProvider(base_path="data/")
        assert Path(provider.store.base_path).absolute() == elsewhere / "data"

    def test_explicit_store_still_takes_precedence(self, tmp_path):
        """Passing a pre-built store ignores base_path entirely."""
        from tradex_market_data.parquet_storage import ParquetStorage

        store = ParquetStorage(tmp_path / "given")
        provider = ParquetMarketProvider(store=store, base_path="data/")
        assert provider.store is store
        loader = ParquetBacktestLoader(store=store, base_path="data/")
        assert loader.provider.store is store


class TestNoRetiredLiteralsRemain:
    """Guard: the literals must not creep back into these modules."""

    _MODULES = (
        "tradex_market_data.backtest_loader",
        "tradex_market_data.catalog",
        "tradex_market_data.market_provider",
    )

    @pytest.mark.parametrize("modpath", _MODULES)
    def test_module_defines_no_cwd_relative_lake_default(self, modpath):
        """No signature default may be a bare ``data/`` or ``data/lake``.

        Parsed, not text-matched, so a module stays free to *name* the retired
        literal in a comment describing the bug it fixed.
        """
        import importlib

        mod = importlib.import_module(modpath)
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        # Defaults live in ``arguments.defaults``/``kw_defaults`` (they are not
        # attributes of ``ast.arg``), so walk the default nodes directly.
        offenders = [
            ast.unparse(default)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for default in (*node.args.defaults, *node.args.kw_defaults)
            if isinstance(default, ast.Constant)
            and default.value in ("data", "data/", "data/lake")
        ]
        assert not offenders, (
            f"{modpath} still defaults a parameter to {offenders}; resolve the "
            "lake through tradex_market_data.paths.datalake_root() instead"
        )

    def test_path_module_anchors_off_the_filesystem_not_the_cwd(
        self, monkeypatch, tmp_path
    ):
        """``paths`` itself must stay cwd-independent.

        Anchoring is done from ``__file__``; if this ever regressed to
        ``Path.cwd()``, every consumer above would inherit the same bug.
        """
        expected = Path(datalake_root())
        monkeypatch.chdir(tmp_path)
        assert Path(datalake_root()) == expected
        assert Path(datalake_root()).is_absolute()
        assert os.path.abspath(datalake_root()) == str(expected)
