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

# Table allowlist — only these views may appear in FROM / JOIN clauses.
_ALLOWED_TABLES = frozenset({"ohlcv", "ohlcv_raw"})

# File-read functions that must never appear anywhere in the SQL.
_FILE_READ_PATTERNS = (
    "READ_CSV", "READ_PARQUET", "READ_JSON", "READ_TEXT", "READ_BLOB",
    "SNIFF_CSV", "GLOB(", "_SCAN(",
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
        # Deny arbitrary file-read functions (Phase 0 security).
        code_upper = _strip_literals(sql).upper()
        for pattern in _FILE_READ_PATTERNS:
            if pattern in code_upper:
                raise QueryNotAllowedError(
                    f"forbidden file-read pattern: {pattern}"
                )
        # Allowlist FROM/JOIN targets — only ohlcv / ohlcv_raw.
        self._check_table_allowlist(sql)

    def _check_table_allowlist(self, sql: str) -> None:
        """Reject SQL that references tables outside the allowlist."""
        code = _strip_literals(sql).upper()
        # Collect CTE names so FROM <cte_alias> is not rejected.
        cte_names: set[str] = set()
        for m in re.finditer(r"\bWITH\b(?:\s+RECURSIVE)?\s+(\w+)(?:\s*\([^)]*\))?\s+AS\s*\(", code):
            cte_names.add(m.group(1))
        # Also match subsequent CTE definitions: , name AS (
        for m in re.finditer(r",\s*(\w+)\s+AS\s*\(", code):
            cte_names.add(m.group(1))

        # Walk through tokens tracking paren depth.
        # After FROM/JOIN at depth 0, the next real token is either:
        # - '(' → subquery, skip until matching ')' then skip alias
        # - a table name → must be in allowlist or a CTE name
        tokens = re.split(r"([\s()]+)", code)  # keep parens; split on whitespace
        expect_table = False
        paren_depth = 0
        in_subquery = False
        for tok in tokens:
            tok = tok.strip()
            if not tok:
                continue
            if tok == "(":
                if expect_table:
                    # Subquery — skip until matching close paren
                    paren_depth = 1
                    expect_table = False
                    in_subquery = True
                else:
                    paren_depth += 1
                continue
            if tok == ")":
                if paren_depth > 0:
                    paren_depth -= 1
                if paren_depth == 0 and in_subquery:
                    in_subquery = False
                    # Next tokens may be AS <alias> — skip them
                    expect_table = False  # consumed
                continue
            if paren_depth > 0:
                continue  # inside a subquery
            if tok in ("FROM", "JOIN"):
                expect_table = True
                continue
            if expect_table:
                expect_table = False
                if tok == "AS":
                    continue  # skip alias keyword + next token is alias name
                table = tok.split(".")[-1] if "." in tok else tok
                if table in cte_names:
                    continue
                if table and table.lower() not in _ALLOWED_TABLES:
                    raise QueryNotAllowedError(
                        f"table {table!r} not in allowlist {_ALLOWED_TABLES}"
                    )

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
              # Runs inside the lock to avoid racing with schema changes.
              try:
                  count_sql = f"SELECT count(*) FROM ({sql.rstrip().rstrip(';')}) AS _q"
                  total = int(con.execute(count_sql, args).fetchone()[0])  # type: ignore[index]
              except Exception:
                  total = None
          if truncated and require_complete:
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
