"""ScannerService — scanner execution over a bound engine."""

from __future__ import annotations

from typing import Any

from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.strategy import ScannerDefinition, ScannerResult


class ScannerService:
    """Scanner execution over a bound engine (D-15); no engine => loud error."""

    def __init__(self, engine: Any = None) -> None:
        self._engine = engine

    def run(self, definition: ScannerDefinition) -> list[ScannerResult]:
        """Run a scanner definition (v3 parity)."""
        if self._engine is None:
            raise CapabilityNotSupportedError("scanner engine is not bound to this session")
        return list(self._engine.run(definition))

    def top(self, definition: ScannerDefinition, limit: int = 20) -> list[ScannerResult]:
        """Run a scanner and return the top *limit* results (v3 parity)."""
        if self._engine is None:
            raise CapabilityNotSupportedError("scanner engine is not bound to this session")
        return list(self._engine.top(definition, limit))


__all__ = ["ScannerService"]
