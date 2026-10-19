"""REF-14 guardrails: enforce the post-consolidation architecture.

Where ``test_import_boundaries.py`` guards *edges* (which package may import
which), this suite guards the *shape* the 2026-09 consolidation waves landed:

1. no cross-package import of underscore-private symbols (one public seam per
   package boundary);
2. canonical constants live in exactly one home (domain/constants.py, the
   strategy sizer) and the deleted duplicate definitions stay deleted;
3. no duck-typed broker probing in the execution/runtime spine — the
   ``BrokerAdapter`` protocol is the seam;
4. single-writer canaries for the venue-adoption/reconciliation entries and
   the metric-name registry;
5. the new-architecture surfaces exist (protocol members, the broker mixins
   package, the engine entry points, the deleted packages stay gone).

Every rule passes on the landed tree; this file is enforcement, not aspiration.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# The root pytest config runs with ``--import-mode=importlib``, which does not
# put this directory on sys.path; the root conftest.py uses the same explicit
# insertion pattern for the package src trees. Needed to reuse the boundary
# suite's scan helpers instead of forking them.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_import_boundaries import _INTERNAL, _ROOT, _SRC, _py_files  # noqa: E402

#: Extra workspace src tree outside ``_SRC`` (standalone read-only service,
#: now a workspace member, so it is scanned by the same rules).
_DUCKDB_SRC = _ROOT / "services/duckdb-analytics/src"

#: Package-qualifying module names for the private-import scan: the
#: ``tradex_*`` set plus the duckdb service's ``duck_analytics`` root.
_SCAN_INTERNAL = set(_INTERNAL) | {"duck_analytics"}

_ALL_SRC_TREES = [*_SRC.values(), _DUCKDB_SRC]

# ---------------------------------------------------------------------------
# Rule 1 — no cross-package import of underscore-private symbols
# ---------------------------------------------------------------------------

#: file (relative to root) → import names it may still take privately. Kept
#: EMPTY on purpose: the wave-0/1/2 cleanup removed every legit case (the last
#: five were promoted to public spellings — ``sma_seeded_ema``,
#: ``is_valid_bracket``, ``TIMEFRAMES``, ``position_instrument_id``,
#: ``make_order``). A new entry needs a documented reason, not convenience.
_PRIVATE_IMPORT_ALLOWLIST: dict[str, set[str]] = {}


def _importer_package(path: Path, src_root: Path) -> str | None:
    """The workspace package *path* lives in (its ``tradex_*``/service root)."""
    for part in path.relative_to(src_root).parts:
        if part in _SCAN_INTERNAL or part.startswith("tradex_"):
            return part
    return None


def _private_cross_package_imports(tree: ast.AST, path: Path, src_root: Path) -> list[str]:
    """``from X.y import _z`` / ``import X.y._z`` across package boundaries."""
    importer = _importer_package(path, src_root)
    if importer is None:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = node.module.split(".")[0]
            if top in _SCAN_INTERNAL and top != importer:
                found.extend(
                    f"{path.relative_to(_ROOT)}: from {node.module} import {a.name}"
                    for a in node.names
                    if a.name.startswith("_")
                )
        elif isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if (
                    parts[0] in _SCAN_INTERNAL
                    and parts[0] != importer
                    and any(p.startswith("_") for p in parts[1:])
                ):
                    found.append(f"{path.relative_to(_ROOT)}: import {a.name}")
    return found


def test_no_cross_package_private_imports() -> None:
    """A package boundary crossing must never touch an underscore name."""
    violations: list[str] = []
    for src in _ALL_SRC_TREES:
        if not src.is_dir():
            continue
        for path in _py_files(src):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations.extend(_private_cross_package_imports(tree, path, src))
    # The allowlist is empty (see above), so this is a straight fail; the loop
    # only exists so a future, *documented* entry keeps the message accurate.
    unallowed = [
        v for v in violations
        if not _private_import_allowlisted(v)
    ]
    assert not unallowed, (
        "cross-package imports of underscore-private symbols:\n" + "\n".join(unallowed)
    )


def _private_import_allowlisted(hit: str) -> bool:
    """True when *hit* (``relpath: ...``) is covered by the allowlist."""
    rel = hit.split(":", 1)[0]
    allowed = _PRIVATE_IMPORT_ALLOWLIST.get(rel, set())
    return any(symbol in hit for symbol in allowed)


# ---------------------------------------------------------------------------
# Rule 2 — forbidden literal guards (canonical homes only)
# ---------------------------------------------------------------------------

_DOMAIN_CONSTANTS = "domain/src/tradex_domain/constants.py"


def _scan_hits(pattern: re.Pattern[str]) -> list[str]:
    hits: list[str] = []
    for src in _ALL_SRC_TREES:
        if not src.is_dir():
            continue
        for path in _py_files(src):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{path.relative_to(_ROOT)}:{i}: {line.strip()}")
    return hits


def test_sizing_fallback_tick_has_one_home() -> None:
    """``SIZING_FALLBACK_TICK`` is the strategy sizer's own constant — anywhere
    else (including a second copy) is a fork of the sizing vocabulary."""
    token = re.compile(r"\bSIZING_FALLBACK_TICK\b")
    hits = _scan_hits(token)
    bad = [h for h in hits if not h.startswith("strategy/src/tradex_strategy/core/sizing.py")]
    assert not bad, "SIZING_FALLBACK_TICK outside its home:\n" + "\n".join(bad)
    assert hits, "scan found no SIZING_FALLBACK_TICK at all — the scan is blind"


def test_domain_constants_are_assigned_only_in_domain_constants() -> None:
    """``DEFAULT_TICK_SIZE``, the bar-frequency map and the segment maps may
    only be *declared* in ``domain/constants.py``; consumers import them.
    (Usages and imports elsewhere stay legal. The session bounds live in
    ``domain/market_calendar.py`` as ``MARKET_OPEN``/``MARKET_CLOSE``.)"""
    patterns = {
        "DEFAULT_TICK_SIZE": re.compile(r"\bDEFAULT_TICK_SIZE\s*[:=][^=]"),
        "MARKET_OPEN/MARKET_CLOSE": re.compile(r"\b(MARKET_OPEN|MARKET_CLOSE)\s*=\s*time\("),
        "bar-freq map": re.compile(r'"1m"\s*:\s*"1min"'),
        "SEGMENT_CANONICAL maps": re.compile(
            r"\b(SEGMENT_CANONICAL_DHAN|SEGMENT_CANONICAL_UPSTOX)\s*[:=][^=]"
        ),
    }
    for label, pattern in patterns.items():
        if label == "MARKET_OPEN/MARKET_CLOSE":
            # The session bounds live in domain/market_calendar.py, not
            # domain/constants.py.
            bad = [
                h
                for h in _scan_hits(pattern)
                if not h.startswith("domain/src/tradex_domain/market_calendar.py")
            ]
            assert not bad, f"{label} declared outside market_calendar.py:\n" + "\n".join(bad)
            continue
        bad = [h for h in _scan_hits(pattern) if not h.startswith(_DOMAIN_CONSTANTS)]
        assert not bad, f"{label} declared outside {_DOMAIN_CONSTANTS}:\n" + "\n".join(bad)


# ``Decimal("100000")`` spellings that are NOT initial capital: each is bound
# to a different concept and documented here. Anything else outside
# domain/constants.py must import ``DEFAULT_INITIAL_CAPITAL`` instead.
_CAPITAL_LITERAL_ALLOWLIST: dict[str, re.Pattern[str]] = {
    # Max notional per order (a sizing *cap*), not opening capital.
    "strategy/src/tradex_strategy/core/sizing.py": re.compile(
        r"DEFAULT_MAX_ORDER_VALUE\s*[:=]"
    ),
    # Synthetic book-depth marker for paper level fills, not money.
    "runtime/src/tradex_runtime/paper_book_fill_source.py": re.compile(
        r"level_depth\s*[:=]"
    ),
}


def test_initial_capital_literal_has_one_home() -> None:
    """``Decimal("100000")`` may only be *declared* in domain/constants.py."""
    literal = re.compile(r'Decimal\(\s*"100000"\s*\)')
    bad: list[str] = []
    for hit in _scan_hits(literal):
        if hit.startswith(_DOMAIN_CONSTANTS):
            continue
        rel = hit.split(":", 1)[0]
        allowed = _CAPITAL_LITERAL_ALLOWLIST.get(rel)
        if allowed and allowed.search(hit):
            continue
        bad.append(hit)
    assert not bad, (
        'Decimal("100000") outside domain/constants.py (import '
        "DEFAULT_INITIAL_CAPITAL, or extend the documented allowlist):\n"
        + "\n".join(bad)
    )


def test_deleted_time_helpers_stay_deleted() -> None:
    """``candle_seconds``/``_chart_seconds``/``_tf_seconds`` duplicates were
    consolidated into ``domain/timeframe.py`` (``interval_seconds`` /
    ``candle_epoch``); no src tree may redefine them."""
    pattern = re.compile(
        r"\bdef\s+(candle_seconds|_candle_seconds|_chart_seconds|_tf_seconds)\b"
    )
    bad = _scan_hits(pattern)
    assert not bad, "consolidated time helpers redefined:\n" + "\n".join(bad)


# ---------------------------------------------------------------------------
# Rule 3 — no duck-typed broker probing in the execution/runtime spine
# ---------------------------------------------------------------------------


def test_no_duck_typed_broker_probing() -> None:
    """``getattr(self._broker, ...)`` / ``hasattr(self._broker, ...)`` are the
    duck-typing pattern Wave 1 removed from fill_sources and market_feed — the
    ``BrokerAdapter`` protocol is the seam, so attribute probes must not creep
    back into the execution/runtime spine."""
    pattern = re.compile(r"(getattr|hasattr)\(\s*self\._broker\b")
    bad: list[str] = []
    for pkg in ("execution", "runtime"):
        for path in _py_files(_SRC[pkg]):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    bad.append(f"{path.relative_to(_ROOT)}:{i}: {line.strip()}")
    assert not bad, "duck-typed broker probing:\n" + "\n".join(bad)


# ---------------------------------------------------------------------------
# Rule 4 — single-writer canaries
# ---------------------------------------------------------------------------


def _files_defining(name: str, trees: list[Path]) -> list[str]:
    hits: list[str] = []
    for src in trees:
        if not src.is_dir():
            continue
        for path in _py_files(src):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
                    hits.append(str(path.relative_to(_ROOT)))
                    break
    return sorted(set(hits))


def test_venue_adoption_entry_is_single_writer() -> None:
    """``adopt_venue_positions`` is THE adoption entry: one pure function in
    ``reconciliation.py`` plus the one engine method that binds it — and no
    third definition anywhere."""
    owners = _files_defining("adopt_venue_positions", _ALL_SRC_TREES)
    assert owners == [
        "execution/src/tradex_execution/engine.py",
        "execution/src/tradex_execution/reconciliation.py",
    ], f"adopt_venue_positions defined outside the sanctioned pair: {owners}"


def test_reconcile_with_broker_is_private_and_single() -> None:
    """``ExecutionEngine.reconcile`` is the public reconcile entry; the broker
    comparison stays a private ``PositionAccountant`` detail."""
    public = _files_defining("reconcile_with_broker", _ALL_SRC_TREES)
    assert not public, f"public reconcile_with_broker re-appeared in: {public}"
    private = _files_defining("_reconcile_with_broker", _ALL_SRC_TREES)
    assert private == ["execution/src/tradex_execution/position_accountant.py"], (
        f"_reconcile_with_broker drifted: {private}"
    )


#: The REF-13 canonical metric-name literals (wire contracts with Prometheus).
_METRIC_LITERALS = (
    "venue_adoption_adopted_total",
    "venue_adoption_cleared_total",
    "venue_adoption_skipped_total",
    "venue_adoption_refused_total",
    "venue_read_failed_total",
    "venue_adoption_last_adopted",
)
_METRICS_HOME = "observability/src/tradex_observability/metrics.py"


def test_metric_name_literals_assigned_in_one_file() -> None:
    """Each canonical metric-name literal exists in exactly one src file."""
    for literal in _METRIC_LITERALS:
        pattern = re.compile(rf'"{literal}"')
        hits = {h.split(":", 1)[0] for h in _scan_hits(pattern)}
        assert hits == {_METRICS_HOME}, (
            f"metric literal {literal!r} declared in {sorted(hits)}, expected only {_METRICS_HOME}"
        )
    # The registry itself still owns the values.
    from tradex_observability import metrics as m

    assert m.METRIC_ADOPTED == "venue_adoption_adopted_total"
    assert m.METRIC_CLEARED == "venue_adoption_cleared_total"
    assert m.METRIC_SKIPPED == "venue_adoption_skipped_total"
    assert m.METRIC_REFUSED == "venue_adoption_refused_total"
    assert m.METRIC_READ_FAILED == "venue_read_failed_total"
    assert m.METRIC_LAST_ADOPTED == "venue_adoption_last_adopted"


# ---------------------------------------------------------------------------
# Rule 5 — the new-architecture surfaces exist
# ---------------------------------------------------------------------------


def test_broker_protocol_owns_the_wave1_seam() -> None:
    """``BrokerAdapter`` declares every member the Wave-1 consolidation gave it,
    so consumers call the protocol instead of probing adapters."""
    from tradex_domain.protocols import BrokerAdapter

    for member in (
        "owns_position_projection",
        "trading_cache",
        "submit_super_order",
        "cancel_super_order",
        "subscribe_depth_30",
        "unsubscribe_instruments",
        "set_reconnect_hook",
    ):
        assert hasattr(BrokerAdapter, member), (
            f"BrokerAdapter.{member} disappeared from the protocol"
        )


def test_broker_common_mixins_package_exists() -> None:
    """``brokers/common/mixins/`` is the consolidated mixin home (with the
    lockstep guard in brokers/tests/test_broker_contracts.py watching it)."""
    mixins = _ROOT / "brokers/src/tradex_brokers/common/mixins"
    for name in ("__init__.py", "admin.py", "orders.py", "portfolio.py", "marketdata.py"):
        assert (mixins / name).is_file(), f"missing brokers/common/mixins/{name}"


def test_execution_engine_entry_points_exist() -> None:
    """The engine owns the adoption/reconciliation entries the safety surface
    consumes; ``interfaces.safety`` re-exports the adoption entry."""
    from tradex_execution.engine import ExecutionEngine

    assert callable(getattr(ExecutionEngine, "adopt_venue_positions", None))
    assert callable(getattr(ExecutionEngine, "reconcile", None))

    safety = _ROOT / "interfaces/src/tradex_interfaces/safety.py"
    tree = ast.parse(safety.read_text(encoding="utf-8"), filename=str(safety))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(
            "tradex_execution"
        ):
            imported.update(a.name for a in node.names)
    assert "adopt_venue_positions" in imported, (
        "interfaces.safety no longer re-exports the adoption entry"
    )
    assert "METRIC_ADOPTED" in imported, (
        "interfaces.safety no longer re-exports the canonical metric names"
    )


def test_deleted_packages_stay_deleted() -> None:
    """``operations/`` and ``persistence/`` were removed (REF-15); the
    directories must not silently reappear."""
    for gone in ("operations", "persistence"):
        assert not (_ROOT / gone).is_dir(), (
            f"{gone}/ was re-created; it was deleted deliberately (REF-15)"
        )
