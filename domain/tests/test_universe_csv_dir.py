"""Pin the package-anchored CSV lookup after universe moved into domain.

``load_universe`` derives ``_DEFAULT_CSV_DIR`` from ``parents[3]``, which only
holds because this file sits at ``<repo>/domain/src/tradex_domain/universe.py``
— package → src → package-dir → repo. A future move that changes that depth
would silently point the lookup at a non-existent directory and break every
caller with a bare ``FileNotFoundError``, so the resolved path and a known
symbol are asserted here rather than trusted to a comment.
"""

from __future__ import annotations

import os
from pathlib import Path

from tradex_domain import universe as _universe
from tradex_domain.universe import _DEFAULT_CSV_DIR, available_universes, load_universe

_MODULE_FILE = Path(_universe.__file__).resolve()


def test_default_csv_dir_is_absolute_and_cwd_independent() -> None:
    """The default must be absolute, and derived from the module, not the cwd."""
    # parents[3] is only correct for a module at <repo>/<pkg>/src/<dist>/x.py.
    # This file is under domain/tests/, so anchor on the module's own path.
    assert _MODULE_FILE.name == "universe.py"
    assert _MODULE_FILE.parent.name == "tradex_domain"
    assert _MODULE_FILE.parent.parent.name == "src"
    assert _MODULE_FILE.parent.parent.parent.name == "domain"

    assert _DEFAULT_CSV_DIR.is_absolute()
    assert _DEFAULT_CSV_DIR == _MODULE_FILE.parents[3] / "Dependencies"
    assert _DEFAULT_CSV_DIR.is_dir()
    assert _DEFAULT_CSV_DIR == Path(_DEFAULT_CSV_DIR).resolve()


def test_default_csv_dir_survives_a_chdir(tmp_path: Path) -> None:
    """``os.chdir`` must not move the universe lookup."""
    before = available_universes()
    cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        assert available_universes() == before
        assert load_universe("nifty50")
    finally:
        os.chdir(cwd)


def test_load_universe_returns_known_instruments() -> None:
    """RELIANCE is in the Nifty 500 list — the row -> Equity mapping is intact."""
    instruments = load_universe("nifty500")
    symbols = [i.symbol for i in instruments]

    assert "RELIANCE" in symbols
    assert len(symbols) == len(set(symbols)), "duplicate symbols in the parsed universe"

    reliance = next(i for i in instruments if i.symbol == "RELIANCE")
    assert reliance.meta.isin, "CSV ISIN should be attached for connect-time resolve"
    assert reliance.meta.extra.get("series") in {"EQ", "BE"}


def test_nifty50_subset_of_nifty500() -> None:
    """Sanity check that the default universe still parses from the same CSVs."""
    nifty50 = {i.symbol for i in load_universe("nifty50")}
    nifty500 = {i.symbol for i in load_universe("nifty500")}
    assert nifty50, "nifty50 universe should not be empty"
    assert nifty50 <= nifty500
