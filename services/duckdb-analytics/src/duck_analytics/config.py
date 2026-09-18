"""Service configuration — paths and resource limits.

Defaults match the datalake layout written by ``ParquetStorage``
(``data/ohlcv/symbol=…/year=…/month=…/data.parquet``). The service reads the
same files; it never writes them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

MARKET_OPEN = "09:15:00"
MARKET_CLOSE = "15:29:00"


@dataclass(frozen=True, slots=True)
class AnalyticsConfig:
    """Paths + guardrails for the analytics connection."""

    #: Repo-root-relative or absolute path to the ``ohlcv`` hive root.
    base_path: Path = field(default_factory=lambda: Path("data/ohlcv"))
    #: DuckDB memory cap — a runaway cross join must not starve the host.
    memory_limit: str = "4GB"
    #: DuckDB thread cap.
    threads: int = 4
    #: Default row cap for query results (server-enforced LIMIT).
    default_row_limit: int = 1000
    #: Hard ceiling for any query's row limit.
    max_row_limit: int = 50_000
    #: Wall-clock budget per query in seconds.
    statement_timeout_s: float = 60.0

    @property
    def glob(self) -> str:
        """Glob pattern over every partition's data.parquet."""
        return str(Path(self.base_path) / "**" / "data.parquet")

    def resolve_base(self, repo_hint: Path | None = None) -> AnalyticsConfig:
        """Return a config whose base_path exists relative to *repo_hint*.

        The MCP server may start from any cwd; when the default relative
        path does not exist, walk up from *repo_hint* looking for a
        directory that has the store.
        """
        if Path(self.base_path).exists():
            return self
        if repo_hint is None:
            return self
        probe = Path(repo_hint).resolve()
        for candidate in (probe, *probe.parents):
            resolved = candidate / self.base_path
            if resolved.exists():
                return AnalyticsConfig(
                    base_path=resolved,
                    memory_limit=self.memory_limit,
                    threads=self.threads,
                    default_row_limit=self.default_row_limit,
                    max_row_limit=self.max_row_limit,
                    statement_timeout_s=self.statement_timeout_s,
                )
        return self


__all__ = ["AnalyticsConfig", "MARKET_OPEN", "MARKET_CLOSE"]
