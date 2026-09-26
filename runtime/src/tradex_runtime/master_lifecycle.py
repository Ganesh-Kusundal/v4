"""Instrument-master lifecycle: dated on-disk cache + daily refresh.

Provides:

* dated cache files (``<prefix>-<YYYY-MM-DD><suffix>``); the *filename date*
  is the freshness boundary — today's file serves the whole day, a fresh
  download happens at most once per day (first connect, or the first request
  after the date rolls), and never again within the same day,
* atomic writes (tmp file + ``os.replace``),
* age-based cleanup of stale cache files,
* a best-effort daily ``InstrumentRefreshScheduler`` daemon that re-downloads
  the master so cold starts stay warm. Refresh failures are counted, never
  raised — a transient CDN hiccup must not take down the trading process.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradex_domain.errors import SDKError

DEFAULT_MASTER_TTL_HOURS = 6.0
DEFAULT_MASTER_CLEANUP_DAYS = 7
_SCHEDULER_DEFAULT_INTERVAL = 86_400.0

ParseRaw = Callable[[Any], list[dict[str, Any]]]
Download = Callable[[], Any]


class MasterFileCache:
    """Dated on-disk master cache: today's file is the cache for the day.

    Freshness is the *filename date*: once today's file exists (non-empty) it
    is served for the rest of the day regardless of age — the master is
    downloaded at most once per day. ``force_refresh`` bypasses the cache.
    """

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        prefix: str,
        suffix: str,
        ttl_hours: float = DEFAULT_MASTER_TTL_HOURS,
        cleanup_days: int = DEFAULT_MASTER_CLEANUP_DAYS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._dir = Path(cache_dir)
        self._prefix = prefix
        self._suffix = suffix
        # Retained for API compatibility; the dated filename is the freshness
        # boundary now — ``ttl_hours`` no longer gates same-day redownloads.
        self._ttl_seconds = ttl_hours * 3600.0
        self._cleanup_seconds = cleanup_days * 24 * 3600.0
        self._clock = clock

    def cache_path(self) -> Path:
        today = datetime.fromtimestamp(
            self._clock(), tz=UTC,
        ).date().isoformat()
        return self._dir / f"{self._prefix}{today}{self._suffix}"

    def load(
        self, download: Download, *, force_refresh: bool = False,
    ) -> bytes:
        """Serve today's cache if present; otherwise download and persist."""
        self._dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_old()
        path = self.cache_path()
        if (
            not force_refresh
            and path.exists()
            and path.stat().st_size > 0
        ):
            # Today's file is the day's cache — no age gate, no same-day
            # re-download (master publishes once per day; a mid-day restart
            # must reuse it, not re-fetch a 25 MB CSV).
            return path.read_bytes()
        content = download()
        if isinstance(content, str):
            content = content.encode("utf-8")
        if not isinstance(content, bytes) or not content:
            raise SDKError("instrument master download was empty")
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_bytes(content)
        os.replace(tmp_path, path)
        return content

    def _cleanup_old(self) -> None:
        cutoff = self._clock() - self._cleanup_seconds
        for candidate in self._dir.glob(f"{self._prefix}*{self._suffix}"):
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except OSError:
                continue


class MasterLoader:
    """Rows loader with an optional on-disk cache."""

    def __init__(
        self,
        download: Download,
        parse: ParseRaw,
        *,
        cache: MasterFileCache | None = None,
    ) -> None:
        self._download = download
        self._parse = parse
        self._cache = cache

    def load(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        if self._cache is not None:
            raw = self._cache.load(
                self._download, force_refresh=force_refresh,
            )
        else:
            raw = self._download()
        return self._parse(raw)


class InstrumentRefreshScheduler:
    """Daemon thread that refreshes the instrument master daily."""

    def __init__(
        self,
        broker_id: str,
        refresh: Callable[[], None],
        *,
        interval_seconds: float = _SCHEDULER_DEFAULT_INTERVAL,
    ) -> None:
        self.broker_id = broker_id
        self._refresh = refresh
        self._interval = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._refresh_count = 0
        self._error_count = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"master-refresh-{self.broker_id}",
        )
        self._thread.start()

    def stop(self, timeout_seconds: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_seconds)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def refresh_now(self) -> bool:
        try:
            self._refresh()
            self._refresh_count += 1
            return True
        except Exception:  # noqa: BLE001
            self._error_count += 1
            return False

    @property
    def refresh_count(self) -> int:
        return self._refresh_count

    @property
    def error_count(self) -> int:
        return self._error_count

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            self.refresh_now()


__all__ = [
    "DEFAULT_MASTER_CLEANUP_DAYS",
    "DEFAULT_MASTER_TTL_HOURS",
    "InstrumentRefreshScheduler",
    "MasterFileCache",
    "MasterLoader",
]
