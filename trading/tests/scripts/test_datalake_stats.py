"""Tests for ``datalake_stats.py`` — the lake's numbers must be re-derivable.

Two contracts, both about drift:

1. **The reporter tells the truth.** It is the thing CLAUDE.md now points at
   instead of asserting coverage figures, so a wrong row count or a density
   shortfall it fails to see is a doc bug with no other symptom. The synthetic
   lake lets the numbers be checked exactly; the real lake is gitignored and
   absent in CI, so asserting against it is not an option.

2. **The doc keeps pointing at it.** The "Datalake Facts" block is asserted to
   name this script and to carry no hardcoded size/day/row figure — the three
   numbers that had gone stale. ``test_datalake_root`` in ``tests/interface``
   is the precedent for a grep-level contract of this kind.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pandas as pd
import pytest

from tradex_trading.datalake.parquet_storage import ParquetStorage

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "datalake_stats.py"
_spec = importlib.util.spec_from_file_location("datalake_stats", _SCRIPT)
assert _spec is not None and _spec.loader is not None, _SCRIPT
datalake_stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(datalake_stats)

measure = datalake_stats.measure
render = datalake_stats.render
main = datalake_stats.main
resolve_root = datalake_stats.resolve_root

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLAUDE_MD = _REPO_ROOT / "CLAUDE.md"


def _bars(symbol: str, day: str, minutes: int, *, start_minute: int = 15) -> pd.DataFrame:
    """``minutes`` 1m bars from ``start_minute`` on a 2026-07 weekday session."""
    return pd.DataFrame([
        dict(symbol=symbol, exchange="NSE", kind="equity", timeframe="1m",
             timestamp=f"2026-07-{day} 09:{start_minute + i:02d}:00",
             open=100, high=101, low=99, close=100, volume=1000)
        for i in range(minutes)
    ])


def _lake(tmp_path: Path) -> Path:
    """A two-symbol, two-day lake where one symbol is deliberately short."""
    store = ParquetStorage(tmp_path)
    for day in ("01", "02"):
        store.upsert(_bars("RELIANCE", day, 5))
        store.upsert(_bars("TCS", day, 3))  # 2 bars short of RELIANCE each day
    return tmp_path


class TestMeasure:
    def test_reports_rows_symbols_and_span(self, tmp_path):
        report = measure(_lake(tmp_path))
        assert report["rows"] == 16  # (5 + 3) bars × 2 days
        assert report["symbols"] == 2
        assert report["trading_days"] == 2
        assert report["first_ts"] == "2026-07-01 09:15:00"
        assert report["last_ts"] == "2026-07-02 09:19:00"  # RELIANCE's 5th bar
        assert report["breakdown"] == [
            {"timeframe": "1m", "kind": "equity", "exchange": "NSE", "rows": 16}
        ]

    def test_counts_files_and_bytes_on_disk(self, tmp_path):
        report = measure(_lake(tmp_path))
        files = sorted((tmp_path / "ohlcv").rglob("*.parquet"))
        # One partition file per symbol-month: both days live in 2026-07.
        assert report["parquet_files"] == len(files) == 2
        assert report["bytes"] == sum(p.stat().st_size for p in files) > 0

    def test_density_is_measured_against_the_day_s_own_session(self, tmp_path):
        """TCS is short on both days; RELIANCE, the densest, never is."""
        report = measure(_lake(tmp_path))
        assert report["interior_session_lengths"] == [4]  # 09:16–09:19
        assert report["days_with_interior_shortfall"] == 2
        assert report["interior_short_bars_total"] == 4  # 2 short/day × 2 days
        assert {d["date"] for d in report["worst_days"]} == {"2026-07-01", "2026-07-02"}

    def test_an_intermittent_open_bar_is_counted_apart_from_density(self, tmp_path):
        """The phantom-gap trap: one symbol holding a 09:15 bar must not score
        every other symbol short of the session.

        RELIANCE and TCS hold the identical interior span here and differ only
        in the opening stamp, so a correct report shows zero interior shortfall
        and exactly one absent open bar — whereas "fewer bars than the densest
        symbol" would have called TCS short for a bar the broker never sent.
        """
        store = ParquetStorage(tmp_path)
        store.upsert(_bars("RELIANCE", "01", 5))                      # 09:15–09:19
        store.upsert(_bars("TCS", "01", 4, start_minute=16))          # 09:16–09:19
        report = measure(Path(tmp_path) / "ohlcv")  # a store root also works
        assert report["days_with_interior_shortfall"] == 0
        assert report["interior_short_bars_total"] == 0
        assert report["open_bar_absent_symbol_days"] == 1

    def test_per_symbol_coverage_counts_symbols_off_the_max(self, tmp_path):
        store = ParquetStorage(tmp_path)
        store.upsert(_bars("RELIANCE", "01", 5))
        store.upsert(_bars("RELIANCE", "02", 5))
        store.upsert(_bars("TCS", "02", 5))  # TCS is missing 07-01 entirely
        report = measure(tmp_path)
        assert report["max_days_per_symbol"] == 2
        assert report["open_bar_absent_symbol_days"] == 0  # TCS-bars start at 09:15
        assert report["min_days_per_symbol"] == 1
        assert report["symbols_below_max_days"] == 1
        assert report["symbols_narrower_span"] == 1

    def test_empty_lake_is_reported_not_raised(self, tmp_path):
        report = measure(tmp_path)
        assert report["empty"] is True
        assert report["parquet_files"] == 0
        assert "empty" in render(report)


class TestCli:
    def test_text_report_names_the_root_and_the_numbers(self, tmp_path, capsys):
        lake = _lake(tmp_path)
        assert main(["--root", str(lake)]) == 0
        out = capsys.readouterr().out
        assert str(lake / "ohlcv") in out
        assert "16 rows" in out
        assert "2026-07-01 09:15:00 → 2026-07-02 09:19:00" in out

    def test_json_output_round_trips(self, tmp_path, capsys):
        assert main(["--root", str(_lake(tmp_path)), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["rows"] == 16
        assert payload["interior_short_bars_total"] == 4

    def test_text_report_separates_edge_stamps_from_density(self, tmp_path, capsys):
        store = ParquetStorage(tmp_path)
        store.upsert(_bars("RELIANCE", "01", 5))
        store.upsert(_bars("TCS", "01", 4, start_minute=16))
        assert main(["--root", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "no interior shortfall" in out
        assert "1 symbol-day missing the 09:15 bar" in out

    def test_close_stamp_is_not_density(self, tmp_path):
        """One symbol holding a 15:30 bar must not score the others short.

        The 15:30 stamp is a session-edge convention (no broker serves it), and
        counting it as interior density is what made a repair look unfinished:
        442 symbols "missing" a bar nobody can fetch.
        """
        store = ParquetStorage(tmp_path)
        store.upsert(_bars("RELIANCE", "01", 5))                       # 09:15–09:19
        # TCS has the identical interior span plus the 15:30 closing stamp.
        extra = _bars("TCS", "01", 5)
        extra.loc[len(extra)] = dict(extra.iloc[-1])
        extra.loc[extra.index[-1], "timestamp"] = "2026-07-01 15:30:00"
        store.upsert(extra)
        report = measure(tmp_path)
        assert report["interior_session_lengths"] == [4]   # 09:16–09:19
        assert report["interior_short_bars_total"] == 0    # TCS's 15:30 is no yardstick
        assert report["close_bar_absent_symbol_days"] == 1  # RELIANCE lacks it
        assert report["open_bar_absent_symbol_days"] == 0

    def test_root_may_name_the_store_directly(self, tmp_path, capsys):
        lake = _lake(tmp_path)
        assert main(["--root", str(lake / "ohlcv"), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["rows"] == 16

    def test_default_root_is_the_anchored_lake(self, monkeypatch, tmp_path):
        """No --root means the same lake ``serve`` reads, not the cwd.

        Resolution only: the real lake is gitignored and absent in CI, so this
        must not require it to exist (or to be scan-worthy).
        """
        from tradex_trading.datalake.paths import datalake_root

        monkeypatch.chdir(tmp_path)  # the 2026-09-02 "serve from trading/" case
        assert resolve_root(None) == Path(datalake_root())
        assert resolve_root(str(tmp_path / "elsewhere")) == tmp_path / "elsewhere"


class TestClaudeMdContract:
    """The doc must delegate its coverage numbers to the reporter, not assert them."""

    @pytest.fixture(scope="class")
    def section(self) -> str:
        text = _CLAUDE_MD.read_text(encoding="utf-8")
        start = text.index("## Datalake Facts")
        end = text.index("## ", start + 1)  # next top-level heading
        return text[start:end]

    def test_points_at_the_reporter(self, section):
        assert "datalake_stats.py" in section, (
            "the Datalake Facts block no longer tells the reader how to get the "
            "real numbers; it is the only place agents look"
        )

    @pytest.mark.parametrize(
        ("pattern", "stale_claim"),
        [
            (r"\d+(?:\.\d+)?\s*(?:KB|MB|GB|TB)\b", "a hardcoded lake size"),
            (r"~?\d+\+?\s*(?:trading\s+)?days\b", "a hardcoded day count"),
            (r"~?\d+\+?\s*[\w,]*\s*rows\b", "a hardcoded row count"),
        ],
    )
    def test_has_no_stale_measurement(self, section, pattern, stale_claim):
        """The block used to claim "~261 MB, ~63+ trading days" against 745 MB
        over 171 days. Volatile figures belong in the reporter, which recomputes
        them, not in prose nobody re-checks."""
        match = re.search(pattern, section, re.IGNORECASE)
        assert match is None, (
            f"Datalake Facts carries {stale_claim} ({match.group(0)!r}); delete it "
            "and let trading/scripts/datalake_stats.py be the source of truth"
        )
