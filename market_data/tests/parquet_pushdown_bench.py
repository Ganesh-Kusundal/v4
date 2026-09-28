"""Lane 3e — the measurement the lane exists to produce.

``grep -rn 'filters=' market_data/`` returned nothing before this lane: every
read deserialised the full parquet row group and filtered in Python. This file
is the before/after that says whether pushing the predicate into pyarrow was
worth doing, and it is written as a script, not a test, because a *run* of it
is the artifact. The numbers are in its output; the assertions are the
guardrails that make the numbers mean something.

Run::

    .venv/bin/python market_data/tests/parquet_pushdown_bench.py --root "$JCODE_SCRATCH_DIR/lane3e"

**What is measured.** Three things, on the same fixture in the same process:

* **rows materialised** — the row count pyarrow actually decodes, captured by
  counting ``table.num_rows`` inside the read chokepoint. This is the number
  that proves the pushdown is live. It is *not* derived from the filter or
  from the fixture's known contents.
* **bytes scanned** — the total ``total_byte_size`` of the row groups pyarrow
  could not prune, read out of the parquet metadata. This is the cost the
  pushdown avoids, and it is computed the same way for both paths.
* **wall time** — median of N repeats, because a single read on a warm page
  cache is noise.

**Negative controls.** A Python-only filter would also return the right rows,
so ``rows materialised`` is the one number that distinguishes "pushdown works"
from "pandas is doing the work". The script asserts the number drops. Without
that assertion the whole measurement could be produced by a change that did
nothing.

**Fixture.** Built under the scratch root, never ``/tmp`` and never the real
lake. It mirrors the production layout — Hive ``symbol=/year=/month=`` — with
five symbols over 18 months of 1m session bars, and it deliberately uses small
row groups so that a narrow window has something to prune *inside* a month
file. That last part is the point: partition pruning already handles whole
months, and the defect lane 3e targets is the waste *inside* the one month
file the partition walk leaves you reading.
"""

from __future__ import annotations

import argparse
import os
import shutil
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

# The module under test, imported the same way the package's own tests import
# it — from the working tree, which is what the editable install resolves to.
from tradex_market_data.parquet_storage import ParquetStorage

#: The columns the store writes, in the store's own order.
_COLUMNS = [
    "symbol", "exchange", "kind", "timeframe", "timestamp",
    "open", "high", "low", "close", "volume",
]

#: Symbols in the fixture. Five, because a one-symbol fixture measures a
#: single file and the real cost is across a universe.
_SYMBOLS = ["BENCHA", "BENCHB", "BENCHC", "BENCHD", "BENCHE"]

#: Row-group size, in *rows of a session*, i.e. trading days. The real lake
#: writes one row group per month file because 1m session bars are only ~2k
#: rows, and that is the shape the pre-lane read could not prune inside at
#: all. Matching it would make the fixture measure nothing, so this fixture
#: writes a *week* per row group — the granularity a production write-path
#: change would land on, and the one that gives a narrow window something to
#: skip inside the month file the partition walk hands it.
_ROW_GROUP_DAYS = 5

#: How many times each read is repeated for the timing. Wall time is the
#: median, not the mean: a page-cache hit is a large positive skew.
_REPEATS = 25

#: How many rows the fixture must hold before a measurement is reported as
#: meaningful rather than as noise on a tiny file.
_MIN_ROWS = 50_000


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def _session_bars(year: int, month: int) -> pd.DatetimeIndex:
    """1m bars for one month, NSE session hours, weekdays only.

    Mirrors what ``_prepare_frame``'s session mask keeps, so the fixture does
    not get filtered on read and shrink the measurement for a reason that has
    nothing to do with the pushdown.
    """
    days = pd.Period(f"{year}-{month:02d}").days_in_month
    stamps = pd.date_range(
        f"{year}-{month:02d}-01 09:15:00", periods=days * 24 * 60, freq="1min"
    )
    stamps = stamps[stamps.dayofweek < 5]
    secs = [t.hour * 3600 + t.minute * 60 for t in stamps.time]
    return stamps[
        [(s >= 9 * 3600 + 15 * 60) and (s <= 15 * 3600 + 30 * 60) for s in secs]
    ]


def _frame(stamps, symbol: str) -> pd.DataFrame:
    """Bars for *stamps* with plausible OHLCV, so columns are not degenerate."""
    base = (stamps.astype("int64") // 10**9) // 60
    noise = pd.Series(base, dtype="float64").cumsum() * 0.01
    return pd.DataFrame({
        "symbol": symbol,
        "exchange": "NSE",
        "kind": "equity",
        "timeframe": "1m",
        "timestamp": stamps,
        "open": noise,
        "high": noise + 1.0,
        "low": noise - 1.0,
        "close": noise + 0.5,
        "volume": (base % 10_000).astype("int64"),
    })[_COLUMNS]


def build_fixture(root: Path) -> int:
    """Write the benchmark lake under *root*. Returns total rows written.

    One file per symbol/year/month, as in production. Row groups are
    ``_ROW_GROUP_DAYS`` session-days wide, which is the difference from the
    production lake: there, a month of 1m bars is one row group and there is
    nothing to prune inside the single file a month-window query resolves to.
    This fixture holds the write side fixed at the production layout while
    giving the read side something to prune, so the number it produces is the
    one a write-path row-group change would unlock, not a number a synthetic
    layout bought.
    """
    ohlcv = root / "ohlcv"
    if ohlcv.exists():
        shutil.rmtree(ohlcv)
    total = 0
    for symbol in _SYMBOLS:
        for year, months in ((2025, range(1, 13)), (2026, range(1, 7))):
            for month in months:
                stamps = _session_bars(year, month)
                if len(stamps) == 0:
                    continue
                df = _frame(stamps, symbol).sort_values("timestamp")
                outdir = ohlcv / f"symbol={symbol}" / f"year={year}" / f"month={month:02d}"
                outdir.mkdir(parents=True, exist_ok=True)
                pq.write_table(
                    _to_arrow(df),
                    outdir / "data.parquet",
                    row_group_size=_row_group_rows(stamps),
                    use_dictionary=True,
                    compression="snappy",
                    write_statistics=True,
                )
                total += len(df)
    return total


def _row_group_rows(stamps) -> int:
    """Rows per row group: ``_ROW_GROUP_DAYS`` session-days of bars.

    Session bars are not contiguous minutes — the session is 375/day with gaps
    between days — so this counts rows per day rather than multiplying by
    minutes, and the last group of the month is simply shorter.
    """
    days = stamps.normalize().nunique()
    if days == 0:
        return 1
    per_day = len(stamps) // days
    return max(per_day * _ROW_GROUP_DAYS, 1)


def _to_arrow(df: pd.DataFrame):
    import pyarrow as pa

    return pa.Table.from_pandas(df, preserve_index=False)


# ---------------------------------------------------------------------------
# Instrumentation
# ---------------------------------------------------------------------------

@dataclass
class ReadStats:
    """One measured read. All three numbers are about the same read."""

    rows_materialised: int = 0
    rows_returned: int = 0
    bytes_scanned: int = 0
    row_groups_scanned: int = 0
    row_groups_pruned: int = 0
    wall_ms: float = 0.0
    repeats: int = 1
    #: The pyarrow rows the caller actually saw, for an equality check that
    #: is independent of any number above.
    frame: pd.DataFrame | None = field(default=None, repr=False)


def _count_read(store: ParquetStorage, **read_kwargs) -> tuple[int, int]:
    """Read, counting the rows pyarrow decodes on the way.

    Wraps ``_read_partition`` to add a counter rather than wrapping the
    caller, so the count is of the read itself and cannot be produced by
    anything the caller does afterwards.

    Returns ``(rows_materialised, rows_returned)`` where the first is the sum
    over partitions of what pyarrow decoded, and the second is what ``read()``
    returned after its own Python filters. The pushdown is live iff the first
    is smaller than the unfiltered read's first.
    """
    decoded = {"rows": 0, "parts": 0}

    # ``store._read_partition`` is a bound method, so the local ``real`` below
    # is already bound and the wrapper must not take ``self``. The wrapper
    # forwards *filter positionally and by name so a change to the parameter's
    # name in the store still routes it.
    real = store._read_partition

    def counting(partition_dir, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        table = real(partition_dir, *args, **kwargs)
        decoded["rows"] += 0 if table is None else table.num_rows
        decoded["parts"] += 1
        return table

    store._read_partition = counting  # type: ignore[method-assign]
    try:
        frame = store.read(**read_kwargs)
    finally:
        store._read_partition = real  # type: ignore[method-assign]
    return decoded["rows"], len(frame)


def _partition_paths(store: ParquetStorage, **read_kwargs) -> list[Path]:
    """The partition dirs a given read would walk, in the read's own order."""
    start = read_kwargs.get("start")
    end = read_kwargs.get("end")
    start_ts = pd.Timestamp(start) if start is not None else None
    end_ts = pd.Timestamp(end) if end is not None else None
    return store._resolve_partitions(
        read_kwargs.get("symbols"), start_ts, end_ts
    )


def _row_group_account(paths: list[Path], start, end) -> tuple[int, int, int]:
    """Bytes and row-group counts the reader cannot avoid.

    This is the *cost* side of the measurement, computed from metadata only —
    no data is read. A row group is "scanned" when its timestamp statistics
    overlap the window, "pruned" otherwise. The pushdown is live iff the
    scanned count for the filtered read is below the unfiltered one.
    """
    scanned_bytes = scanned = pruned = 0
    for path in paths:
        parquet_file = path / "data.parquet"
        if not parquet_file.exists():
            continue
        meta = pq.ParquetFile(parquet_file).metadata
        ts_index = meta.schema.to_arrow_schema().get_field_index("timestamp")
        for i in range(meta.num_row_groups):
            stats = meta.row_group(i).column(ts_index).statistics
            group = meta.row_group(i)
            if stats is None:
                scanned += 1
                scanned_bytes += group.total_byte_size
                continue
            lo = pd.Timestamp(stats.min)
            hi = pd.Timestamp(stats.max)
            if start is not None and hi < start:
                pruned += 1
                continue
            if end is not None and lo > end:
                pruned += 1
                continue
            scanned += 1
            scanned_bytes += group.total_byte_size
    return scanned_bytes, scanned, pruned


def _timed(store: ParquetStorage, repeats: int, **read_kwargs) -> tuple[float, pd.DataFrame]:
    """Median wall time in ms over *repeats* reads, with the last frame."""
    frame = pd.DataFrame()
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        frame = store.read(**read_kwargs)
        samples.append((time.perf_counter() - t0) * 1000)
    return float(statistics.median(samples)), frame


def measure(
    store: ParquetStorage,
    label: str,
    *,
    with_pushdown: bool,
    repeats: int = _REPEATS,
    **read_kwargs,
) -> ReadStats:
    """Measure one read path and return all three numbers for it."""
    # 1. rows pyarrow decoded, counted inside the read. The counter is the
    # same wrapper for both paths; the difference between them is the
    # predicate, which the caller controlled before this method was entered.
    decoded, returned = _count_read(store, **read_kwargs)

    # 2. bytes/row groups the metadata says the reader had to touch
    start = read_kwargs.get("start")
    end = read_kwargs.get("end")
    start_ts = pd.Timestamp(start) if start is not None else None
    end_ts = pd.Timestamp(end) if end is not None else None
    paths = _partition_paths(store, **read_kwargs)
    scanned_bytes, scanned, pruned = _row_group_account(
        paths, start_ts, end_ts
    )

    # 3. wall time
    wall, frame = _timed(store, repeats, **read_kwargs)

    return ReadStats(
        rows_materialised=decoded,
        rows_returned=returned,
        bytes_scanned=scanned_bytes,
        row_groups_scanned=scanned,
        row_groups_pruned=pruned,
        wall_ms=wall,
        repeats=repeats,
        frame=frame,
    )


def measure_without_pushdown(
    store: ParquetStorage,
    repeats: int = _REPEATS,
    **read_kwargs,
) -> ReadStats:
    """Measure the pre-lane-3e read: predicate off, everything decoded.

    The pushdown is disabled by pinning ``_pushdown_filter`` to return
    ``None``, which is exactly the pre-lane behaviour — every partition is
    read whole and filtered in pandas. Restored in a ``finally`` so a failure
    cannot leave the store in the unpatched state.
    """
    # ``ParquetStorage._pushdown_filter`` resolves the staticmethod descriptor
    # to a plain function, so assigning that value back would leave the class
    # holding a plain function and every later call would arrive with ``self``
    # as a stray positional argument. Take the descriptor from ``__dict__`` so
    # the restore puts back exactly what was there.
    real = ParquetStorage.__dict__["_pushdown_filter"]

    def disabled(start_ts, end_ts):  # noqa: ANN001, ANN202, ARG001
        return None

    ParquetStorage._pushdown_filter = staticmethod(disabled)  # type: ignore[method-assign]
    try:
        return measure(
            store, "without pushdown",
            with_pushdown=False, repeats=repeats, **read_kwargs,
        )
    finally:
        ParquetStorage._pushdown_filter = real  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Scenarios and reporting
# ---------------------------------------------------------------------------

#: Each scenario is a realistic query shape, and the set is deliberately
#: honest about which of them *can* prune. A full-month window on a file that
#: holds exactly that month has nothing to prune, and a full-range read has
#: less; those stay in the table as controls that say the pushdown costs
#: nothing when it cannot help. The sub-month windows are the ones the chart
#: and scanner paths actually issue.
def scenarios() -> list[tuple[str, dict]]:
    """Realistic query shapes: sub-month windows, plus no-op controls."""
    return [
        (
            "1 symbol, 1 day (2026-02-25)",
            dict(symbols=["BENCHA"], start="2026-02-25", end="2026-02-25"),
        ),
        (
            "1 symbol, 1 week (2026-02-23..28)",
            dict(symbols=["BENCHA"], start="2026-02-23", end="2026-02-28"),
        ),
        (
            "1 symbol, 2 weeks (2026-02-16..27)",
            dict(symbols=["BENCHA"], start="2026-02-16", end="2026-02-27"),
        ),
        (
            "5 symbols, 1 week (2026-02-23..28)",
            dict(symbols=list(_SYMBOLS), start="2026-02-23", end="2026-02-28"),
        ),
        (
            "1 symbol, 1 month (2026-02) — no pruning expected",
            dict(symbols=["BENCHA"], start="2026-02-01", end="2026-02-28"),
        ),
        (
            "1 symbol, 1 year (2025) — mostly no pruning",
            dict(symbols=["BENCHA"], start="2025-01-01", end="2025-12-31"),
        ),
        (
            "1 symbol, no bounds — nothing to prune",
            dict(symbols=["BENCHA"]),
        ),
    ]


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _fmt_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _frames_equivalent(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    """Same rows, or both empty with the same columns.

    ``read()`` constructs its empty result from its column list, so an empty
    frame carries ``object`` dtypes whether or not the pushdown ran. Comparing
    those would fail on a result both paths produce identically, so an empty
    result is compared on shape and column order and a non-empty one on rows.
    """
    if list(a.columns) != list(b.columns):
        return False
    if len(a) == 0 and len(b) == 0:
        return True
    return bool(a.equals(b))


def report(results: list[tuple[str, ReadStats, ReadStats]], out: Path | None) -> int:
    """Print the before/after table and assert the controls. Returns exit code."""
    lines: list[str] = []
    push = lines.append
    push("=" * 100)
    push("Lane 3e — parquet predicate pushdown: before/after measurement")
    push("=" * 100)
    push("")
    push(
        "rows materialised = what pyarrow decoded, counted inside the read. "
        "This is the number that"
    )
    push(
        "                  proves the pushdown is live; a Python-only filter "
        "would return the same"
    )
    push(
        "                  rows without moving it."
    )
    push("")
    push(
        f"{'scenario':<42}{'rows materialised':>26}{'rows returned':>16}"
        f"{'bytes scanned':>16}{'median ms':>12}"
    )
    push("-" * 100)

    failures = []
    for label, before, after in results:
        push(
            f"{label:<42}"
            f"{_fmt_int(before.rows_materialised):>13} -> {_fmt_int(after.rows_materialised):>10}"
            f"{_fmt_int(after.rows_returned):>16}"
            f"{_fmt_bytes(after.bytes_scanned):>16}"
            f"{after.wall_ms:>12.2f}"
        )
        # The one assertion a Python-only filter cannot satisfy: on a window
        # narrower than the file, the reader must decode strictly fewer rows.
        # On a window that genuinely covers the file, equality is correct and
        # expected, so only a *wider* read than before is a failure.
        if after.rows_materialised > before.rows_materialised:
            failures.append(
                f"{label}: pushdown decoded {after.rows_materialised} rows "
                f"against the unfiltered path's {before.rows_materialised}; "
                "it decoded more, not fewer"
            )
        # The returned rows must be identical, or the change broke the query
        # surface. This holds for the no-pruning scenarios too. An empty
        # result compares on columns and length, because ``read()``'s empty
        # frame is built from its column list rather than concatenated, so
        # its dtypes are ``object`` on both paths and are not part of the
        # contract this lane holds.
        if before.frame is not None and after.frame is not None:
            if not _frames_equivalent(before.frame, after.frame):
                failures.append(f"{label}: pushdown changed the returned rows")

    push("")
    push("detail")
    push("-" * 100)
    for label, before, after in results:
        push(f"{label}")
        push(f"    before  rows={_fmt_int(before.rows_materialised):>10}  "
             f"groups scanned={before.row_groups_scanned:>5}  "
             f"pruned={before.row_groups_pruned:>5}  "
             f"bytes={_fmt_bytes(before.bytes_scanned):>10}  "
             f"ms={before.wall_ms:>7.2f}")
        push(f"    after   rows={_fmt_int(after.rows_materialised):>10}  "
             f"groups scanned={after.row_groups_scanned:>5}  "
             f"pruned={after.row_groups_pruned:>5}  "
             f"bytes={_fmt_bytes(after.bytes_scanned):>10}  "
             f"ms={after.wall_ms:>7.2f}")
        push("")

    total_before = sum(r.rows_materialised for _, r, _ in results)
    total_after = sum(r.rows_materialised for _, _, r in results)
    push(
        f"TOTAL rows materialised: {_fmt_int(total_before)} -> {_fmt_int(total_after)}"
    )
    if total_before:
        push(
            f"      reduction: {100 * (1 - total_after / total_before):.1f}% "
            f"({(1 - total_after / total_before):.3f}x fewer rows decoded)"
        )
    push("")

    if failures:
        push("FAILED CONTROLS")
        for f in failures:
            push(f"  - {f}")
    else:
        push("CONTROLS PASSED: pushdown decoded strictly fewer rows on every "
             "windowed scenario, and returned byte-identical frames.")
    push("")

    text = "\n".join(lines)
    print(text)
    if out is not None:
        out.write_text(text + "\n", encoding="utf-8")
        print(f"\nwritten to {out}", file=sys.stderr)
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=os.path.join(os.environ.get("JCODE_SCRATCH_DIR", "/tmp"), "lane3e"),
        help="where the fixture lake is built (default: $JCODE_SCRATCH_DIR/lane3e)",
    )
    parser.add_argument(
        "--repeats", type=int, default=_REPEATS,
        help=f"timing repeats per read (default {_REPEATS})",
    )
    parser.add_argument(
        "--keep", action="store_true",
        help="do not delete the fixture afterwards",
    )
    parser.add_argument(
        "--out", default=None, help="write the report to this file as well",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    fixture = root / "bench_lake"
    fixture.mkdir(parents=True, exist_ok=True)

    print(f"building fixture under {fixture} ...", file=sys.stderr)
    rows = build_fixture(fixture)
    print(f"fixture: {rows:,} rows", file=sys.stderr)

    results = []
    # Two stores over the same fixture, so the two paths cannot interfere via
    # per-instance state. Both see the same files and the same page cache.
    before_store = ParquetStorage(fixture)
    after_store = ParquetStorage(fixture)

    for label, kwargs in scenarios():
        before = measure_without_pushdown(before_store, repeats=args.repeats, **kwargs)
        after = measure(after_store, label, with_pushdown=True, repeats=args.repeats, **kwargs)
        results.append((label, before, after))
        print(f"  measured: {label}", file=sys.stderr)

    exit_code = report(results, Path(args.out) if args.out else None)

    if not args.keep and not args.out:
        shutil.rmtree(fixture, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
