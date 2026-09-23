"""Writers must resolve the datalake through the one shared seam.

Phase 4B. ``tradex_trading.datalake.paths.datalake_root()`` anchors the lake to
the repo from ``__file__`` and honours ``$TRADEX_DATALAKE_ROOT``. The interface
*readers* used it; the *writers* did not:

- eight ``trading/scripts/*.py`` hardcoded ``ROOT / "data"`` — ``fill_gaps``,
  ``repair_gaps``, ``topup_gaps``, ``sync_today``, ``backfill_parquet``,
  ``backfill_2025``, ``backtest_datalake``, ``reconcile_bars``;
- ``clean_datalake.py`` defaulted ``--data-root`` to the cwd-relative ``"data/"``;
- ``tradex sync`` in ``interface/cli.py`` hardcoded ``ROOT / "data"`` — guarded
  by ``TestDatalakeRoot.test_no_module_in_interface_defines_the_lake_root`` in
  ``trading/tests/interface/test_datalake_root.py``, which scans the whole
  interface package recursively. The older route-scoped guard there covered only
  ``chart`` and ``stream`` and matched a single exact literal, so it neither
  looked at ``cli.py`` nor recognised a ``ROOT / "data"`` spelling.

Why it matters: with ``$TRADEX_DATALAKE_ROOT`` set, a reader that honours it and
a writer that ignores it silently disagree about which directory is the lake, so
bars land where the server never looks. That is the same empty-chart failure the
2026-09-02 ``trading/data/`` shadow lake produced, reached from the other side.

Grep-level by design, exactly like the interface guard: a second *definition* of
the root is the defect, so the literal itself is what this test forbids.
"""

from __future__ import annotations

import re
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

#: Any second definition of the lake root, repo-anchored or cwd-relative.
_HARDCODED_ROOT = re.compile(
    r'ROOT\s*/\s*["\']data["\']'  # ROOT / "data"
    r'|default\s*=\s*["\']data/?["\']'  # default="data" / default="data/"
)

#: Constructors that take a lake root, and the seam names that must supply it.
_LAKE_CONSTRUCTORS = ("ParquetStorage(", "ParquetBacktestLoader(")
_SEAM_NAMES = ("datalake_root(", "DATALAKE_ROOT")


def _scripts() -> list[Path]:
    return sorted(_SCRIPTS.glob("*.py"))


def test_the_scripts_directory_is_not_empty() -> None:
    """Guard against the scan silently covering nothing."""
    assert _scripts(), f"no scripts found under {_SCRIPTS}"


def test_no_script_defines_the_lake_root_itself() -> None:
    """No script may hardcode a repo- or cwd-relative lake root."""
    offenders: list[str] = []
    for path in _scripts():
        source = path.read_text(encoding="utf-8")
        match = _HARDCODED_ROOT.search(source)
        if match:
            offenders.append(f"{path.name}: {match.group(0)!r}")
    assert not offenders, (
        "these scripts define the datalake root themselves instead of calling "
        "datalake_root(), so $TRADEX_DATALAKE_ROOT cannot move their writes:\n"
        + "\n".join(offenders)
    )


def test_lake_writers_resolve_the_root_through_the_seam() -> None:
    """A script that opens the lake must name the seam that located it."""
    offenders: list[str] = []
    for path in _scripts():
        source = path.read_text(encoding="utf-8")
        if not any(ctor in source for ctor in _LAKE_CONSTRUCTORS):
            continue
        if not any(seam in source for seam in _SEAM_NAMES):
            offenders.append(path.name)
    assert not offenders, (
        "these scripts build a lake handle without the seam, so the root they "
        "open may not be the one the server reads:\n" + "\n".join(offenders)
    )
