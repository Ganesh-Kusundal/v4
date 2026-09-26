"""The runtime and the datalake scripts must read the canonical registries.

``tradex_domain.timeframe`` and ``tradex_domain.market_calendar`` are the only
copies of the timeframe->seconds map and of the 09:15/15:30 session bounds.
These tests fail if a private copy creeps back into a consumer, or if the
canonical registry stops answering for a registered timeframe (``W1`` used to
be missing from ``bar_aggregator``'s local dict, so a weekly aggregation
request raised ``ValueError``).
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import time
from pathlib import Path

from tradex_domain.enums import Timeframe
from tradex_domain.market_calendar import (
    MARKET_CLOSE,
    MARKET_CLOSE_STR,
    MARKET_OPEN,
    MARKET_OPEN_STR,
)
from tradex_domain.timeframe import bucket_seconds

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    """Import a ``trading/scripts`` module by path (scripts are not a package)."""
    path = _REPO_ROOT / "trading" / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"_adoption_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bucket_seconds_answers_weekly() -> None:
    """The canonical registry covers W1 (the key the private copy omitted)."""
    assert bucket_seconds(Timeframe.W1) == 604_800


def test_runtime_bar_aggregation_resolves_weekly() -> None:
    """A weekly BarAggregator constructs instead of raising ValueError."""
    from tradex_runtime.bar_aggregator import BarAggregator, _tf_seconds

    assert _tf_seconds(Timeframe.W1) == 604_800
    aggregator = BarAggregator("NSE:RELIANCE", Timeframe.W1, on_frame=lambda frame: None)
    assert aggregator._seconds == 604_800


def test_datalake_stats_sql_matches_the_literal_it_replaced() -> None:
    """Calendar-derived SQL is byte-identical to the hand-written literal.

    The interior band stays 1 minute inside each session edge: 09:16/15:29
    are measured density, 09:15/15:30 are counted as session-edge stamps.
    """
    stats = _load_script("datalake_stats.py")
    sql = stats._PER_DAY_SQL
    assert "BETWEEN TIME '09:16:00' AND TIME '15:29:00'" in sql
    assert "= TIME '09:15:00'" in sql
    assert "> TIME '15:29:00'" in sql
    # The band really is the interior, not the session bounds.
    assert stats.INTERIOR_OPEN_STR == "09:16:00"
    assert stats.INTERIOR_CLOSE_STR == "15:29:00"
    assert stats.OPEN_STR == MARKET_OPEN_STR == "09:15:00"
    assert stats.CLOSE_STR == MARKET_CLOSE_STR == "15:30:00"
    assert (stats.INTERIOR_OPEN_LABEL, stats.INTERIOR_CLOSE_LABEL) == ("09:16", "15:29")
    assert (stats.OPEN_LABEL, stats.CLOSE_LABEL) == ("09:15", "15:30")


def test_feed_integrity_default_session_window_is_the_canonical_session() -> None:
    """The default session window is the market calendar's, not a local literal."""
    import inspect

    from tradex_runtime.feed_integrity import FeedIntegrityTracker

    default = inspect.signature(FeedIntegrityTracker.__init__).parameters["session_window"].default
    assert default == (MARKET_OPEN, MARKET_CLOSE) == (time(9, 15), time(15, 30))
    assert FeedIntegrityTracker()._session_window == (MARKET_OPEN, MARKET_CLOSE)


def test_seed_fixture_starts_at_the_canonical_open() -> None:
    """The seeded fixture's first bar is stamped at MARKET_OPEN, 375 bars long."""
    seed = _load_script("seed_e2e_datalake.py")
    assert seed.SESSION_START == MARKET_OPEN == time(9, 15)
    assert seed.SESSION_MINUTES == 375
