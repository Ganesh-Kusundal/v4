"""ScannerService — scanner execution over a bound engine."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.strategy import ScannerDefinition, ScannerResult


class ScannerService:
    """Scanner execution over a bound engine (D-15); no engine => loud error.

    Optionally carries the auto-discovered extension scanner definitions so
    ``boot()`` can bind them into the session (the ScannerRuntime replacement
    surface: ``discovered`` / ``run_all``).
    """

    def __init__(
        self,
        engine: Any = None,
        definitions: Sequence[ScannerDefinition] | None = None,
    ) -> None:
        self._engine = engine
        self._definitions = tuple(definitions or ())

    @property
    def discovered(self) -> tuple[ScannerDefinition, ...]:
        """Auto-discovered extension scanner definitions bound at boot."""
        return self._definitions

    def run(self, definition: ScannerDefinition) -> list[ScannerResult]:
        """Run a scanner definition (v3 parity)."""
        if self._engine is None:
            raise CapabilityNotSupportedError("scanner engine is not bound to this session")
        return list(self._engine.run(definition))

    def run_all(self) -> dict[str, list[ScannerResult]]:
        """Run every discovered definition, keyed by index (ScannerRuntime parity)."""
        if self._engine is None:
            raise CapabilityNotSupportedError("scanner engine is not bound to this session")
        return {
            f"scanner_{i}": list(self._engine.run(definition))
            for i, definition in enumerate(self._definitions)
        }

    def top(self, definition: ScannerDefinition, limit: int = 20) -> list[ScannerResult]:
        """Run a scanner and return the top *limit* results (v3 parity)."""
        if self._engine is None:
            raise CapabilityNotSupportedError("scanner engine is not bound to this session")
        return list(self._engine.top(definition, limit))


__all__ = ["ScannerService"]
