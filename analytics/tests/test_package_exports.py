"""The package's re-exports are complete, resolvable, and sorted.

``tradex_analytics/__init__.py`` re-exports its public surface so a caller can
``from tradex_analytics import TradeValidationReport`` rather than reaching into
a submodule. That only stays true while the re-export list is maintained, and a
re-export that quietly omits one name of the module it re-exports is the kind of
inconsistency nobody notices until a consumer imports the submodule path instead
and two spellings of the same thing exist.

So this is mechanical rather than editorial: for every module this package
re-exports, its ``__all__`` must be a subset of the package's, and every name in
the package's ``__all__`` must actually resolve. ``test_research_validity.py``
and ``test_validation_report.py`` were edited by two lanes that each added a
block here without a name either of them owned — which is exactly how
``GATING_FIELDS``, ``REPORT_FIELDS`` and ``EULER_MASCHERONI`` came to be missing
while their modules exported them.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest
import tradex_analytics

#: The modules whose public surface this package re-exports wholesale. A module
#: listed here is one where a name in its ``__all__`` that is absent from the
#: package's is a hole rather than a decision.
REEXPORTED = (
    "tradex_analytics.research_validity",
    "tradex_analytics.validation_report",
)


def _re_exported() -> list[str]:
    """Every module under this package, for the docstring's benefit.

    Kept as a function rather than inlined into a test: the *list* of modules is
    not the assertion. The assertion is per module (above), and it only holds for
    the modules this package re-exports wholesale — ``trade_metrics`` is
    re-exported name by name and has no ``__all__`` of its own, so treating it
    the same way would be asserting a convention it does not follow.
    """
    return sorted(
        f"tradex_analytics.{info.name}"
        for info in pkgutil.iter_modules(tradex_analytics.__path__)
        if not info.ispkg
    )


def test_the_package_re_exports_the_modules_this_file_names() -> None:
    """The list above is not a stale copy of a directory listing."""
    modules = set(_re_exported())
    for name in REEXPORTED:
        assert name in modules, f"{name} is gone or renamed; update REEXPORTED"


def test_every_name_in_the_package_all_resolves() -> None:
    """An ``__all__`` entry that does not exist is an ``AttributeError`` in prod."""
    missing = [name for name in tradex_analytics.__all__ if not hasattr(tradex_analytics, name)]
    assert missing == [], f"tradex_analytics.__all__ names nothing: {missing}"


@pytest.mark.parametrize("module_name", REEXPORTED)
def test_every_public_name_of_a_re_exported_module_is_re_exported(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module.__all__, f"{module_name} exports nothing to re-export"
    missing = sorted(set(module.__all__) - set(tradex_analytics.__all__))
    assert missing == [], (
        f"{module_name} exports {missing} but tradex_analytics does not re-export "
        "them. Add them to the import block and to __all__, or say in the module "
        "why they are deliberately package-private."
    )


def test_every_re_exported_name_is_imported_and_every_import_is_listed() -> None:
    """The import block and ``__all__`` are the same set, with no duplicates.

    Order is deliberately not asserted: ``__all__`` is grouped constants →
    classes → functions and Ruff owns that convention, so a sortedness test here
    would be a second, weaker opinion about a file several lanes edit. What is a
    real defect is a name imported and not listed (so ``import *`` drops it) or
    listed and not imported (so ``hasattr`` is the only thing that works), and
    both are checked by comparing the two sets.
    """
    import ast
    from pathlib import Path

    source = Path(tradex_analytics.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.asname or alias.name for alias in node.names)
    listed = tradex_analytics.__all__

    assert len(listed) == len(set(listed)), "tradex_analytics.__all__ has a duplicate"
    assert sorted(imported) == sorted(listed), (
        "the import block and __all__ disagree: "
        f"imported but unlisted {sorted(imported - set(listed))}, "
        f"listed but unimported {sorted(set(listed) - imported)}"
    )


def test_the_validation_report_exports_are_the_ones_the_bar_needs() -> None:
    """The six thresholds and the bar itself, reachable from the package.

    AC-13's promotion bar is a thing a route, a script and a report all read. If
    a consumer has to import ``tradex_analytics.validation_report`` to get at
    ``PROMOTION_CRITERIA`` then the package's re-export is decoration, and the
    next lane to add a field will add a third spelling.
    """
    from tradex_analytics.validation_report import GATING_FIELDS, PROMOTION_CRITERIA

    for name in (
        "PROMOTION_CRITERIA",
        "GATING_FIELDS",
        "REPORT_FIELDS",
        "COST_STRESS_MULTIPLIER",
        "MIN_OOS_SHARPE",
        "MIN_OOS_ROUND_TRIPS",
        "MIN_OOS_SPAN_DAYS",
        "MIN_DEFATED_SHARPE",
        "MIN_PARAM_STABILITY_SHARE",
        "TradeValidationReport",
        "ValidationReportError",
        "round_trips_from_fills",
        "count_round_trips",
    ):
        assert name in tradex_analytics.__all__, name
    assert tradex_analytics.GATING_FIELDS == GATING_FIELDS
    assert tradex_analytics.PROMOTION_CRITERIA is PROMOTION_CRITERIA
    assert len(PROMOTION_CRITERIA) == 6
    assert len(GATING_FIELDS) == 6
