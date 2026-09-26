"""Enforce the v4 package dependency boundary (F10).

Post-extraction workspace graph (strangler). ``tradex_trading`` is the
composition root and may import extracted packages via compatibility shims.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

_SRC = {
    "domain": _ROOT / "domain/src",
    "brokers": _ROOT / "brokers/src",
    "config": _ROOT / "config/src",
    "trading": _ROOT / "trading/src",
    "research": _ROOT / "research/src",
    "operations": _ROOT / "operations/src",
    "observability": _ROOT / "observability/src",
    "analytics": _ROOT / "analytics/src",
    "reactive": _ROOT / "reactive/src",
    "execution": _ROOT / "execution/src",
    "strategy": _ROOT / "strategy/src",
    "replay": _ROOT / "replay/src",
    "application": _ROOT / "application/src",
    "market_data": _ROOT / "market_data/src",
    "interfaces": _ROOT / "interfaces/src",
    "runtime": _ROOT / "runtime/src",
    "persistence": _ROOT / "persistence/src",
}

_INTERNAL = {
    "tradex_domain",
    "tradex_brokers",
    "tradex_config",
    "tradex_trading",
    "tradex_research",
    "tradex_operations",
    "tradex_observability",
    "tradex_analytics",
    "tradex_reactive",
    "tradex_execution",
    "tradex_strategy",
    "tradex_replay",
    "tradex_application",
    "tradex_market_data",
    "tradex_interfaces",
    "tradex_runtime",
    "tradex_persistence",
}

# package key → allowed internal imports (self always allowed separately)
_ALLOWED: dict[str, set[str]] = {
    "domain": set(),
    "brokers": {"tradex_domain"},
    "config": {"tradex_domain"},
    "research": {"tradex_domain"},
    "operations": set(),
    "observability": set(),
    "analytics": {"tradex_domain"},
    "reactive": {"tradex_domain"},
    "execution": {"tradex_domain", "tradex_reactive", "tradex_observability"},
    "strategy": {"tradex_domain", "tradex_analytics", "tradex_market_data"},
    "replay": {
        "tradex_domain",
        "tradex_execution",
        "tradex_strategy",
        "tradex_analytics",
        "tradex_reactive",
        "tradex_market_data",
    },
    "application": {"tradex_domain", "tradex_execution"},
    "market_data": {"tradex_domain", "tradex_brokers", "tradex_replay", "tradex_runtime"},
    "interfaces": {
        "tradex_domain",
        "tradex_brokers",
        "tradex_config",
        "tradex_execution",
        "tradex_application",
        "tradex_strategy",
        "tradex_replay",
        "tradex_market_data",
        "tradex_analytics",
        "tradex_reactive",
        "tradex_runtime",
    },
    "runtime": {
        "tradex_domain",
        "tradex_brokers",
        "tradex_config",
        "tradex_execution",
        "tradex_strategy",
        "tradex_market_data",
        "tradex_reactive",
        "tradex_observability",
        "tradex_trading",  # sdk session only (config → tradex_config)
    },
    "persistence": {"tradex_execution"},
    # Composition root + shims may import every extracted package.
    "trading": set(_INTERNAL) - {"tradex_trading"},
}

# file → extra allowed modules (for the documented importlib exception)
_EXCEPTIONS = {
    "domain/src/tradex_domain/serialization.py": {"importlib"},
}


def _imported_modules(tree: ast.AST) -> set[str]:
    """Collect top-level internal module names imported by *tree*.

    Type-checking-only imports are excluded: a name bound inside
    ``if TYPE_CHECKING:`` never executes, so it cannot participate in a real
    import cycle. ``ast.walk`` descends into that block unconditionally,
    which reported ``runtime -> trading -> runtime`` as a cycle even after
    ``tradex_trading/__init__.py`` stopped importing ``tradex_runtime`` at
    runtime (its re-exports are PEP 562 lazy). Counting those edges makes the
    graph disagree with the interpreter, and a gate that cries wolf is a gate
    that gets deleted.
    """
    modules: set[str] = set()

    # Subtrees guarded by TYPE_CHECKING (or `if False`) never run.
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking_guard(node.test):
            for child in ast.walk(node):
                guarded.add(id(child))

    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
    return modules


def _is_type_checking_guard(test: ast.expr) -> bool:
    """True when *test* is the TYPE_CHECKING (or constant-false) sentinel."""
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    # `if False:` / `if 0:` are the other never-executed form.
    if isinstance(test, ast.Constant):
        return not test.value
    return False


def _py_files(src: Path) -> list[Path]:
    return sorted(p for p in src.rglob("*.py") if "__pycache__" not in p.parts)


def test_domain_never_imports_brokers_or_trading() -> None:
    forbidden = _INTERNAL - _ALLOWED["domain"]
    for path in _py_files(_SRC["domain"]):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_modules(tree)
        imported -= {"tradex_domain"}  # self-imports are legitimate
        # serialization.py may importlib for tradex_domain markers only
        if str(path.relative_to(_ROOT)) in _EXCEPTIONS:
            imported -= {"importlib"}
        violations = imported & forbidden
        assert not violations, f"{path.relative_to(_ROOT)} imports {sorted(violations)}"


def test_brokers_never_imports_trading() -> None:
    forbidden = _INTERNAL - _ALLOWED["brokers"]
    for path in _py_files(_SRC["brokers"]):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_modules(tree)
        imported -= {"tradex_brokers"}  # self-imports are legitimate
        violations = imported & forbidden
        assert not violations, f"{path.relative_to(_ROOT)} imports {sorted(violations)}"


def test_trading_only_imports_allowed_internals() -> None:
    forbidden = _INTERNAL - _ALLOWED["trading"]
    for path in _py_files(_SRC["trading"]):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_modules(tree)
        imported -= {"tradex_trading"}
        violations = imported & forbidden
        assert not violations, f"{path.relative_to(_ROOT)} imports {sorted(violations)}"


def test_extracted_packages_respect_allowed_internals() -> None:
    """Each extracted package only imports its declared internal dependencies."""
    for pkg, allowed in _ALLOWED.items():
        if pkg in {"domain", "brokers", "trading", "research", "operations", "config"}:
            continue
        if pkg not in _SRC:
            continue
        forbidden = _INTERNAL - allowed
        self_name = {
            "observability": "tradex_observability",
            "analytics": "tradex_analytics",
            "reactive": "tradex_reactive",
            "execution": "tradex_execution",
            "strategy": "tradex_strategy",
            "replay": "tradex_replay",
            "application": "tradex_application",
            "market_data": "tradex_market_data",
            "interfaces": "tradex_interfaces",
            "runtime": "tradex_runtime",
            "persistence": "tradex_persistence",
        }[pkg]
        for path in _py_files(_SRC[pkg]):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported = _imported_modules(tree)
            imported -= {self_name}
            violations = imported & forbidden
            assert not violations, f"{path.relative_to(_ROOT)} imports {sorted(violations)}"


def test_all_workspace_packages_present() -> None:
    """Guard against the test silently scanning nothing."""
    for name, src in _SRC.items():
        assert _py_files(src), f"no .py files found under {name}/src"


def test_config_package_never_imports_trading_brokers_or_ops() -> None:
    forbidden = _INTERNAL - _ALLOWED["config"]
    for path in _py_files(_SRC["config"]):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_modules(tree)
        imported -= {"tradex_config"}
        violations = imported & forbidden
        assert not violations, f"{path.relative_to(_ROOT)} imports {sorted(violations)}"


def test_config_shim_only_imports_tradex_config() -> None:
    """Compatibility shims may re-export tradex_config; nothing else."""
    shim_src = _ROOT / "trading/src/tradex_trading/config"
    for path in _py_files(shim_src):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_modules(tree)
        extras = (imported & _INTERNAL) - {"tradex_trading", "tradex_config"}
        assert not extras, (
            f"{path.relative_to(_ROOT)} imports {sorted(extras)}; "
            "config shim must only re-export tradex_config"
        )


def test_interfaces_does_not_import_tradex_trading() -> None:
    """interfaces must use tradex_config / tradex_runtime façades, not composition root."""
    prefix = "tradex_trading"
    offenders: list[str] = []
    for path in _py_files(_SRC["interfaces"]):
        dotted = _imported_dotted(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        hits = sorted(
            m for m in dotted if m == prefix or m.startswith(f"{prefix}.")
        )
        if hits:
            offenders.append(f"{path.relative_to(_ROOT)}: {hits}")
    assert not offenders, (
        "interfaces imports tradex_trading — use tradex_config, tradex_runtime, "
        "tradex_strategy instead:\n" + "\n".join(offenders)
    )


def test_runtime_does_not_import_tradex_trading_config() -> None:
    """Runtime must use tradex_config, not the tradex_trading.config shim."""
    prefix = "tradex_trading.config"
    offenders: list[str] = []
    for path in _py_files(_SRC["runtime"]):
        dotted = _imported_dotted(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        hits = sorted(
            m for m in dotted if m == prefix or m.startswith(f"{prefix}.")
        )
        if hits:
            offenders.append(f"{path.relative_to(_ROOT)}: {hits}")
    assert not offenders, (
        "runtime imports tradex_trading.config — use tradex_config instead:\n"
        + "\n".join(offenders)
    )


_RUNTIME_TRADEX_TRADING_ALLOWLIST = frozenset({
    "tradex_trading.sdk.session",
    "tradex_trading.sdk.live_fill_bridge",
})


def test_runtime_tradex_trading_imports_are_sdk_only() -> None:
    """Runtime may import only sdk façade modules until TradingSession is extracted."""
    prefix = "tradex_trading"
    offenders: list[str] = []
    for path in _py_files(_SRC["runtime"]):
        dotted = _imported_dotted(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        for mod in sorted(dotted):
            if mod != prefix and not mod.startswith(f"{prefix}."):
                continue
            if mod in _RUNTIME_TRADEX_TRADING_ALLOWLIST:
                continue
            offenders.append(f"{path.relative_to(_ROOT)}: {mod}")
    assert not offenders, (
        "runtime may only import tradex_trading.sdk.session / "
        "tradex_trading.sdk.live_fill_bridge until session extraction:\n"
        + "\n".join(offenders)
    )


def test_research_package_never_imports_trading_brokers_or_ops() -> None:
    forbidden = _INTERNAL - _ALLOWED["research"]
    for path in _py_files(_SRC["research"]):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_modules(tree)
        imported -= {"tradex_research"}
        violations = imported & forbidden
        assert not violations, f"{path.relative_to(_ROOT)} imports {sorted(violations)}"


def test_operations_package_never_imports_internal_packages() -> None:
    forbidden = _INTERNAL - _ALLOWED["operations"]
    for path in _py_files(_SRC["operations"]):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_modules(tree)
        imported -= {"tradex_operations"}
        violations = imported & forbidden
        assert not violations, f"{path.relative_to(_ROOT)} imports {sorted(violations)}"


# ---------------------------------------------------------------------------
# Single-authority guard for the active execution spine
# ---------------------------------------------------------------------------

# The platform's production-readiness argument is: one order authority and one
# money path. If a new module starts claiming order state, fill identity, mark
# freshness, or unrealized PnL as authoritative, this test should fail instead
# of letting the claim silently exist alongside the active spine.
#
# This is a small structural guard, not a full architectural lint. It watches the
# named authority symbols that the 2026-09-04 reviews treated as the live spine:
#   - execution: ExecutionEngine / PositionManager / TradingCache
#   - money path: position_math.apply_fill
#
# If the repo later adopts a new spine, this guard must be revisited explicitly.

_SINGLE_AUTHORITY_SYMBOLS: dict[str, set[str]] = {
    "execution/src/tradex_execution/engine.py": {"ExecutionEngine"},
    # Position authority moved to position_accountant.py (the 13-line
    # position_manager.py is now an alias shim, which this same test skips as
    # a non-authority "Compatibility shim" file, so the old entry could never
    # be satisfied). Same shape as the apply_fill move below: point the guard
    # at the file that now owns the behavior.
    "execution/src/tradex_execution/position_accountant.py": {"PositionAccountant"},
    # M8 fix: apply_fill canonical authority moved to domain/position_math.py.
    # trading/position_math.py now re-exports from domain.
    "domain/src/tradex_domain/position_math.py": {"apply_fill"},
    "execution/src/tradex_execution/trading_cache.py": {"TradingCache"},
}


def _top_level_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
    return names


def test_single_execution_authority_in_active_spine() -> None:
    """The active spine keeps order + money authority in a small, named set.

    If a second module in the execution tree starts defining the same
    authoritative symbols, that is a duplication signal and the build must
    fail so the team can decide which authority owns the concern.
    """
    trading_src = _ROOT / "trading/src"
    domain_src = _ROOT / "domain/src"
    execution_src = _ROOT / "execution/src"
    executed: dict[str, set[str]] = {}
    for src_root in (trading_src, domain_src, execution_src):
        for path in sorted(src_root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            # Skip thin compatibility shims — they are not authority owners.
            text = path.read_text(encoding="utf-8")
            if "Compatibility shim" in text:
                continue
            rel = str(path.relative_to(_ROOT))
            names = _top_level_names(path)
            if names:
                executed[rel] = names

    assert executed, "trading/src + domain/src + execution/src appear to contain no Python files"

    for path_text, expected in _SINGLE_AUTHORITY_SYMBOLS.items():
        owning = executed.get(path_text)
        assert owning is not None, f"expected authority file missing: {path_text}"
        missing = expected - owning
        assert not missing, (
            f"{path_text} no longer owns the expected authority symbols: {sorted(missing)}. "
            "If this is intentional, update the single-authority guard explicitly."
        )

    # One money path: apply_fill should not be redefined in another execution
    # module as an authoritative fill applier. We do not forbid helpers named
    # apply_fill elsewhere; we forbid the authoritative one from being duplicated
    # in the active execution tree.
    authoritative_owner = (
        "domain/src/tradex_domain/position_math.py"
    )
    owners_of_apply_fill = sorted(
        rel for rel, names in executed.items()
        if "apply_fill" in names and rel != authoritative_owner
    )
    assert not owners_of_apply_fill, (
        "apply_fill is defined in more than one execution module. "
        "Money path duplication detected:\n" + "\n".join(owners_of_apply_fill)
    )


# ---------------------------------------------------------------------------
# Retired-stack guard: ``tradex_trading.events`` must not come back
# ---------------------------------------------------------------------------

# Ported from ``trading/scripts/probe_review_fixes.py``, deleted 2026-09-22.
#
# The legacy ``tradex_trading.events`` OMS stack (~2.7k LoC: its own FSM, risk
# engine, event store, recovery, kill switch with *different* semantics) was
# retired and the package was removed. ``probe_review_fixes.py`` was a
# manually-run diagnostic asserting that nothing in production or scripts
# re-imports it. A manual script only helps if someone remembers to run it, so
# the check now lives here, where the suite runs it on every build.
#
# Why the layer tests above do not already cover this: they compare *top-level*
# module names (``alias.name.split(".")[0]``), so ``tradex_trading.events``
# collapses to the legitimate self-import ``tradex_trading`` and passes.

_RETIRED_PREFIX = "tradex_trading.events"

_RETIRED_SCAN_ROOTS = (
    _ROOT / "trading/src",
    _ROOT / "trading/scripts",
)


def _imported_dotted(tree: ast.AST) -> set[str]:
    """Collect full dotted module names imported by *tree*."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                modules.add(node.module)
    return modules


def test_retired_events_stack_is_not_imported() -> None:
    """Nothing in trading production code or scripts imports the retired stack.

    The package is gone, so today an import would raise ``ImportError`` at
    runtime anyway. The point of stating it as a test is that re-creating the
    stack — or sneaking a reference back in behind a lazy import — fails the
    build instead of silently reintroducing a second order-management universe.
    """
    offenders: list[str] = []
    for root in _RETIRED_SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            imported = _imported_dotted(
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            )
            hits = sorted(
                name
                for name in imported
                if name == _RETIRED_PREFIX or name.startswith(f"{_RETIRED_PREFIX}.")
            )
            if hits:
                offenders.append(f"{path.relative_to(_ROOT)}: {hits}")

    assert not offenders, (
        "the retired ``tradex_trading.events`` stack is imported again; it was "
        "deleted deliberately (second OMS universe) and must not be revived:\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# Wave C6 / extraction — research + execution boundary gates
# ---------------------------------------------------------------------------
#
# After Wave-1 strangler extraction, the canonical research tree is
# ``research/src/tradex_research``. The trading path is a compatibility shim
# that may import ``tradex_research`` only.
#
# Boundaries:
#   1. research package must not import fastapi / tradex_brokers / sqlite3
#   2. interface/routes must not import research (shim or package)
#   3. execution must not import fastapi

_RESEARCH_PACKAGE_SRC = _ROOT / "research/src/tradex_research"
_RESEARCH_SHIM_SRC = _ROOT / "trading/src/tradex_trading/research"
_ROUTES_SRC = _ROOT / "interfaces/src/tradex_interfaces/routes"
_EXECUTION_SRC = _ROOT / "execution/src/tradex_execution"

_RESEARCH_FORBIDDEN_TOPLEVEL = {"fastapi", "tradex_brokers", "sqlite3"}
_EXECUTION_FORBIDDEN_TOPLEVEL = {"fastapi"}
_RESEARCH_DOTTED_PREFIXES = (
    "tradex_trading.research",
    "tradex_research",
)


def test_research_does_not_import_fastapi_brokers_or_sqlite() -> None:
    """Canonical research package must stay offline and store-free."""
    for path in _py_files(_RESEARCH_PACKAGE_SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_modules(tree)
        violations = imported & _RESEARCH_FORBIDDEN_TOPLEVEL
        assert not violations, (
            f"{path.relative_to(_ROOT)} imports {sorted(violations)}; "
            "research must not depend on fastapi, tradex_brokers, or sqlite3"
        )


def test_research_shim_only_imports_tradex_research() -> None:
    """Compatibility shims may re-export tradex_research; nothing else."""
    for path in _py_files(_RESEARCH_SHIM_SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_modules(tree)
        # Self-package and the extracted package are the only internals allowed.
        extras = (imported & _INTERNAL) - {"tradex_trading", "tradex_research"}
        assert not extras, (
            f"{path.relative_to(_ROOT)} imports {sorted(extras)}; "
            "research shim must only re-export tradex_research"
        )
        violations = imported & _RESEARCH_FORBIDDEN_TOPLEVEL
        assert not violations, (
            f"{path.relative_to(_ROOT)} imports {sorted(violations)}"
        )


def test_interface_routes_do_not_import_research() -> None:
    """interface/routes/* must not import research (package or shim)."""
    offenders: list[str] = []
    for path in _py_files(_ROUTES_SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        dotted = _imported_dotted(tree)
        hits = sorted(
            m
            for m in dotted
            for prefix in _RESEARCH_DOTTED_PREFIXES
            if m == prefix or m.startswith(f"{prefix}.")
        )
        if hits:
            offenders.append(f"{path.relative_to(_ROOT)}: {hits}")
    assert not offenders, (
        "interface/routes imports research — coupling prevents package isolation:\n"
        + "\n".join(offenders)
    )


def test_execution_does_not_import_fastapi() -> None:
    """execution/* must not import the web framework.

    The execution engine is a pure domain-level concern. FastAPI belongs
    exclusively in the interface layer. Any coupling here breaks the
    interface-agnostic guarantee, prevents future deployment splits (e.g.
    running the engine headless), and pulls a heavy ASGI dependency into a
    module that has no business serving HTTP.
    """
    for path in _py_files(_EXECUTION_SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_modules(tree)
        violations = imported & _EXECUTION_FORBIDDEN_TOPLEVEL
        assert not violations, (
            f"{path.relative_to(_ROOT)} imports {sorted(violations)}; "
            "execution must not depend on fastapi"
        )


def test_wave_c6_scan_roots_are_not_empty() -> None:
    """Guard the extraction scans against silently passing on missing directories."""
    assert _py_files(_RESEARCH_PACKAGE_SRC), (
        f"no .py files found under {_RESEARCH_PACKAGE_SRC.relative_to(_ROOT)}; "
        "the research package boundary scan is not looking at anything"
    )
    assert _py_files(_RESEARCH_SHIM_SRC), (
        f"no .py files found under {_RESEARCH_SHIM_SRC.relative_to(_ROOT)}; "
        "the research shim boundary scan is not looking at anything"
    )
    assert _py_files(_ROUTES_SRC), (
        f"no .py files found under {_ROUTES_SRC.relative_to(_ROOT)}; "
        "the routes boundary scan is not looking at anything"
    )
    assert _py_files(_EXECUTION_SRC), (
        f"no .py files found under {_EXECUTION_SRC.relative_to(_ROOT)}; "
        "the execution boundary scan is not looking at anything"
    )


# ---------------------------------------------------------------------------

def test_retired_events_stack_is_actually_gone() -> None:
    """Guard the scan above against silently passing on an empty file set."""
    assert not (_ROOT / "trading/src/tradex_trading/events").exists(), (
        "tradex_trading/events was re-created; if that is intentional the "
        "retirement decision must be revisited explicitly."
    )
    for root in _RETIRED_SCAN_ROOTS:
        assert _py_files(root), (
            f"no .py files found under {root.relative_to(_ROOT)}; the retired-stack "
            "scan is not looking at anything."
        )


# ---------------------------------------------------------------------------
# Package-level import-cycle guard
# ---------------------------------------------------------------------------
#
# Every test above checks *edges*: which package may import which. None of them
# check *cycles*. ``_ALLOWED`` admits ``market_data -> replay`` and
# ``replay -> market_data`` in the same breath, because each edge is legal in
# isolation, so a real import cycle could be introduced and the boundary suite
# would stay green. Cycles matter at package level for a reason the edge rules
# cannot express: a partially initialised import cycle leaves module-level
# state half-built, and the failure surfaces as a circular-import error at an
# arbitrary import site rather than as a layering error.
#
# The graph is package-level, not module-level. Module-level cycles are
# widespread and normal here (a package importing a submodule that imports back
# is a common Python idiom); package-level cycles are the coarse, actionable
# signal. Self-edges are excluded, since intra-package imports are not a
# layering concern.

#: module name → package key, so ``tradex_market_data`` maps to ``market_data``.
_MODULE_TO_PKG = {name: name[len("tradex_"):] for name in _INTERNAL}

#: Known cycles, canonicalised so each ring starts at its lexicographically
#: smallest member. A cycle is covered when *every* edge of the listed ring is
#: an edge of the graph, so a longer ring would also cover the shorter ones.
#:
#: Empty as of 2026-09-26. The allowlist existed to pin the seam where market
#: data depended on the consumers that need it, which depended back on market
#: data for instrument metadata. Both rings are now closed:
#:
#:   * ``market_data -> replay -> strategy -> market_data`` — ``load_universe``
#:     / ``available_universes`` are pure domain functions and moved from
#:     ``tradex_market_data.universe`` to ``tradex_domain.universe``, so
#:     ``tradex_strategy`` no longer imports the datalake package for them.
#:     ``tradex_market_data.universe`` survives only as a re-export shim.
#:   * ``market_data -> replay -> market_data`` — ``backtest_loader`` no longer
#:     imports the replay driver; the run-with-loader helper is replay-side.
#:
#: ``market_data -> runtime -> strategy -> market_data`` was already gone.
#:
#: Adding a cycle back to this dict is a decision to re-accept that layering,
#: not a way to silence a regression: ``test_no_unknown_import_cycles`` fails
#: the build on any ring, and ``test_known_import_cycles_all_still_exist``
#: fails the moment an entry stops describing real code.
KNOWN_CYCLES: dict[tuple[str, ...], str] = {}


def _package_import_graph() -> dict[str, set[str]]:
    """Map each package to the set of other packages it imports."""
    graph: dict[str, set[str]] = {pkg: set() for pkg in _SRC}
    for pkg, src in _SRC.items():
        for path in _py_files(src):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for module in _imported_modules(tree):
                dep = _MODULE_TO_PKG.get(module)
                # Self-imports are intra-package, not a layering concern.
                if dep is not None and dep != pkg:
                    graph[pkg].add(dep)
    return graph


def _cycle_contains(graph: dict[str, set[str]], ring: tuple[str, ...]) -> bool:
    """True when every edge of the closed *ring* exists in *graph*."""
    return all(
        nxt in graph[cur] for cur, nxt in zip(ring, ring[1:] + ring[:1], strict=True)
    )


def _find_import_cycles(graph: dict[str, set[str]]) -> list[tuple[str, ...]]:
    """Every simple cycle of length >= 2 in *graph*, each rotated to start at
    its smallest member so the same ring is always reported the same way.

    This deliberately walks every simple path rather than memoising on the
    visited set. The cycles reachable from a given set of nodes depend on which
    node the walk started from, so a set-based memo can silently *under*-report
    cycles — and a guard that misses a new cycle is worse than no guard. The
    graph is 17 nodes and ~50 edges, so the simple paths are cheap to enumerate.
    """
    found: set[tuple[str, ...]] = set()

    def visit(start: str, path: list[str]) -> None:
        for nxt in sorted(graph[path[-1]]):
            if nxt == start:
                ring = tuple(path)
                pivot = ring.index(min(ring))
                found.add(ring[pivot:] + ring[:pivot])
            elif nxt not in path:
                visit(start, path + [nxt])

    for start in sorted(graph):
        visit(start, [start])
    return sorted(found)


def _cycles_covering(graph: dict[str, set[str]], cycle: tuple[str, ...]) -> set[tuple[str, ...]]:
    """The ``KNOWN_CYCLES`` rings that cover *cycle*.

    Coverage is decided by the *ring's* edges, not the cycle's: a known ring
    covers a cycle when the graph still contains every edge of that ring and
    the cycle visits only nodes of it. The edge test matters because the
    longer rings (``market_data -> replay -> strategy -> market_data``) also
    cover the shorter ``market_data -> replay -> market_data``, so the allowlist
    stays four entries instead of one per traversal. The node test matters
    because a graph can hold several disjoint cycles at once, and one known ring
    must not excuse a cycle through unrelated packages.
    """
    members = set(cycle)
    return {
        ring
        for ring in KNOWN_CYCLES
        if _cycle_contains(graph, ring) and members <= set(ring)
    }


def test_no_unknown_import_cycles_between_packages() -> None:
    """No package may participate in a cycle absent from ``KNOWN_CYCLES``.

    A new cycle fails the build. A cycle already recorded in ``KNOWN_CYCLES``
    passes, so this pins the current shape without demanding a refactor of the
    market_data / replay / runtime seam.
    """
    graph = _package_import_graph()
    offending = [
        " -> ".join(cycle) + " -> " + cycle[0]
        for cycle in _find_import_cycles(graph)
        if not _cycles_covering(graph, cycle)
    ]
    assert not offending, (
        "new package import cycle(s) not listed in KNOWN_CYCLES:\n"
        + "\n".join(offending)
        + "\n\nBreak the cycle, or record it in KNOWN_CYCLES with why it exists "
        "and what would break it."
    )


def test_known_import_cycles_all_still_exist() -> None:
    """Every ``KNOWN_CYCLES`` entry must still describe a real cycle.

    Without this, fixing a cycle (the whole point) would silently leave a stale
    entry behind and ``KNOWN_CYCLES`` would drift into describing code that no
    longer exists.
    """
    graph = _package_import_graph()
    found = _find_import_cycles(graph)
    stale = sorted(
        " -> ".join(ring) + " -> " + ring[0]
        for ring in KNOWN_CYCLES
        if not _cycle_contains(graph, ring)
        or not any(_cycle_contains(graph, cycle) for cycle in found)
    )
    assert not stale, (
        "KNOWN_CYCLES lists cycles that no longer exist; drop those entries and "
        "re-run the boundary suite:\n" + "\n".join(stale)
    )


def test_import_cycle_scan_roots_are_not_empty() -> None:
    """Guard the cycle scan against passing vacuously.

    Distinct from ``test_all_workspace_packages_present``: that only checks each
    ``src`` has files, while this asserts the graph itself has real
    cross-package edges, so a broken scan yielding an empty graph would report
    "no cycles" instead of failing.
    """
    graph = _package_import_graph()
    edges = sum(len(deps) for deps in graph.values())
    assert edges > 0, (
        "the package import graph has no cross-package edges; the cycle scan "
        "is not looking at anything."
    )

