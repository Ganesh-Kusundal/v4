"""QueryService — guarded, read-only SQL execution with result metadata.

Guards:
- SELECT/WITH statements only; mutations, ATTACH/COPY/INSTALL/PRAGMA/CALL and
  multi-statement strings are rejected before touching DuckDB. Keywords
  inside single-quoted literals are ignored (a symbol named 'DROP' is data).
- Server-side row cap: an extra ``LIMIT`` is appended when the user's SQL has
  none; results beyond the cap are reported as ``truncated``.
- Statement timeout: the query is interrupted after
  ``AnalyticsConfig.statement_timeout_s`` seconds.
- Retry-once on read IO errors (backfills rewrite partition files in place;
  a concurrent reader can observe a torn file).
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

import duckdb

from duck_analytics.catalog import DuckDBCatalog
from duck_analytics.config import AnalyticsConfig

_FORBIDDEN = (
    "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "ATTACH",
    "DETACH", "COPY", "EXPORT", "IMPORT", "INSTALL", "LOAD", "CALL",
    "PRAGMA", "SET", "VACUUM", "GRANT", "REVOKE", "CHECKPOINT",
)

# Single-quoted literal (doubled quotes are escapes) — stripped before the
# keyword scan so values are never mistaken for statements.
_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


class QueryNotAllowedError(ValueError):
    """Raised when a statement fails the read-only guard."""


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Rows plus execution metadata for callers/MCP."""

    columns: list[str]
    rows: list[tuple]
    row_count: int
    total_row_count: int | None  # None when unknown (LIMIT hit without count)
    truncated: bool
    elapsed_ms: float
    point_in_time_safe: bool
    dataset_fingerprint: str


def _first_keyword(sql: str) -> str:
    stripped = sql.strip().lstrip("( \n\t")
    return stripped.split(None, 1)[0].upper() if stripped else ""


def _strip_literals(sql: str) -> str:
    return _LITERAL_RE.sub("''", sql)


def _contains_forbidden(sql: str) -> str | None:
    code = _strip_literals(sql).upper()
    tokens = code.replace("(", " ").replace(")", " ").replace(";", " ").split()
    for word in _FORBIDDEN:
        if any(tok == word or tok.startswith(word + "(") for tok in tokens):
            return word
    return None


class QueryService:
    """Execute read-only SQL against the catalog's views."""

    def __init__(
        self,
        catalog: DuckDBCatalog,
        config: AnalyticsConfig | None = None,
    ) -> None:
        self._catalog = catalog
        self._config = config or AnalyticsConfig()

    # ------------------------------------------------------------------ guard

    def _validate(self, sql: str) -> None:
        first = _first_keyword(sql)
        if first not in ("SELECT", "WITH"):
            raise QueryNotAllowedError(
                f"only SELECT/WITH queries are allowed (got {first!r})"
            )
        body = sql.strip().rstrip(";")
        if ";" in body:
            # Allow one trailing semicolon only.
            raise QueryNotAllowedError("multiple statements are not allowed")
        forbidden = _contains_forbidden(sql)
        if forbidden is not None:
            raise QueryNotAllowedError(f"forbidden keyword: {forbidden}")

    def _apply_limit(self, sql: str, limit: int) -> tuple[str, bool]:
        """Wrap SQL so the service cap cannot be bypassed by caller SQL."""
        return f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS _limited_q LIMIT {limit}", True

    # ------------------------------------------------------------------ exec

    def execute(
        self,
        sql: str,
        params: dict[str, object] | list[object] | None = None,
        *,
        limit: int | None = None,
        require_complete: bool = False,
    ) -> QueryResult:
        """Run *sql* read-only. Named params use DuckDB ``$name`` syntax.

        Raw SQL is never classified as verified point-in-time safe. Scanner
        wrappers expose that property separately after constructing a bounded
        query.
        """
        self._validate(sql)
        cap = min(limit or self._config.default_row_limit, self._config.max_row_limit)
        limited_sql, _applied = self._apply_limit(sql, cap)

        con = self._catalog.connection
        args: list[object] | dict[str, object] = (
            params if params is not None else []
        )
        start = time.perf_counter()
        timer: threading.Timer | None = None
        timeout_s = self._config.statement_timeout_s
        with self._catalog.execution_lock:
          try:
            if timeout_s and timeout_s > 0:
                timer = threading.Timer(timeout_s, con.interrupt)
                timer.daemon = True
                timer.start()
            rel = con.execute(limited_sql, args)
            rows = rel.fetchall()
            col_names = [c[0] for c in (rel.description or [])]
          except duckdb.IOException as e:
            # Torn parquet file during a concurrent upsert rewrite — retry once.
            try:
                rel = con.execute(limited_sql, args)
                rows = rel.fetchall()
                col_names = [c[0] for c in (rel.description or [])]
            except duckdb.IOException:
                raise RuntimeError(
                    f"data file unreadable after retry (concurrent rewrite?): {e}"
                ) from e
          finally:
            if timer is not None:
                timer.cancel()

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        truncated = len(rows) >= cap
        total: int | None = None
        if truncated:
            # Best-effort true count so callers know what they missed.
            try:
                count_sql = f"SELECT count(*) FROM ({sql.rstrip().rstrip(';')}) AS _q"
                total = int(con.execute(count_sql, args).fetchone()[0])  # type: ignore[index]
            except Exception:
                total = None
            if require_complete:
                raise QueryNotAllowedError(f"query result truncated at {cap} rows")
        return QueryResult(
            columns=col_names,
            rows=rows[:cap],
            row_count=len(rows),
            total_row_count=total,
            truncated=truncated,
            elapsed_ms=round(elapsed_ms, 2),
            point_in_time_safe=False,
            dataset_fingerprint=self._catalog.dataset_fingerprint,
        )


__all__ = ["QueryService", "QueryResult", "QueryNotAllowedError"]
