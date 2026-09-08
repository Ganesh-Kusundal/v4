"""Sync orchestrator: thin facade over ParallelHistoryFetcher + GapDetector.

Fetch/chunk/rate-limit/failover live in the fetcher; skip-complete logic in
the gap detector. This module only converts and persists in batches.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from tradex_domain import Equity, Timeframe
from tradex_domain.market import HistoricalSeries

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    requested: int
    fetched: int
    written: int
    failed: list[str]
    errors: list[str] = field(default_factory=list)


def series_to_frame(series: HistoricalSeries, symbol: str) -> pd.DataFrame:
    """Convert HistoricalSeries to storage DataFrame (tz-naive IST)."""
    if not series.candles:
        return pd.DataFrame()
    from tradex_domain.market_calendar import to_ist_naive

    rows = []
    for c in series.candles:
        ts = c.timestamp
        # ponytail: attach UTC only when naive; aware stamps convert as-is
        # (old code used replace(tzinfo=UTC) which corrupted aware non-UTC ts)
        ts = to_ist_naive(ts if ts.tzinfo is not None else ts.replace(tzinfo=_utc()))
        rows.append({
            "symbol": symbol,
            "exchange": c.instrument.exchange.value if hasattr(c.instrument, "exchange") else "NSE",
            "kind": "equity",
            "timeframe": str(c.timeframe.value),
            "timestamp": ts,
            "open": float(c.ohlc.open.value),
            "high": float(c.ohlc.high.value),
            "low": float(c.ohlc.low.value),
            "close": float(c.ohlc.close.value),
            "volume": float(c.volume.value) if c.volume else 0.0,
        })
    return pd.DataFrame(rows)


def _utc():
    from datetime import UTC

    return UTC


def _bar_freq(timeframe: Timeframe | str) -> str:
    """Map a Timeframe to a GapDetector bar_freq."""
    tf = str(timeframe.value if isinstance(timeframe, Timeframe) else timeframe)
    return {"1m": "1min", "5m": "5min", "15m": "15min"}.get(tf, "1min")


class SyncOrchestrator:
    """Gap-aware sync facade: detect → fetch → convert → batched upsert.

    Parameters
    ----------
    store : ParquetStorage
        Backing store (upsert is idempotent).
    fetcher : ParallelHistoryFetcher
        Serves history with split routing, auto-chunking, rate limiting,
        K-failure blacklist and failover.
    gaps : GapDetector | None
        When set and ``skip_existing`` is true, complete symbols are
        skipped and partial ones fetch only missing ``ranges``.
    """

    def __init__(self, store: Any, fetcher: Any, gaps: Any = None) -> None:
        self.store = store
        self.fetcher = fetcher
        self.gaps = gaps

    def sync(
        self,
        universe: str | list[Any],
        timeframe: str,
        start: Any,
        end: Any,
        *,
        skip_existing: bool = True,
        min_gap_stamps: int = 15,
        batch_size: int = 20,
        backoff_base: float = 15.0,
        backoff_max: float = 300.0,
    ) -> SyncResult:
        """Sync one universe/timeframe for the given window."""
        import time

        from tradex_trading.datalake.parallel_fetcher import fetch_with_backoff

        instruments = universe if isinstance(universe, list) else _load_universe(universe)
        tf = Timeframe(timeframe) if isinstance(timeframe, str) else timeframe
        if not instruments:
            return SyncResult(0, 0, 0, [], [])

        to_fetch, ranges = self._plan(instruments, tf, start, end, skip_existing, min_gap_stamps)
        if not to_fetch:
            log.info("sync: all %d symbols complete — nothing to fetch", len(instruments))
            return SyncResult(len(instruments), 0, 0, [], [])

        fetched = written = 0
        failed: list[str] = []
        errors: list[str] = []
        dead: set[str] = set()
        by_id = {_key(i): i for i in to_fetch}
        batches = [to_fetch[i:i + batch_size] for i in range(0, len(to_fetch), batch_size)]
        backoff = float(backoff_base)
        for bi, batch in enumerate(batches, 1):
            batch_ranges = {k: v for k, v in (ranges or {}).items()
                            if k in {_key(i) for i in batch}}
            results = fetch_with_backoff(
                self.fetcher, batch, tf, start, end,
                ranges=batch_ranges or None, dead_symbols=dead,
                max_retries=3,
            )
            frames: list[pd.DataFrame] = []
            for inst_id, series in results.items():
                inst = by_id.get(inst_id)
                sym = inst.symbol if inst is not None else inst_id.split(":")[-1]
                df = series_to_frame(series, sym)
                if df.empty:
                    dead.add(inst_id)
                    continue
                frames.append(df)
                fetched += 1
            if frames:
                written += self.store.upsert(pd.concat(frames, ignore_index=True))
                backoff = float(backoff_base)
            missing = [i for i in batch if _key(i) not in results]
            if missing:
                # ponytail: empty/partial batch usually means 429s — back off
                # and let the next batch drain the recovering quota window.
                log.warning("sync: batch %d/%d partial %d/%d — backing off %.0fs",
                            bi, len(batches), len(batch) - len(missing), len(batch), backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, float(backoff_max))
            # Reconcile: fetch_with_backoff already retried transients, so
            # anything still missing is failed for this run.
            failed.extend(i.symbol for i in missing)
            log.info("sync: batch %d/%d done (fetched=%d written=%d)",
                     bi, len(batches), fetched, written)

        if failed:
            log.warning("sync: %d failed: %s", len(failed), failed[:5])
        log.info("sync: %d/%d fetched, %d rows", fetched, len(instruments), written)
        return SyncResult(len(instruments), fetched, written, failed, errors)

    def sync_today(
        self,
        universe: str | list[Any],
        timeframe: str = "1m",
    ) -> SyncResult:
        """Sync only today's bars (09:15-now IST)."""
        from datetime import UTC, datetime

        from tradex_domain.market_calendar import MARKET_OPEN, to_ist_naive

        now = to_ist_naive(datetime.now(UTC)).replace(second=0, microsecond=0)
        day_start = now.replace(
            hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
            second=0, microsecond=0,
        )
        if now <= day_start:
            log.info("pre-open — nothing to sync today")
            return SyncResult(0, 0, 0, [], [])

        instruments = universe if isinstance(universe, list) else _load_universe(universe)
        return self.sync(instruments, timeframe, day_start, now)

    # ------------------------------------------------------------------ internal

    def _plan(self, instruments, tf, start, end, skip_existing, min_gap_stamps):
        if not skip_existing or self.gaps is None:
            return list(instruments), {}
        try:
            hits = self.gaps.detect(
                instruments, start=start, end=end,
                timeframe=str(tf.value), bar_freq=_bar_freq(tf),
                min_gap_stamps=min_gap_stamps,
            )
        except Exception as e:
            log.warning("gap detect failed (%s) — full fetch", e)
            return list(instruments), {}
        ranges = {_key(inst): r for inst, r in hits if r}
        return [i for i in instruments if _key(i) in ranges], ranges


def _key(inst: Any) -> str:
    return str(inst.instrument_id)


def _load_universe(name: str) -> list[Equity]:
    """Lazy import to avoid circular deps."""
    from tradex_trading.datalake.universe import load_universe
    return load_universe(name)
