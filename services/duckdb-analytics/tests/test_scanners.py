"""Tests for the single consolidated screener + breadth."""

from __future__ import annotations

from duck_analytics.scanners import breadth, scan_screener


class TestScreener:
    def test_renders_with_rs_ctes(self, service):
        q = scan_screener("2026-08-12 09:45:00")
        assert "rs_bars_px" in q.sql and "rs_scored" in q.sql
        assert "TIMESTAMP '2026-08-12 00:00'" in q.sql  # RS bound = prev open

    def test_runs_on_fixture(self, service):
        # tiny RS params so the fixture's 6x2 15m bars satisfy the HAVING
        q = scan_screener(
            "2026-08-12 09:45:00",
            days=2, bars_per_day=2, ma_lens=(2, 3, 5), atr_len=2,
            gap_min_pct=0.0, vol_multiple=0.0, pre30_min_pct=0.0,
            adx_min=0.0, min_open=0.0,
        )
        res = service.execute(q.sql)
        assert res.columns[0] == "symbol"
        assert all(r[1].isoformat().startswith("2026-08-12") for r in res.rows)

    def test_criteria_enforced_when_triggered(self, service):
        q = scan_screener(
            "2026-08-12 09:45:00",
            days=2, bars_per_day=2, ma_lens=(2, 3, 5), atr_len=2,
        )
        res = service.execute(q.sql)
        for row in res.rows:
            assert row[2] >= 100.0                       # penny exclusion
            assert row[3] >= 0.5                         # gap
            assert row[4] >= 0.5                         # momentum
            assert row[5] >= 3.0                         # rel volume
            assert row[6] > 0                            # RS above median

    def test_asof_bound_excludes_future(self, service):
        # First fixture day: no ADX/RS history -> screener must be empty,
        # proving no future bars leak into the decision.
        q = scan_screener(
            "2026-08-05 09:45:00",
            days=2, bars_per_day=2, ma_lens=(2, 3, 5), atr_len=2,
        )
        assert service.execute(q.sql).row_count == 0

    def test_loosened_thresholds_pass_more(self, service):
        strict = scan_screener(
            "2026-08-12 09:45:00", days=2, bars_per_day=2,
            ma_lens=(2, 3, 5), atr_len=2, vol_multiple=50.0,
        )
        loose = scan_screener(
            "2026-08-12 09:45:00", days=2, bars_per_day=2,
            ma_lens=(2, 3, 5), atr_len=2, vol_multiple=0.0,
        )
        assert (service.execute(strict.sql).row_count
                <= service.execute(loose.sql).row_count)


class TestBreadth:
    def test_breadth_returns_per_day_rows(self, service):
        from duck_analytics.testing import DAYS

        q = breadth("2026-08-12 23:59:59", dma=2, timeframe="1d")
        res = service.execute(q.sql)
        assert res.row_count == len(DAYS)
        first = res.rows[-1]  # ORDER BY bucket DESC → last row is first day
        assert str(first[0]).startswith("2026-08-05")
        assert first[2] == 0  # nothing above its (partial) DMA on day 1
