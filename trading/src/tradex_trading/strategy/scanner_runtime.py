"""Scanner runtime — lifecycle wrapper around ScannerEngine."""

from __future__ import annotations

from tradex_domain import ScannerDefinition

from tradex_trading.strategy.scanner import ScannerEngine


class ScannerRuntime:
    """Wraps a ``ScannerEngine`` with runtime lifecycle management.

    Manages a registry of named ``ScannerDefinition`` instances and
    provides convenience methods for running them individually or in bulk.
    """

    def __init__(
        self,
        scanner_engine: ScannerEngine,
        definitions: list | None = None,
    ) -> None:
        self._engine = scanner_engine
        self._definitions: dict[str, ScannerDefinition] = {}
        self._last_results: dict[str, list] = {}
        if definitions:
            for idx, defn in enumerate(definitions):
                name = f"scanner_{idx}"
                self._definitions[name] = defn

    # -- registry ---------------------------------------------------------------

    def add_definition(self, name: str, definition: ScannerDefinition) -> None:
        """Register a scanner definition under *name*."""
        self._definitions[name] = definition

    def remove_definition(self, name: str) -> None:
        """Remove a previously registered definition by *name*."""
        self._definitions.pop(name, None)
        self._last_results.pop(name, None)

    def list_definitions(self) -> list[str]:
        """Return the names of all registered definitions."""
        return list(self._definitions.keys())

    # -- execution --------------------------------------------------------------

    def run_all(self) -> dict[str, list]:
        """Run every registered definition and return ``{name: results}``."""
        results: dict[str, list] = {}
        for name, defn in self._definitions.items():
            results[name] = self._engine.run(defn)
        self._last_results = results
        return results

    def run_one(self, name: str) -> list:
        """Run a single definition by *name* and return its results."""
        defn = self._definitions[name]
        result = self._engine.run(defn)
        self._last_results[name] = result
        return result

    def last_results(self) -> dict[str, list]:
        """Return cached results from the most recent run."""
        return dict(self._last_results)


__all__ = ["ScannerRuntime"]
