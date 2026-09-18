"""Tests for ``reconcile_bars.py`` — the repair must refuse to make things worse.

Reconciliation writes over stored history, so the interesting behaviour is not
that it fixes a wrong day; it is what it declines to touch:

* a broker response too thin to cover the day it would replace (the rewrite
  would leave half a session at the old level — worse than the original);
* a day only one side has at all, which the storage guard already drops as a
  phantom session.

Both are asserted here, plus the end-to-end path against a synthetic lake, since
the real lake is gitignored and absent in CI.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from tradex_trading.datalake.parquet_storage import ParquetStorage

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "reconcile_bars.py"
_spec = importlib.util.spec_from_file_location("reconcile_bars", _SCRIPT)
assert _spec is not None and _spec.loader is not None, _SCRIPT
reconcile = importlib.util.module_from_spec(_spec)
# ``@dataclass`` resolves annotations through ``sys.modules[cls.__module__]``,
# so the module must be registered before it executes.
sys.modules["reconcile_bars"] = reconcile
_spec.loader.exec_module(reconcile)

compare_days = reconcile.compare_days
daily_aggregate = reconcile.daily_aggregate
reconcile_symbol = reconcile.reconcile_symbol

#: Wed/Thu/Fri — ``_prepare_frame`` drops weekend bars, so synthetic days must
#: be real weekdays inside the session.
_WED, _THU, _FRI = "2026-02-04", "2026-02-05", "2026-02-06"


def _frame(symbol: str, day: str, close: float, *, bars: int = 375,
           volume: float = 1000.0) -> pd.DataFrame:
    """``bars`` valid 1m bars for one weekday session, all closing at *close*."""
    start = datetime.fromisoformat(f"{day} 09:15")
    rows = []
    for i in range(bars):
        stamp = start + timedelta(minutes=i)
        rows.append({
            "symbol": symbol, "exchange": "NSE", "kind": "equity",
            "timeframe": "1m", "timestamp": stamp,
            "open": close, "high": close, "low": close, "close": close,
            "volume": volume,
        })
    return pd.DataFrame(rows)


def _lake(tmp_path: Path, symbol: str, *days: tuple[str, float]) -> ParquetStorage:
    store = ParquetStorage(tmp_path)
    store.upsert(pd.concat([_frame(symbol, d, c) for d, c in days],
                           ignore_index=True))
    return store


class TestDailyAggregate:
    def test_takes_the_high_close_and_sums_volume(self) -> None:
        frame = pd.concat([
            _frame("X", _WED, 10.0, bars=3, volume=100.0),
            _frame("X", _WED, 12.0, bars=2, volume=50.0),
        ], ignore_index=True)
        out = daily_aggregate(frame)
        assert list(out.columns) == ["symbol", "day", "bars", "close", "volume"]
        row = out.iloc[0]
        assert (row.bars, row.close, row.volume) == (5, 12.0, 400.0)

    def test_empty_frame_keeps_the_shape(self) -> None:
        assert daily_aggregate(pd.DataFrame()).empty


class TestCompareDays:
    def _agg(self, symbol: str, *days: tuple[str, float], bars: int = 375):
        return daily_aggregate(pd.concat(
            [_frame(symbol, d, c, bars=bars) for d, c in days], ignore_index=True))

    def test_agreement_is_not_a_disagreement(self) -> None:
        lake = self._agg("X", (_WED, 100.0))
        source = self._agg("X", (_WED, 100.0))
        days = compare_days(lake, source, tol=0.005, min_coverage=0.95, min_bars=300)
        assert [d.status for d in days] == ["agree"]
        assert days[0].rewrite is False

    def test_a_rebased_day_is_flagged_and_rewritable(self) -> None:
        """The ANANDRATHI shape: every stored day at 2x the broker's."""
        lake = self._agg("X", (_WED, 200.0))
        source = self._agg("X", (_WED, 100.0))
        days = compare_days(lake, source, tol=0.005, min_coverage=0.95, min_bars=300)
        assert days[0].status == "disagree"
        assert days[0].ratio == pytest.approx(2.0)
        assert days[0].rewrite is True
        assert days[0].reason == ""

    def test_a_thin_response_is_refused(self) -> None:
        """200 bars cannot replace a full day: it would leave a stale remainder."""
        lake = self._agg("X", (_WED, 200.0))
        source = self._agg("X", (_WED, 100.0), bars=200)
        days = compare_days(lake, source, tol=0.005, min_coverage=0.95, min_bars=300)
        assert days[0].status == "disagree"
        assert days[0].rewrite is False
        assert "need 356" in days[0].reason

    def test_the_absolute_floor_also_refuses(self) -> None:
        """A 95% response of a 50-bar day is still not enough to overwrite."""
        lake = self._agg("X", (_WED, 200.0), bars=50)
        source = self._agg("X", (_WED, 100.0), bars=48)
        days = compare_days(lake, source, tol=0.005, min_coverage=0.95, min_bars=300)
        assert days[0].rewrite is False

    def test_one_sided_days_are_named_not_rewritten(self) -> None:
        """A day only the broker has is a phantom session (2026-02-01, a Sunday)
        or a gap — never a rewrite, and never silently merged into 'agree'."""
        lake = self._agg("X", (_WED, 100.0))
        source = self._agg("X", (_WED, 100.0), (_THU, 101.0))
        days = {d.day: d for d in compare_days(
            lake, source, tol=0.005, min_coverage=0.95, min_bars=300)}
        assert days[_THU].status == "source_only"
        assert days[_THU].rewrite is False

    def test_volume_ratio_is_reported(self) -> None:
        lake = daily_aggregate(_frame("X", _WED, 200.0, volume=2000.0))
        source = daily_aggregate(_frame("X", _WED, 100.0, volume=1000.0))
        days = compare_days(lake, source, tol=0.005, min_coverage=0.95, min_bars=300)
        assert days[0].volume_ratio == pytest.approx(2.0)


class TestReconcileSymbol:
    """End-to-end against a synthetic lake, with the fetch stubbed out."""

    def _run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
             apply: bool) -> reconcile.SymbolReport:
        store = _lake(tmp_path, "X", (_WED, 100.0), (_THU, 200.0), (_FRI, 101.0))
        source = pd.concat([_frame("X", _WED, 100.0), _frame("X", _THU, 100.0),
                            _frame("X", _FRI, 101.0)], ignore_index=True)
        monkeypatch.setattr(reconcile, "fetch_source",
                            lambda *a, **kw: source)
        return reconcile_symbol(
            store, object(), "X",
            start=datetime.fromisoformat(f"{_WED} 09:15"),
            end=datetime.fromisoformat(f"{_FRI} 15:30"),
            tol=0.005, min_coverage=0.95, min_bars=300, window_days=20,
            apply=apply,
        )

    def test_dry_run_reports_without_writing(self, tmp_path, monkeypatch) -> None:
        report = self._run(tmp_path, monkeypatch, apply=False)
        assert len(report.disagreed) == 1
        assert report.days_rewritten == 0
        stored = ParquetStorage(tmp_path).read(symbols=["X"])
        kept = stored[stored.timestamp.dt.date.astype(str) == _THU].close.max()
        assert kept == 200.0, "dry run must not touch the lake"

    def test_apply_corrects_the_day_and_confirms_it(self, tmp_path, monkeypatch) -> None:
        report = self._run(tmp_path, monkeypatch, apply=True)
        assert report.days_rewritten == 1
        assert report.residual == 0, "the repair must be confirmed by re-reading"
        stored = ParquetStorage(tmp_path).read(symbols=["X"])
        fixed = stored[stored.timestamp.dt.date.astype(str) == _THU]
        assert fixed.close.max() == 100.0
        # Neighbouring days are untouched.
        assert stored[stored.timestamp.dt.date.astype(str) == _WED].close.max() == 100.0

    def test_an_empty_broker_response_is_reported_not_applied(
        self, tmp_path, monkeypatch
    ) -> None:
        store = _lake(tmp_path, "X", (_WED, 100.0))
        monkeypatch.setattr(reconcile, "fetch_source",
                            lambda *a, **kw: pd.DataFrame())
        report = reconcile_symbol(
            store, object(), "X",
            start=datetime.fromisoformat(f"{_WED} 09:15"),
            end=datetime.fromisoformat(f"{_WED} 15:30"),
            tol=0.005, min_coverage=0.95, min_bars=300, window_days=20, apply=True,
        )
        assert report.rows_written == 0
        assert report.skipped and "no bars" in report.skipped[0]
