"""Query guard tests — read-only enforcement, caps, truncation."""

from __future__ import annotations

import pytest

from duck_analytics.query import QueryNotAllowedError


class TestGuards:
    @pytest.mark.parametrize("sql", [
        "INSERT INTO ohlcv VALUES (1)",
        "DELETE FROM ohlcv",
        "CREATE TABLE t(a int)",
        "DROP VIEW ohlcv",
        "ATTACH 'x.db' AS x",
        "COPY ohlcv TO '/tmp/out.csv'",
        "INSTALL httpfs",
        "PRAGMA database_list",
        "SET threads=8",
        "CALL dbgen()",
    ])
    def test_forbidden_statements_rejected(self, service, sql):
        with pytest.raises(QueryNotAllowedError):
            service.execute(sql)

    def test_multi_statement_rejected(self, service):
        with pytest.raises(QueryNotAllowedError):
            service.execute("SELECT 1; SELECT 2")

    def test_select_allowed(self, service):
        res = service.execute("SELECT count(*) AS n FROM ohlcv WHERE symbol='TCS'")
        assert res.rows[0][0] > 0
        assert not res.truncated

    def test_with_allowed(self, service):
        res = service.execute(
            "WITH c AS (SELECT 1 AS x) SELECT sum(x) FROM c"
        )
        assert res.rows[0][0] == 1


class TestCaps:
    def test_server_limit_appended(self, service):
        # 6 fixture days x 30 bars for TCS; ask as if unlimited — cap at 10.
        res = service.execute(
            "SELECT ts FROM ohlcv WHERE symbol='TCS'", limit=10
        )
        assert res.row_count == 10
        assert res.truncated is True
        assert res.total_row_count == 180

    def test_limit_ceiling_enforced(self, service):
        res = service.execute("SELECT ts FROM ohlcv", limit=10**9)
        assert res.row_count <= 50_000

    def test_user_limit_cannot_bypass_service_cap(self, service):
        res = service.execute("SELECT ts FROM ohlcv LIMIT 100", limit=2)
        assert res.row_count == 2
        assert res.truncated is True

    def test_elapsed_and_columns(self, service):
        res = service.execute("SELECT symbol, close FROM ohlcv LIMIT 3")
        assert res.columns == ["symbol", "close"]
        assert res.elapsed_ms >= 0.0


class TestLiteralGuard:
    def test_forbidden_word_inside_string_literal_allowed(self, service):
        # 'DROP' as DATA must not trip the statement guard.
        res = service.execute("SELECT count(*) AS n FROM ohlcv WHERE symbol='DROP'")
        assert res.rows[0][0] == 0

    def test_real_statement_still_rejected(self, service):
        with pytest.raises(QueryNotAllowedError):
            service.execute("SELECT 1; DROP VIEW ohlcv")


class TestTimeout:
    def test_long_query_interrupted(self, catalog, tmp_path):
        from duck_analytics.config import AnalyticsConfig
        from duck_analytics.query import QueryService

        cfg = AnalyticsConfig(base_path=tmp_path / "missing",
                              statement_timeout_s=0.2)
        svc = QueryService(catalog, cfg)
        with pytest.raises(Exception) as ei:
            svc.execute(
                "WITH RECURSIVE t(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM t) "
                "SELECT sum(x) FROM t"  # infinite recursion → interrupted
            )
        assert "interrupt" in str(ei.value).lower() or "timed out" in str(ei.value).lower() \
            or "INTERRUPTED" in str(ei.value)


class TestPointInTimeFlag:
    def test_raw_queries_are_not_verified_safe(self, service):
        res = service.execute("SELECT 1")
        assert res.point_in_time_safe is False

    def test_strict_query_rejects_truncated_results(self, service):
        with pytest.raises(QueryNotAllowedError, match="truncated"):
            service.execute("SELECT ts FROM ohlcv", limit=2, require_complete=True)

    def test_result_contains_dataset_provenance(self, service):
        res = service.execute("SELECT 1")
        assert res.dataset_fingerprint
