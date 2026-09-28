"""ParquetStorage — Hive-partitioned Parquet store with upsert semantics.

Layout: ``base_path/ohlcv/symbol={SYMBOL}/year={YYYY}/month={MM}/data.parquet``

**Storage contract:**

- Timestamps: tz-naive IST wall time (no timezone info)
- Volume: int64 (0 = unknown/missing volume)
- Session hours only: 09:15–15:30 IST, weekdays only (weekends/holidays dropped)
- Dedup semantics: for matching (symbol, timeframe, timestamp), new data replaces old

Partition pruning keeps reads fast: ``read(symbols, start, end)`` only scans
the symbol + year/month partitions that overlap the requested range.

**Predicate pushdown (lane 3e).** Partition pruning is directory-level and
stops at the month: a request for the last week of March still decodes every
row group in ``month=03/data.parquet`` and throws 95% of the rows away in
pandas. ``read()`` now builds a timestamp predicate and hands it to the
parquet reader, so row groups whose statistics fall entirely outside the
window are skipped before any page is decompressed. The predicate is
intentionally loose and inclusive on both ends, so the read stays
*superset-shaped* and the caller's own filters are still applied — the rows
returned are bit-identical to what the old path returned, only fewer are
decoded to produce them.

Upsert is idempotent: re-upserting the same (symbol, timeframe, timestamp)
replaces old rows instead of duplicating them. Concurrent upserts to the
same partition are serialized via per-partition locks.

**Corrupt files are quarantined, not raised (AC-15).** A truncated or
otherwise unreadable ``data.parquet`` used to make ``pyarrow.ArrowInvalid``
propagate out of every read to every call site — chart, scanner, backtest,
replay, recovery — so one bad file made that symbol unreadable everywhere,
permanently, and there was no operator signal. Every parquet read now goes
through :meth:`ParquetStorage._read_partition`, which catches the decode
errors, moves the file to ``<base>/quarantine/<mtime>-<symbol>.parquet``,
increments ``datalake_quarantine_total{symbol,file}`` (the counter
``deploy/prometheus-rules.yml`` pages on) and reports the partition as
absent. The affected symbol therefore reads as an empty series, the other
symbols are unaffected, and the data team is paged. On the two write paths
a quarantined partition is replaced by the incoming rows, so the lake
rebuilds itself on the next backfill. The filtered read goes through the
same chokepoint — it raises ``ArrowInvalid`` from the same call site, so the
quarantine guard covers it without a second ``try``.

Adapted from nTrade's ParquetStorage.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

# ponytail: pyarrow is already installed (ParquetDataCatalog depends on it).
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from tradex_domain.market_calendar import MARKET_CLOSE, MARKET_OPEN

if TYPE_CHECKING:  # pragma: no cover — import cycle avoidance only
    from tradex_observability.metrics import MetricsRegistry

log = logging.getLogger(__name__)

_BASE_COLUMNS = [
    "symbol", "exchange", "kind", "timeframe", "timestamp",
    "open", "high", "low", "close", "volume",
]

#: The one column the read path pushes down (lane 3e). Every range query on
#: the lake is a time range, and the Hive layout already prunes symbol and
#: year/month, so timestamp is the one column whose per-row-group statistics
#: are worth handing to the reader. ``timeframe`` is deliberately NOT pushed
#: down: a file holds one timeframe today, so pushing it would add a
#: predicate that cannot prune anything and would give a caller a way to
#: ask for a silently-empty read by typo'ing the string.
_TIMESTAMP_COLUMN = "timestamp"

#: Local filesystem used to build one-file fragments. Module-level because it
#: is stateless and constructing it per partition was measurable overhead in
#: a 500-symbol scan for no benefit.
_local_fs = pa.fs.LocalFileSystem()

#: Parquet read failures that mean "this file is unreadable" and must be
#: quarantined, rather than "this process is in trouble".
#:
#: ``ArrowInvalid`` is the one AC-15 names, and it is what a truncated, empty
#: or garbage-bodied file raises; the other Arrow decode errors are the shapes
#: a file written by a different/broken writer produces instead, and all of
#: them have the same consequence for the lake. ``OSError`` covers
#: ``ArrowIOError`` (pyarrow's I/O errors subclass it) and a file that vanished
#: between the ``exists()`` check and the read.
#:
#: ``ArrowMemoryError`` is deliberately absent: it IS an ``ArrowException``,
#: but quarantining a healthy file because the box was short on RAM would
#: destroy good data to hide a transient.
_CORRUPT_FILE_ERRORS: tuple[type[BaseException], ...] = (
    pa.ArrowInvalid,
    pa.ArrowNotImplementedError,
    pa.ArrowTypeError,
    pa.ArrowSerializationError,
    OSError,
)

#: The quarantine counter. This exact name is the one
#: ``deploy/prometheus-rules.yml`` alerts on (``TradeXDatalakeQuarantine``,
#: ``increase(datalake_quarantine_total[15m]) > 0``), so renaming it silently
#: de-fuses a live page. It is a legal Prometheus name, so ``_sanitized``
#: leaves it exactly as written here. Labels are ``symbol`` and ``file``,
#: exactly as the spec states.
_QUARANTINE_METRIC = "datalake_quarantine_total"

#: Guards quarantine target-name allocation so two threads quarantining two
#: different corrupt files for the same symbol in the same second cannot
#: resolve to one destination and one of the files be overwritten.
_quarantine_lock = threading.Lock()

# ponytail: calendar constants single-sourced (domain/market_calendar.py); keep
# local aliases so this module's grep for _MARKET_OPEN still hits.
_MARKET_OPEN = MARKET_OPEN
_MARKET_CLOSE = MARKET_CLOSE


def market_session_mask(ts: pd.Series) -> pd.Series:
    """Boolean mask: timestamps that are NSE weekday-session bars.

    True for Mon–Fri 09:15–15:30 IST (both bounds inclusive), False otherwise.
    Single source for the session rule so the write path, read path and
    ``clean_datalake`` agree exactly. ponytail: the store is equity-only today
    (``kind=\"equity\"``); when futures/currency land here, extend per-exchange
    hours (see ``DHAN_SESSION_*``) instead of broadening this mask.
    """
    t = ts.dt.time
    return (ts.dt.dayofweek < 5) & (t >= _MARKET_OPEN) & (t <= _MARKET_CLOSE)


class ParquetStorage:
    """Hive-partitioned Parquet store for OHLCV history.

    ``base_path`` is the root directory; the store creates
    ``base_path/ohlcv/symbol=.../year=.../month=.../`` as needed.
    """

    # Per-partition locks serialize concurrent upserts to the same file.
    # Class-level so all instances share the same lock for a given partition key.
    _partition_locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(
        self,
        base_path: str | Path,
        metrics: MetricsRegistry | None = None,
    ):
        self.base_path = Path(base_path)
        # ponytail: if base_path already ends with 'ohlcv', use it directly
        if self.base_path.name == "ohlcv":
            self._ohlcv_root = self.base_path
        else:
            self._ohlcv_root = self.base_path / "ohlcv"
        self._ohlcv_root.mkdir(parents=True, exist_ok=True)
        #: Optional AC-15 metrics registry. Duck-typed exactly as
        #: ``FeeCalculator``/``ExecutionEngine`` type theirs: anything exposing
        #: ``counter(name) -> .inc(**labels)`` works, and ``None`` is normal
        #: (scripts, the chart route's cached store) rather than a bug.
        self._metrics = metrics
        #: Siblings of ``_ohlcv_root`` under the same base path, so the
        #: quarantine directory sits at ``<base>/quarantine`` — which is
        #: ``data/quarantine`` for the production root
        #: (``ParquetStorage("data")``) and lands inside ``tmp_path`` for a
        #: test, with nothing to patch and no chance of a test writing into
        #: the real lake. A base path already named ``ohlcv`` puts it at
        #: ``<ohlcv>/../quarantine``, i.e. beside the store it protects.
        self._quarantine_root = self._ohlcv_root.parent / "quarantine"

    @classmethod
    def _get_partition_lock(cls, partition_key: str) -> threading.Lock:
        """Return the lock for *partition_key*, creating it if needed."""
        with cls._locks_guard:
            if partition_key not in cls._partition_locks:
                cls._partition_locks[partition_key] = threading.Lock()
            return cls._partition_locks[partition_key]

    # ------------------------------------------------------------------ quarantine

    def _partition_symbol(self, partition_dir: Path) -> str:
        """The symbol a Hive partition directory belongs to.

        Read off the ``symbol=`` path segment rather than out of the file,
        because the file is unreadable — that is the only situation this is
        ever called in. Falls back to ``"unknown"`` if a caller ever hands in
        a path outside the Hive layout, so the metric is still emitted with a
        usable value instead of a ``KeyError`` replacing the ``ArrowInvalid``.
        """
        for part in reversed(partition_dir.parts):
            if part.startswith("symbol="):
                return part.split("=", 1)[1]
        return "unknown"

    def _quarantine_name(self, path: Path, symbol: str) -> str:
        """Allocate a free ``<mtime>-<symbol>.parquet`` name under the lock.

        The name is the file's mtime rendered as a compact UTC stamp,
        ``YYYYmmddTHHMMSSZ``, for two reasons. It sorts lexicographically in
        chronological order, so ``ls data/quarantine`` reads as a timeline
        without a sort. And it is derived from the corrupt file itself, not
        from the clock, so two quarantines of the same file at different times
        are distinguishable, and a replayed test is deterministic when the
        mtime is pinned.

        The lock is held across the existence check *and* the subsequent move
        so a concurrent quarantine of a second corrupt file for the same
        symbol cannot pick the same free name. That is the one case where
        plain ``os.replace`` would silently destroy the earlier file.
        """
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        prefix = f"{stamp.strftime('%Y%m%dT%H%M%SZ')}-{symbol}"
        with _quarantine_lock:
            candidate = self._quarantine_root / f"{prefix}.parquet"
            n = 0
            while candidate.exists():
                n += 1
                candidate = self._quarantine_root / f"{prefix}-{n}.parquet"
            return candidate.name

    def quarantine(
        self, path: Path, error: BaseException, *, symbol: str | None = None
    ) -> str:
        """Move an unreadable parquet file out of the lake and count it.

        *path* is a partition's ``data.parquet`` and *error* the decode
        exception it raised. Returns the quarantine file's name.

        This never raises. The whole point of quarantining is that the caller
        cannot go on reading the file, so a failure to move it (a read-only
        mount, a full disk, a race with another thread that already took the
        file) must not replace ``ArrowInvalid`` with an ``OSError`` and defeat
        the guard. On such a failure the file is left where it is, the error
        is logged, and the counter still fires — a file that stays put and
        keeps failing is a louder signal than a silent skip, because the next
        read quarantines (or re-reports) it again.

        The counter is emitted even when the move fails, and before the move,
        so a quarantine is observable whether or not the file could be moved.
        """
        symbol = symbol or self._partition_symbol(path.parent)
        try:
            file_label = str(path.relative_to(self._ohlcv_root.parent))
        except ValueError:  # a path outside this store; label it as given
            file_label = str(path)

        if self._metrics is not None:
            try:
                self._metrics.counter(_QUARANTINE_METRIC).inc(
                    symbol=symbol, file=file_label
                )
            except Exception:  # noqa: BLE001 — a metric must never break a read
                log.exception("quarantine: could not publish %s", _QUARANTINE_METRIC)

        try:
            self._quarantine_root.mkdir(parents=True, exist_ok=True)
            destination = self._quarantine_root / self._quarantine_name(path, symbol)
            # os.replace, not Path.rename: the quarantine dir is a sibling of
            # the store, so the move can cross a filesystem, where rename
            # raises EXDEV and shutil.move would.
            os.replace(path, destination)
        except OSError:
            log.exception(
                "quarantine: could not move corrupt file %s (%s: %s); "
                "leaving it in place and skipping it for this read",
                path, type(error).__name__, error,
            )
            return ""

        log.error(
            "quarantine: corrupt parquet %s (%s: %s) moved to %s; "
            "%s returns an empty series until the partition is rebuilt",
            file_label, type(error).__name__, error,
            self._quarantine_root / destination.name, symbol,
        )
        return destination.name

    def _read_partition(
        self,
        partition_dir: Path,
        filter: pc.Expression | None = None,
    ) -> pa.Table | None:
        """Read one partition's parquet, quarantining it if it is unreadable.

        Returns ``None`` — the caller's signal to drop this partition — for
        every corrupt/unreadable file, so an ``ArrowInvalid`` never escapes
        this method. This is the single chokepoint all three call sites go
        through, so a fourth unguarded read added later is guarded by
        construction rather than by remembering.

        *filter* is an optional :class:`pyarrow.compute.Expression` (lane 3e).
        When it is present the read is a filtered one and row groups whose
        timestamp statistics cannot satisfy the predicate are skipped before
        any page is decompressed, instead of being decoded and discarded in
        pandas. It cannot change the result, only how much is decoded to
        produce it: a row group is skipped only when the predicate provably
        excludes every row in it, and the caller still applies its own bounds.
        """
        parquet_file = partition_dir / "data.parquet"
        if not parquet_file.exists():
            return None
        try:
            if filter is None:
                # The dedup reads in ``upsert`` and ``_write_parquet`` need
                # every row, so they pass no predicate. Keeping both shapes on
                # this one chokepoint keeps AC-15's guard covering exactly the
                # reads it covered before.
                return pq.ParquetFile(parquet_file).read()
            return self._read_filtered(parquet_file, filter)
        except _CORRUPT_FILE_ERRORS as exc:
            self.quarantine(parquet_file, exc)
            return None

    @staticmethod
    def _read_filtered(path: Path, filter: pc.Expression) -> pa.Table:
        """Read one parquet file with *filter* pushed into the reader.

        The whole of lane 3e. ``ParquetFile.read()`` decodes every row group
        in the file and hands all of them to pandas, which then throws most
        away for a narrow window. Building a one-file
        :class:`pyarrow.dataset.ParquetFileFragment` and reading it with the
        predicate makes pyarrow consult each row group's per-column statistics
        and skip the ones that cannot match, so the decode never happens.

        A fragment, not a whole-directory ``ds.dataset()``, on purpose: the
        quarantine chokepoint above needs the read to raise from one call
        site, and a fragment is the narrowest dataset object that still
        accepts a filter and still raises ``ArrowInvalid`` on a truncated file
        — which is what keeps AC-15 intact (verified:
        ``test_parquet_quarantine.py`` passes unchanged).

        When the predicate excludes every row of every partition, ``read()``
        still returns an empty frame — it builds one from its own column list,
        which types every column ``object``, exactly as the unfiltered path
        already did. Matching that is deliberate: the caller's empty-result
        contract is unchanged by this lane, and a typed empty frame is a
        separate improvement for a separate change.
        """
        fragment = ds.ParquetFileFormat().make_fragment(
            str(path), filesystem=_local_fs
        )
        return fragment.to_table(filter=filter)

    # ------------------------------------------------------------------ write

    def upsert(self, df: pd.DataFrame) -> int:
        """Insert-or-replace rows by (symbol, timeframe, timestamp).

        Returns the number of NEW rows written (not net change in file size).
        If the file already had 100 rows and we replace 10, returns 10.
        """
        if df is None or df.empty:
            return 0

        df = self._prepare_frame(df)
        written = 0

        for symbol in df["symbol"].unique():
            sub = df[df["symbol"] == symbol]
            for (year, month), grp in sub.groupby(
                [sub["timestamp"].dt.year, sub["timestamp"].dt.month]
            ):
                partition_dir = (
                    self._ohlcv_root
                    / f"symbol={symbol}"
                    / f"year={year}"
                    / f"month={int(month):02d}"
                )
                partition_dir.mkdir(parents=True, exist_ok=True)
                parquet_file = partition_dir / "data.parquet"

                partition_key = f"{symbol}/{year}/{int(month):02d}"
                lock = self._get_partition_lock(partition_key)
                with lock:
                    existing_table = self._read_partition(partition_dir)
                    if existing_table is not None:
                        existing = existing_table.to_pandas()
                        existing["timestamp"] = pd.to_datetime(existing["timestamp"])
                        if getattr(existing["timestamp"].dt, "tz", None) is not None:
                            existing["timestamp"] = existing["timestamp"].dt.tz_localize(None)
                        key_cols = ["symbol", "timeframe", "timestamp"]
                        existing = existing[
                            ~existing.set_index(key_cols).index.isin(
                                grp.set_index(key_cols).index
                            )
                        ]
                        combined = (
                            pd.concat([existing, grp], ignore_index=True)
                            if not existing.empty
                            else grp
                        )
                    else:
                        # First write into a new/empty partition, or the
                        # partition was just quarantined as unreadable. Either
                        # way there is nothing to merge, so the incoming rows
                        # become the whole file — the write path stays usable
                        # after a quarantine instead of needing a manual wipe.
                        combined = grp

                    if not combined.empty:
                        self._write_atomic(combined, parquet_file)
                        written += len(grp)
                        log.info("upsert: symbol=%s %d-%02d — %d new rows",
                                 symbol, year, int(month), len(grp))

        return written

    def _write_atomic(self, df: pd.DataFrame, path: Path) -> None:
        """Write *df* atomically via tmp+replace to avoid torn reads."""
        # thread id in tmp name: two threads upserting the same partition
        # must not collide on the tmp file (pid alone is not enough).
        tmp = (
            path.parent /
            f".tmp-{path.name}.{os.getpid()}.{threading.get_ident()}"
        )
        table = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(table, str(tmp), use_dictionary=False)
        tmp.replace(path)

    def _write_parquet(self, df: pd.DataFrame, path: Path) -> None:
        """Write a DataFrame to parquet (append + dedupe if file exists).

        Uses the same dedup semantics as :meth:`upsert`: for matching
        (symbol, timeframe, timestamp), new data replaces old.

        External callers may use this directly; it remains atomic and dedupes.
        """
        if path.exists():
            # Site 2 of 3. The dedup read goes through the same guarded
            # chokepoint as upsert() and read(): a corrupt file is quarantined
            # and this write replaces it with the incoming rows rather than
            # raising. The file may no longer exist by the time _read_partition
            # gets to it — quarantine moves it — so `path.exists()` is the
            # fast path, not the guard.
            existing_table = self._read_partition(path.parent)
            if existing_table is not None:
                existing = existing_table.to_pandas()
                existing["timestamp"] = pd.to_datetime(existing["timestamp"])
                if getattr(existing["timestamp"].dt, "tz", None) is not None:
                    existing["timestamp"] = existing["timestamp"].dt.tz_localize(None)
                # Same dedup semantics as upsert: new data replaces old for
                # matching (symbol, timeframe, timestamp) keys.
                key_cols = ["symbol", "timeframe", "timestamp"]
                existing = existing[
                    ~existing.set_index(key_cols).index.isin(
                        df.set_index(key_cols).index
                    )
                ]
                combined = (
                    pd.concat([existing, df], ignore_index=True)
                    if not existing.empty
                    else df
                )
                self._write_atomic(combined, path)
            else:
                self._write_atomic(df, path)
        else:
            self._write_atomic(df, path)

    def _prepare_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        """Ensure required columns + types for parquet storage."""
        df = df.copy()  # ponytail: never mutate caller's DataFrame
        for col in _BASE_COLUMNS:
            if col not in df.columns:
                df[col] = None
        df = df[_BASE_COLUMNS + [c for c in df.columns if c not in _BASE_COLUMNS]].copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        # ponytail: strip tz — storage contract is tz-naive wall time
        if getattr(df["timestamp"].dt, "tz", None) is not None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(None)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["volume"] = df["volume"].fillna(0).astype("int64")
        # --- OHLC invariant validation (drop corrupt bars) ---
        # high must be >= open/close, low <= open/close, high >= low, prices >0
        before = len(df)
        mask_valid = (
            (df["high"] >= df["open"]) & (df["high"] >= df["close"]) &
            (df["low"] <= df["open"]) & (df["low"] <= df["close"]) &
            (df["high"] >= df["low"]) &
            (df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0) &
            (df["volume"] >= 0)
        )
        # NaNs fail the mask and are dropped
        if mask_valid.sum() != before:
            dropped = df[~mask_valid]
            n_dropped = before - int(mask_valid.sum())
            examples = dropped.head(5)[
                ["symbol", "timestamp", "open", "high", "low", "close"]
            ].to_dict("records")
            log.warning(
                "ParquetStorage: dropping %d invalid OHLC bars (examples: %s)",
                n_dropped, examples,
            )
            df = df[mask_valid].copy()

        # Root-cause guard: the storage contract is NSE *weekday-session* bars.
        # Brokers occasionally emit phantom rows (a whole weekend session,
        # pre-market 03:45+, post-15:30). Drop them at the write chokepoint so
        # they never persist — read-time stripping alone hid them and made the
        # lake look clean while 0.7% was garbage (2026-02-01 took a phantom
        # Sunday session). See market_session_mask. (Idempotent: already-clean
        # frames pass through untouched.)
        before_session = len(df)
        df = df[market_session_mask(df["timestamp"])].reset_index(drop=True)
        if len(df) < before_session:
            log.warning(
                "ParquetStorage: dropping %d non-session bars "
                "(weekend/pre-post market)",
                before_session - len(df),
            )
        return df

    # ------------------------------------------------------------------ read

    def read(
        self,
        symbols: list[str] | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        timeframe: str | None = None,
        strip_post_market: bool = True,
    ) -> pd.DataFrame:
        """Read OHLCV data with partition pruning.

        Parameters
        ----------
        strip_post_market : bool
            Drop bars outside the 09:15-15:30 IST market session (default
            True). Dhan emits phantom post-market bars up to 20:00; pass
            ``False`` to read the raw stored bars (e.g. for audit).
        """
        if symbols is not None and len(symbols) == 0:
            return pd.DataFrame(columns=_BASE_COLUMNS)

        start_ts = pd.Timestamp(start) if start is not None else None
        end_ts = pd.Timestamp(end) if end is not None else None
        # ponytail: normalize tz-aware inputs to naive
        if start_ts is not None and start_ts.tzinfo is not None:
            start_ts = start_ts.tz_localize(None)
        if end_ts is not None and end_ts.tzinfo is not None:
            end_ts = end_ts.tz_localize(None)

        hive_paths = self._resolve_partitions(symbols, start_ts, end_ts)
        if not hive_paths:
            return pd.DataFrame(columns=_BASE_COLUMNS)

        # Lane 3e: hand the time bounds to the reader, not just to pandas.
        # ``_pushdown_filter`` is None only when the caller gave no bounds at
        # all, which is the case where nothing can be pruned anyway.
        pushdown = self._pushdown_filter(start_ts, end_ts)

        tables = []
        for pdir in hive_paths:
            # Site 3 of 3, and the one the spec describes: a corrupt partition
            # is quarantined and contributes nothing, so the symbol it held
            # comes back as an empty series and the symbols that were fine
            # still come back. This is the read path every call site (chart,
            # scanner, backtest, replay, recovery) shares.
            table = self._read_partition(pdir, filter=pushdown)
            if table is not None and table.num_rows > 0:
                tables.append(table.to_pandas())

        if not tables:
            return pd.DataFrame(columns=_BASE_COLUMNS)

        result = pd.concat(tables, ignore_index=True)
        result["timestamp"] = pd.to_datetime(result["timestamp"])
        if getattr(result["timestamp"].dt, "tz", None) is not None:
            result["timestamp"] = result["timestamp"].dt.tz_localize(None)
        if start_ts is not None:
            result = result[result["timestamp"] >= start_ts]
        if end_ts is not None:
            result = result[result["timestamp"] <= end_ts]
        if timeframe is not None:
            result = result[result["timeframe"] == timeframe]
        if strip_post_market:
            # Session rule (weekday + market hours), single-sourced. Sundays /
            # pre-market blocks are not "post-market noise" but they are the
            # same class of phantom; keep stored & served views consistent.
            result = result[market_session_mask(result["timestamp"])]
        return result.sort_values(["symbol", "timestamp"]).reset_index(drop=True)

    @staticmethod
    def _pushdown_filter(
        start_ts: pd.Timestamp | None,
        end_ts: pd.Timestamp | None,
    ) -> pc.Expression | None:
        """Build the timestamp predicate handed to the parquet reader.

        Returns ``None`` when neither bound is set, which signals "read
        everything" — the same signal ``read()``'s own Python filters take
        from an absent bound, so the two cannot disagree.

        The bounds deliberately mirror the comparison ``read()`` applies
        itself: the lower bound closes to ``>=`` and the upper to ``<=``, both
        inclusive. Every row the predicate admits is one the caller may keep,
        so it can only make the read *superset-shaped*; it can never drop a row
        the caller wanted. That is what makes the pushdown safe to leave on by
        default rather than behind a flag, and it is also why the caller's own
        filters are still applied afterwards — the predicate widens nothing,
        and the row groups it leaves in can straddle the requested edge.

        Both bounds are sent even though the Hive partition walk has already
        narrowed to the month: inside one month file a request for the last
        week still spans every row group, and that is the case where pruning
        pays.
        """
        terms: list[pc.Expression] = []
        if start_ts is not None:
            terms.append(pc.field(_TIMESTAMP_COLUMN) >= pa.scalar(start_ts))
        if end_ts is not None:
            terms.append(pc.field(_TIMESTAMP_COLUMN) <= pa.scalar(end_ts))
        if not terms:
            return None
        expr = terms[0]
        for term in terms[1:]:
            expr = expr & term
        return expr

    def _resolve_partitions(
        self,
        symbols: list[str] | None,
        start_ts: pd.Timestamp | None,
        end_ts: pd.Timestamp | None,
    ) -> list[Path]:
        """Resolve which Hive partition dirs to scan."""
        hive_paths: list[Path] = []
        if start_ts is not None and end_ts is not None:
            for sym in symbols or self._all_symbols():
                for period in pd.period_range(start=start_ts, end=end_ts, freq="M"):
                    pdir = (
                        self._ohlcv_root
                        / f"symbol={sym}"
                        / f"year={period.year}"
                        / f"month={period.month:02d}"
                    )
                    if pdir.exists():
                        hive_paths.append(pdir)
        elif symbols is not None:
            for sym in symbols:
                sym_dir = self._ohlcv_root / f"symbol={sym}"
                if sym_dir.exists():
                    hive_paths.extend(p for p in sym_dir.rglob("month=*") if p.is_dir())
        else:
            hive_paths = list(self._ohlcv_root.rglob("month=*"))
        return hive_paths

    # ------------------------------------------------------------------ metadata

    def symbols(self) -> list[str]:
        """All symbols present in the store."""
        return self._all_symbols()

    def _all_symbols(self) -> list[str]:
        if not self._ohlcv_root.exists():
            return []
        return sorted(
            p.name.split("=", 1)[1]
            for p in self._ohlcv_root.iterdir()
            if p.is_dir() and p.name.startswith("symbol=")
        )

    def date_range(
        self, symbol: str, timeframe: str | None = None
    ) -> tuple[datetime, datetime] | None:
        """Min/max timestamp for a symbol."""
        df = self.read(symbols=[symbol], timeframe=timeframe)
        if df.empty:
            return None
        return df["timestamp"].min(), df["timestamp"].max()

    def clear(self) -> None:
        """Wipe all stored data."""
        if self._ohlcv_root.exists():
            shutil.rmtree(self._ohlcv_root)
        self._ohlcv_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------- DuckDB integration

    def duckdb_scan(self, con, *, start=None, end=None) -> str:
        """Register a DuckDB view over the parquet store.

        Usage::

            import duckdb
            store = ParquetStorage("data/")
            con = duckdb.connect()
            store.duckdb_scan(con)
            con.execute("SELECT * FROM ohlcv WHERE symbol='RELIANCE'").fetchdf()
        """
        path_pattern = str(self._ohlcv_root / "**" / "data.parquet")
        if start is None and end is None:
            now = pd.Timestamp.now()
            start = now - pd.Timedelta(minutes=1)
            end = now
        where = ""
        if start is not None or end is not None:
            parts = []
            if start is not None:
                ts_val = pd.Timestamp(start)
                if ts_val.tzinfo is not None:
                    ts_val = ts_val.tz_localize(None)
                parts.append(f"timestamp >= TIMESTAMP '{ts_val.strftime('%Y-%m-%d %H:%M:%S')}'")
            if end is not None:
                ts_val = pd.Timestamp(end)
                if ts_val.tzinfo is not None:
                    ts_val = ts_val.tz_localize(None)
                parts.append(f"timestamp <= TIMESTAMP '{ts_val.strftime('%Y-%m-%d %H:%M:%S')}'")
            where = " WHERE " + " AND ".join(parts)
        con.execute(
            f"CREATE OR REPLACE VIEW ohlcv AS "
            f"SELECT *, CAST(timestamp AS TIMESTAMP) AS ts "
            f"FROM parquet_scan('{path_pattern}', hive_partitioning=True){where}"
        )
        return "ohlcv"


__all__ = ["ParquetStorage"]
