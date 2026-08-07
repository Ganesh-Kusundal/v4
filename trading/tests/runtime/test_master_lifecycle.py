"""Master lifecycle tests — cache, loader, scheduler.

Ported from v3 ``test_master_lifecycle.py``.

v4 ``MasterFileCache``, ``MasterLoader``, ``InstrumentRefreshScheduler`` have
compatible APIs with v3.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest
from tradex_domain.errors import SDKError

from tradex_trading.runtime.master_lifecycle import (
    InstrumentRefreshScheduler,
    MasterFileCache,
    MasterLoader,
)


def _cache(tmp_path: Path, *, clock: Any = None) -> MasterFileCache:
    kwargs: dict[str, Any] = {"prefix": "dhan-instruments-", "suffix": ".csv"}
    if clock is not None:
        kwargs["clock"] = clock
    return MasterFileCache(tmp_path, **kwargs)


# ---------------------------------------------------------------------------
# MasterFileCache — dated on-disk cache with TTL
# ---------------------------------------------------------------------------


class TestMasterFileCache:
    """MasterFileCache — dated per-day cache with atomic writes."""

    def test_downloads_then_serves_warm(self, tmp_path: Path) -> None:
        now = time.time()
        cache = _cache(tmp_path, clock=lambda: now)
        downloads: list[int] = []

        def download() -> bytes:
            downloads.append(1)
            return b"row-one\nrow-two\n"

        assert cache.load(download) == b"row-one\nrow-two\n"
        assert cache.load(download) == b"row-one\nrow-two\n"  # warm: no download
        assert len(downloads) == 1
        path = cache.cache_path()
        assert path.name.startswith("dhan-instruments-")
        assert path.name.endswith(".csv")

    def test_same_day_file_served_regardless_of_age(self, tmp_path: Path) -> None:
        """Today's dated file is the day's cache — a mid-day restart (even past
        any hour-based TTL) reuses it instead of re-downloading."""
        clock_value = [1_000_000.0]
        cache = _cache(tmp_path, clock=lambda: clock_value[0])
        downloads: list[int] = []

        def download() -> bytes:
            downloads.append(1)
            return b"content"

        cache.load(download)
        path = cache.cache_path()
        os.utime(path, (clock_value[0], clock_value[0]))
        clock_value[0] += 7 * 3600.0  # same day, hours later
        assert cache.load(download) == b"content"
        assert len(downloads) == 1  # no same-day re-download

    def test_new_day_triggers_single_download(self, tmp_path: Path) -> None:
        """Once per day: the first request after the date rolls downloads and
        caches the new day's file; the rest of the day serves it."""
        clock_value = [1_000_000.0]
        cache = _cache(tmp_path, clock=lambda: clock_value[0])
        downloads: list[int] = []

        def download() -> bytes:
            downloads.append(1)
            return b"content"

        cache.load(download)                     # day 1: first connect downloads
        cache.load(download)                     # day 1: warm, cached for the day
        clock_value[0] += 24 * 3600.0            # date rolls over
        assert cache.load(download) == b"content"  # day 2: one fresh download
        cache.load(download)                     # day 2: warm again
        assert len(downloads) == 2

    def test_force_refresh_bypasses_cache(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        downloads: list[int] = []

        def download() -> bytes:
            downloads.append(1)
            return b"content"

        cache.load(download)
        cache.load(download, force_refresh=True)
        assert len(downloads) == 2

    def test_cleans_up_stale_files(self, tmp_path: Path) -> None:
        clock_value = [10_000_000.0]
        cache = _cache(tmp_path, clock=lambda: clock_value[0])
        stale = tmp_path / "dhan-instruments-2020-01-01.csv"
        stale.write_bytes(b"old")
        old_mtime = clock_value[0] - 8 * 24 * 3600.0
        os.utime(stale, (old_mtime, old_mtime))

        other = tmp_path / "unrelated.csv"
        other.write_bytes(b"keep")
        os.utime(other, (old_mtime, old_mtime))

        cache.load(lambda: b"fresh")

        assert not stale.exists()
        assert other.exists()  # cleanup only touches the provider prefix

    def test_rejects_empty_download(self, tmp_path: Path) -> None:
        cache = _cache(tmp_path)
        with pytest.raises(SDKError, match="empty"):
            cache.load(lambda: b"")


# ---------------------------------------------------------------------------
# MasterLoader — download + parse pipeline
# ---------------------------------------------------------------------------


class TestMasterLoader:
    """MasterLoader — download + parse with optional cache."""

    def test_warm_and_force_paths(self) -> None:
        downloads: list[bool] = []

        def download() -> bytes:
            downloads.append(True)
            return b"a,b\n1,2\n"

        import csv
        import io

        def parse(raw: object) -> list[dict[str, Any]]:
            assert isinstance(raw, bytes)
            return [dict(row) for row in csv.DictReader(io.StringIO(raw.decode()))]

        loader = MasterLoader(download, parse)
        assert loader.load() == [{"a": "1", "b": "2"}]
        assert loader.load(force_refresh=True) == [{"a": "1", "b": "2"}]
        assert len(downloads) == 2  # uncached loader always downloads


# ---------------------------------------------------------------------------
# InstrumentRefreshScheduler — daemon thread with error counting
# ---------------------------------------------------------------------------


class TestInstrumentRefreshScheduler:
    """InstrumentRefreshScheduler — periodic refresh daemon."""

    def test_counts_success_and_errors(self) -> None:
        state = {"fail": False}

        def refresh() -> None:
            if state["fail"]:
                raise RuntimeError("cdn hiccup")

        scheduler = InstrumentRefreshScheduler("dhan", refresh)
        assert scheduler.refresh_now() is True
        state["fail"] = True
        assert scheduler.refresh_now() is False
        assert scheduler.refresh_count == 1
        assert scheduler.error_count == 1

    def test_starts_and_stops(self) -> None:
        scheduler = InstrumentRefreshScheduler(
            "upstox", lambda: None, interval_seconds=3600.0,
        )
        scheduler.start()
        assert scheduler.is_running is True
        scheduler.start()  # idempotent
        scheduler.stop()
        assert scheduler.is_running is False
