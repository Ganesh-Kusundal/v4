"""HistoricalSyncService — complete gap-aware continuous sync over ParquetStorage.

Orchestrates the existing primitives:

* GapDetector (session-aware, holiday-aware, IPO-clipped)
* ParallelHistoryFetcher (chunk-aware, rate-limited, bounded failover)
* ParquetStorage (atomic upsert, OHLC-validated, hive-partitioned)

This is the historical “data sync module” callers should use instead of
hand-wiring fetcher+store.  It adds:

* Three-phase sync: primary backfill (all brokers) + filler top-up (Upstox)
  for residual gaps + same-day top-up (Dhan, since Upstox's historical
  endpoint excludes the current session). The pattern
  ``backfill_parquet.py`` + ``topup_gaps.py`` previously required multiple
  manual script invocations.
* Incremental ranged fetching: only GapDetector's missing sub-windows are
  requested per symbol — a 20-minute hole costs one small poll, not a
  re-pull of the whole trailing window.
* Failure blacklist: symbols still gapped after a run are skipped for
  ``BLACKLIST_COOLDOWN_DAYS`` and reported in :class:`SyncResult`, so dead
  symbols don't slow every subsequent sync.
* Post-sync verification: GapDetector re-run (gaps before vs remaining)
  + OHLC sanity sample.
* Survivorship-aware instrument resolution (delisted filter) via
  ParquetBacktestLoader semantics.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from tradex_domain import Timeframe
from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.market_calendar import NSE_HOLIDAYS_2026, to_ist_naive

from tradex_trading.datalake.gap_detector import GapDetector
from tradex_trading.datalake.parallel_fetcher import (
    ParallelHistoryFetcher,
    _default_workers,
    fetch_with_backoff,
)
from tradex_trading.datalake.parquet_storage import ParquetStorage
from tradex_trading.datalake.universe import load_universe

log = logging.getLogger(__name__)

#: Symbols that stayed gapped after a full sync are skipped for this many
#: days (retried automatically afterwards), so repeated dead-symbol failures
#: don't slow every run.  Entries are pruned as soon as a verification finds
#: the symbol gap-free again.
BLACKLIST_COOLDOWN_DAYS = 7
_DEFAULT_BLACKLIST_PATH = Path("data") / ".sync_blacklist.json"


def _load_blacklist(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_blacklist(path: Path, blacklist: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(blacklist, indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(tmp, path)  # atomic: readers never see a half-written file


def _serves_same_day(broker: Any) -> bool:
    """True when the broker's capability table claims same-day intraday M1."""
    caps = getattr(broker, "_capabilities", None)
    return isinstance(caps, BrokerCapabilities) and caps.supports_same_day_intraday


def _active_blacklist(
    blacklist: dict[str, dict[str, Any]], now: datetime,
) -> dict[str, dict[str, Any]]:
    """Entries younger than the cooldown; older ones have expired."""
    active: dict[str, dict[str, Any]] = {}
    for sym, entry in blacklist.items():
        try:
            last = datetime.fromisoformat(str(entry.get("last_ts")))
        except ValueError:
            continue
        if (now - last).days < BLACKLIST_COOLDOWN_DAYS:
            active[sym] = entry
    return active


def _series_to_frame(series, symbol: str) -> pd.DataFrame:
    rows = []
    for c in series.candles:
        rows.append({
            "symbol": symbol,
            "exchange": str(c.instrument.exchange),
            "kind": "equity",
            "timeframe": str(c.timeframe.value),
            "timestamp": to_ist_naive(c.timestamp),
            "open": float(c.ohlc.open.value),
            "high": float(c.ohlc.high.value),
            "low": float(c.ohlc.low.value),
            "close": float(c.ohlc.close.value),
            "volume": float(c.volume.value),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _cluster_gaps(
    gap_pairs: list[tuple],
) -> dict[tuple[datetime, datetime], list]:
    """Group missing ranges into day-span clusters (fill_gaps semantics)."""
    clusters: dict[tuple[datetime, datetime], list] = defaultdict(list)
    seen: dict[tuple[datetime, datetime], set[str]] = defaultdict(set)
    for inst, ranges in gap_pairs:
        for gs, ge in ranges:
            key = (
                datetime.combine(gs.date(), datetime.min.time()),
                datetime.combine(ge.date(), datetime.min.time())
                + timedelta(days=1),
            )
            if inst.symbol not in seen[key]:
                clusters[key].append(inst)
                seen[key].add(inst.symbol)
    return clusters


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Outcome of one sync run.

    ``gaps_before`` / ``gaps_remaining`` bracket the run so callers can see
    how much of the gap was closed; ``blacklisted`` lists symbols skipped
    because they recently failed outright; ``failed`` lists every symbol the
    final verification still finds gapped (blacklisted ones included).
    """

    requested: int
    fetched: int
    written: int
    gaps_remaining: int
    failed: list[str]
    gaps_before: int = 0
    blacklisted: list[str] = field(default_factory=list)


class HistoricalSyncService:
    """Gap-aware, incremental, three-phase historical sync with verification.

    Each run only requests what GapDetector says is missing (per-symbol
    sub-windows, not the whole trailing window), splits work across all
    configured brokers, tops residual gaps up from a filler broker, and
    same-day bars from a broker serving live-day M1.

    Parameters
    ----------
    store : ParquetStorage | None
        Backing store (defaults to ``ParquetStorage("data/")``).
    detector : GapDetector | None
        Gap detector (defaults to ``GapDetector(store)``).
    fetcher : ParallelHistoryFetcher | None
        Fetcher for the primary phase.  When ``None`` the service builds
        one from the supplied ``brokers`` dict on each ``sync()`` call so
        callers can inject per-run broker sets.
    blacklist_path : Path | None
        JSON file of symbols that repeatedly stayed gapped after a sync;
        they are skipped for ``BLACKLIST_COOLDOWN_DAYS`` so dead symbols
        don't slow every run (defaults to ``data/.sync_blacklist.json``).
    holidays : frozenset | None
        NSE holiday dates (ISO date strings) for gap detection. Defaults to
        the newest list in ``tradex_domain.market_calendar``. ponytail: a
        single injected set — pass the union of known years when a sync
        window spans year boundaries.
    """

    def __init__(
        self,
        store: ParquetStorage | None = None,
        detector: GapDetector | None = None,
        fetcher: ParallelHistoryFetcher | None = None,
        blacklist_path: Path | None = None,
        holidays: frozenset | None = None,
    ) -> None:
        self._store = store or ParquetStorage(Path("data"))
        self._detector = detector or GapDetector(self._store)
        self._fetcher = fetcher
        self._blacklist_path = blacklist_path or _DEFAULT_BLACKLIST_PATH
        self._holidays = holidays or NSE_HOLIDAYS_2026

    # ------------------------------------------------------------------ public

    def sync(
        self,
        universe: str = "nifty500",
        timeframe: str | Timeframe = "1m",
        months: int = 3,
        brokers: dict[str, Any] | None = None,
        filler_broker: str | None = "upstox",
        batch_size: int = 20,
        workers: int | None = None,
        min_gap_stamps: int = 15,
        verify: bool = True,
        tail_days: int | None = 7,
        gap_workers: int = 8,
    ) -> SyncResult:
        """Sync one universe/timeframe for the trailing ``months`` window.

        Three phases (all gap-aware):
          1. Primary fetch (all brokers via ``ParallelHistoryFetcher``) —
             covers bulk history with Dhan 90d chunks / Upstox 30d caps.
          2. Filler top-up (``filler_broker`` only) for residual gaps —
             re-fetches only gapped symbols' full window from the filler.
          3. Same-day top-up (a broker serving live-day M1, currently Dhan)
             — Upstox's historical endpoint excludes the current session.

        When *brokers* is ``None`` the service builds brokers from env
        (``build_broker_from_env``); tests inject a dict directly.
        """
        tf = Timeframe(timeframe) if isinstance(timeframe, str) else timeframe
        # Session-aligned bounds in IST-naive wall clock (the storage contract),
        # regardless of host timezone; midnight start, minute-floored end.
        end = to_ist_naive(datetime.now(UTC)).replace(second=0, microsecond=0)
        start = (end - timedelta(days=months * 30)).replace(
            hour=0, minute=0, second=0, microsecond=0)

        instruments = load_universe(universe)
        log.info("HistoricalSync: %s %s %s->%s (%d instruments)",
                 universe, tf, start.date(), end.date(), len(instruments))

        # --- Blacklist gate: recently failed symbols skip this run ---
        bl_all = _load_blacklist(self._blacklist_path)
        bl_active = _active_blacklist(bl_all, end)
        blacklisted_names = sorted(bl_active)
        active_insts = [i for i in instruments if i.symbol not in bl_active]
        if bl_active:
            log.warning(
                "HistoricalSync: skipping %d blacklisted symbol(s) "
                "(cooldown %dd): %s", len(bl_active),
                BLACKLIST_COOLDOWN_DAYS, blacklisted_names[:10],
            )

        # --- Phase 1: gap-aware primary fetch (missing sub-windows only) ---
        if brokers is None:
            brokers = self._build_brokers_from_env()

        worker_count = workers if workers is not None else _default_workers(
            list(brokers.keys())
        )

        gap_pairs = self._gapped_pairs(
            active_insts, start, end, str(tf.value), min_gap_stamps,
            tail_days=tail_days, gap_workers=gap_workers,
        )
        to_fetch = [inst for inst, _ in gap_pairs]
        gaps_before = len(to_fetch)
        log.info("HistoricalSync phase 1: %d/%d need data",
                 gaps_before, len(active_insts))

        written1 = self._fetch_clusters(
            gap_pairs, brokers or {}, tf, start, end,
            batch_size, worker_count, now=end,
        )

        # --- Phase 2: filler top-up for residual gaps (ranged) ---
        written2 = 0
        if filler_broker and filler_broker in (brokers or {}):
            gap_pairs2 = self._gapped_pairs(
                to_fetch, start, end, str(tf.value), min_gap_stamps,
                gap_workers=gap_workers,
            )
            if gap_pairs2:
                log.info("HistoricalSync phase 2 (filler %s): %d gapped symbols",
                         filler_broker, len(gap_pairs2))
                written2 = self._fetch_clusters(
                    gap_pairs2,
                    {filler_broker: brokers[filler_broker]},
                    tf, start, end, batch_size, worker_count,
                    broker_pref=[filler_broker], now=end,
                )

        # --- Phase 3: same-day top-up via a broker that serves live-day M1 ---
        written3 = 0
        same_day = [
            name for name, broker in (brokers or {}).items()
            if name != filler_broker and _serves_same_day(broker)
        ]
        if same_day:
            now = to_ist_naive(datetime.now(UTC))
            day_start = now.replace(hour=9, minute=0, second=0, microsecond=0)
            gaps_today = (
                self._gapped_pairs(
                    to_fetch, day_start, now, str(tf.value), min_gap_stamps,
                    gap_workers=gap_workers,
                )
                if now > day_start else []
            )
            if gaps_today:
                log.info("HistoricalSync phase 3 (same-day via %s): %d symbols "
                         "missing today", same_day[0], len(gaps_today))
                sd_brokers = {same_day[0]: brokers[same_day[0]]}
                written3 = self._fetch_clusters(
                    gaps_today, sd_brokers, tf, day_start, now,
                    batch_size, worker_count,
                    broker_pref=same_day, now=now,
                )

        # --- Verification + failure bookkeeping ---
        gaps_remaining = 0
        failed: list[str] = []
        if verify:
            verify_insts = to_fetch if to_fetch else active_insts
            gaps_after = self._gapped_pairs(
                verify_insts, start, end, str(tf.value), min_gap_stamps,
                gap_workers=gap_workers,
            )
            gaps_remaining = len(gaps_after)
            failed = [inst.symbol for inst, _ in gaps_after]
            if gaps_remaining:
                log.warning("HistoricalSync: %d symbols still gapped after sync: %s",
                            gaps_remaining, failed[:5])
            else:
                log.info("HistoricalSync: verification clean — no gaps")
            self._update_blacklist(bl_active, failed, end)

        return SyncResult(
            requested=len(instruments),
            fetched=len(to_fetch),
            written=written1 + written2 + written3,
            gaps_before=gaps_before,
            gaps_remaining=gaps_remaining,
            failed=failed,
            blacklisted=blacklisted_names,
        )

    def verify(
        self,
        universe: str = "nifty500",
        timeframe: str = "1m",
        months: int = 3,
    ) -> dict[str, Any]:
        """Read-only verification: gaps + OHLC sanity + row counts."""
        tf = Timeframe(timeframe) if isinstance(timeframe, str) else timeframe
        end = to_ist_naive(datetime.now(UTC)).replace(second=0, microsecond=0)
        start = (end - timedelta(days=months * 30)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        instruments = load_universe(universe)
        gaps = self._detector.detect(
            instruments, start=start, end=end,
            timeframe=str(tf.value), bar_freq="1min",
            holidays=self._holidays, min_gap_stamps=15,
        )
        # Sample OHLC sanity: read one symbol's recent bars and check invariants
        sample = self._store.read(symbols=[instruments[0].symbol], start=start, end=end)
        bad = 0
        if not sample.empty:
            bad = int(((sample["high"] < sample["open"]) |
                       (sample["high"] < sample["close"]) |
                       (sample["low"] > sample["open"]) |
                       (sample["low"] > sample["close"])).sum())
        return {
            "universe": universe,
            "timeframe": str(tf.value),
            "requested": len(instruments),
            "gapped_symbols": len(gaps),
            "ohlc_bad_rows": bad,
            "total_rows": len(sample),
        }

    # ------------------------------------------------------------------ helpers

    def _gapped_pairs(
        self, instruments, start, end, timeframe, min_gap_stamps,
        *, tail_days: int | None = None, gap_workers: int = 1,
    ):
        """GapDetector run -> ``[(inst, missing_ranges)]`` for gapped symbols."""
        return self._detector.detect(
            instruments, start=start, end=end,
            timeframe=timeframe, bar_freq="1min",
            holidays=self._holidays, min_gap_stamps=min_gap_stamps,
            max_workers=gap_workers, tail_days=tail_days,
        ) or []

    def _update_blacklist(
        self,
        bl_active: dict[str, dict[str, Any]],
        still_gapped: list[str],
        now: datetime,
    ) -> None:
        """Bump entries for symbols that keep failing; prune recovered ones.

        A symbol still gapped after a full run gets/bumps an entry so the
        next run skips it for ``BLACKLIST_COOLDOWN_DAYS``; a blacklisted
        symbol the verification finds gap-free is pruned immediately.
        """
        gapped_now = set(still_gapped)
        updated: dict[str, dict[str, Any]] = {}
        for sym, entry in bl_active.items():
            if sym in gapped_now:
                updated[sym] = {
                    "fails": int(entry.get("fails", 0)) + 1,
                    "last_ts": now.isoformat(),
                    "reason": entry.get("reason", "still_gapped_after_sync"),
                }
            # else: recovered during cooldown -> pruned by omission
        fresh = [s for s in still_gapped if s not in bl_active]
        for sym in fresh:
            updated[sym] = {
                "fails": 1,
                "last_ts": now.isoformat(),
                "reason": "still_gapped_after_sync",
            }
        if fresh:
            log.warning(
                "HistoricalSync: blacklisting %d symbol(s) after failed "
                "top-up: %s", len(fresh), sorted(fresh)[:10],
            )
        # ponytail: unconditional rewrite keeps prune/bump logic trivial.
        _save_blacklist(self._blacklist_path, updated)

    def _fetch_clusters(
        self,
        gap_pairs: list[tuple],
        brokers: dict[str, Any],
        tf: Timeframe,
        start: datetime,
        end: datetime,
        batch_size: int,
        workers: int,
        *,
        broker_pref: list[str] | None = None,
        now: datetime | None = None,
    ) -> int:
        if not gap_pairs or not brokers:
            return 0
        now = now or end
        clusters = _cluster_gaps(gap_pairs)
        total = 0
        dead_symbols: set[str] = set()
        for (c_start, c_end), insts in sorted(
            clusters.items(), key=lambda kv: len(kv[1]), reverse=True,
        ):
            fetch_end = min(c_end - timedelta(seconds=1), end)
            if broker_pref:
                names = [n for n in broker_pref if n in brokers]
            elif c_end.date() >= now.date():
                names = [
                    n for n in brokers
                    if n == "dhan" or _serves_same_day(brokers[n])
                ] or list(brokers.keys())
            else:
                names = (
                    ["upstox"] if "upstox" in brokers else list(brokers.keys())
                )
            chosen = {n: brokers[n] for n in names if n in brokers} or brokers
            if self._fetcher is not None:
                fetcher = self._fetcher
            else:
                w = workers if workers else _default_workers(list(chosen.keys()))
                fetcher = ParallelHistoryFetcher(chosen, max_workers=w)
            log.info(
                "HistoricalSync cluster %s->%s: %d symbols via %s",
                c_start.date(), fetch_end.date(), len(insts), list(chosen),
            )
            for i in range(0, len(insts), batch_size):
                batch = insts[i:i + batch_size]
                results = fetch_with_backoff(
                    fetcher, batch, tf, c_start, fetch_end,
                    dead_symbols=dead_symbols,
                )
                frames = []
                for inst_id, series in results.items():
                    sym = inst_id.split(":")[-1] if ":" in inst_id else inst_id
                    df = _series_to_frame(series, sym)
                    if not df.empty:
                        frames.append(df)
                if frames:
                    total += self._store.upsert(
                        pd.concat(frames, ignore_index=True),
                    )
        return total

    def _fetch_and_upsert(self, fetcher, instruments, tf, start, end, batch_size,
                          ranges=None) -> int:
        if not instruments:
            return 0
        total = 0
        batches = [instruments[i:i + batch_size] for i in range(0, len(instruments), batch_size)]
        for batch in batches:
            results = fetch_with_backoff(
                fetcher, batch, tf, start, end, ranges=ranges,
            )
            frames = []
            for inst_id, series in results.items():
                sym = inst_id.split(":")[-1] if ":" in inst_id else inst_id
                df = _series_to_frame(series, sym)
                if not df.empty:
                    frames.append(df)
            if frames:
                total += self._store.upsert(pd.concat(frames, ignore_index=True))
        return total

    def _build_brokers_from_env(self) -> dict[str, Any]:
        from tradex_trading.config.env import load_env_file
        from tradex_trading.runtime.live import build_broker_from_env

        root = Path(__file__).resolve().parents[4]
        load_env_file(str(root / ".env.local"))
        brokers: dict[str, Any] = {}
        for name in ("dhan", "upstox"):
            try:
                b = build_broker_from_env(name)
                b.connect()
                brokers[name] = b
            except Exception as exc:
                log.warning("HistoricalSync: broker %s unavailable (%s)", name, exc)
        return brokers


__all__ = ["HistoricalSyncService", "SyncResult"]
