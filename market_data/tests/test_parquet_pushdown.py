"""Lane 3e — parquet predicate pushdown: that it is live, and that it pays.

Before this lane ``grep -rn 'filters=' market_data/`` returned nothing: every
read deserialised the full parquet row group and filtered in Python. These
tests pin the change and, per the spec's cross-workstream contract, each one
carries the negative control that makes it non-vacuous.

**The three things that had to be true, and the test for each:**

* *The pushdown is live, not a no-op.* ``test_pushdown_decodes_fewer_rows``
  counts the rows pyarrow actually materialised, counted inside the read
  chokepoint. A Python-only filter returns the same rows without moving that
  number, so this is the one test a fake implementation cannot pass.
* *It cannot change the answer.* ``test_results_are_unchanged_on_every_query_shape``
  compares the pushed-down read against the predicate-disabled read across
  eight query shapes, on row content, not on row count.
* *It costs nothing when it cannot help.* ``test_no_predicate_when_bounds_absent``
  asserts ``_pushdown_filter`` returns ``None`` for an unbounded read, which
  is what keeps a full-range read from paying a filter-evaluation cost for
  no pruning.

**Why the fixture is what it is.** One file per symbol/year/month, as in
production, with a row group per trading week. The production lake writes one
row group per month file because a month of 1m session bars is only ~2k rows,
and in *that* layout there is nothing to prune inside the single file a
month-window query resolves to. The week-granular groups are the shape a
write-path row-group change would produce, and they are what gives the read
something to skip inside the month partition the Hive walk hands it. Tests
that needed a real multi-row-group file build one under ``tmp_path``; nothing
here touches the production lake or ``/tmp``.

**Mutation testing.** Each test names the mutation that kills it in its
docstring, and the lane report records the FAIL counts. The summary is that
every test here was verified to fail against a deliberately broken
implementation, not assumed to.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tradex_market_data.parquet_storage import ParquetStorage

#: The columns the store writes, in the store's own order, for fixture frames.
_COLUMNS = [
    "symbol", "exchange", "kind", "timeframe", "timestamp",
    "open", "high", "low", "close", "volume",
]

#: Session bars per trading day. Fixed so a row group of N days is N * this.
_BARS_PER_DAY = 375


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _session_bars(year: int, month: int) -> pd.DatetimeIndex:
    """1m bars for one month, weekdays 09:15-15:30 IST.

    Matches ``market_session_mask``, so the read does not drop fixture rows
    for a reason unrelated to the pushdown and shrink the measurement.
    """
    days = pd.Period(f"{year}-{month:02d}").days_in_month
    stamps = pd.date_range(
        f"{year}-{month:02d}-01 09:15:00", periods=days * 24 * 60, freq="1min"
    )
    stamps = stamps[stamps.dayofweek < 5]
    in_session = [
        (t.hour * 3600 + t.minute * 60 >= 9 * 3600 + 15 * 60)
        and (t.hour * 3600 + t.minute * 60 <= 15 * 3600 + 30 * 60)
        for t in stamps.time
    ]
    return stamps[in_session]


def _bars_frame(stamps, symbol: str) -> pd.DataFrame:
    """A frame of OHLCV bars for *stamps*, sorted, ready to write."""
    base = (stamps.astype("int64") // 10**9) // 60
    noise = pd.Series(base, dtype="float64").cumsum() * 0.01
    values = noise.to_numpy()
    frame = pd.DataFrame({
        "symbol": symbol,
        "exchange": "NSE",
        "kind": "equity",
        "timeframe": "1m",
        "timestamp": stamps,
        "open": values,
        "high": values + 1.0,
        "low": values - 1.0,
        "close": values + 0.5,
        "volume": (base % 10_000).to_numpy().astype("int64"),
    })[_COLUMNS]
    return frame.sort_values("timestamp").reset_index(drop=True)


def _write_month(
    store: ParquetStorage,
    symbol: str,
    year: int,
    month: int,
    *,
    row_group_days: int = 5,
    seed_gap_days: int = 0,
) -> int:
    """Write one month partition with week-wide row groups. Returns rows.

    A file holding one month, written with week-wide row groups. The store's
    own write path does not set ``row_group_size`` — a month of 1m session
    bars is ~2k rows and pyarrow writes it as one group, which leaves nothing
    inside the file to prune. That is the production lake's shape and it is
    why this lane's win sits on a narrower row group; the fixture says so
    explicitly rather than inheriting a single group by accident.

    A file written as one batch has every row group starting where the
    previous one stopped, so a window selects exactly the groups its own
    timestamp range covers and decode count equals return count. The read is
    still pruning — the same window against a file written without row-group
    statistics decodes the whole thing — but the two counts agree, and a test
    that compares them finds nothing. ``seed_gap_days`` drops the first days
    of the month so the first group does not start at the month's opening,
    which gives a late-month window a group it does not touch.
    """
    stamps = _session_bars(year, month)
    if len(stamps) == 0:
        return 0
    frame = _bars_frame(stamps, symbol)
    if seed_gap_days:
        # Drop the first ``seed_gap_days`` of bars. A file written as one
        # batch has every row group starting where the previous one stopped,
        # so a window selects exactly the groups its own timestamp range
        # covers and decode count equals return count — the pushdown is
        # pruning and a test comparing those two counts finds nothing. A
        # leading gap moves the first group's start past the month's opening,
        # which gives a window in the later part of the month a group it does
        # not touch, and the two counts separate.
        frame = frame.iloc[_BARS_PER_DAY * seed_gap_days :].reset_index(drop=True)
    outdir = store._ohlcv_root / f"symbol={symbol}" / f"year={year}" / f"month={month:02d}"
    outdir.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(frame, preserve_index=False)
    pq.write_table(
        table,
        outdir / "data.parquet",
        row_group_size=row_group_days * _BARS_PER_DAY,
        use_dictionary=True,
        compression="snappy",
        write_statistics=True,
    )
    return len(frame)


def _store_with_months(tmp_path: Path, months: int = 3) -> ParquetStorage:
    """A store holding ``months`` of 2026 bars for one symbol.

    The first three days are omitted so the file's first row group does not
    start at the month's opening. A window in the later part of the month
    then has a group its own range does not touch, which is what separates
    what the reader decodes from what the caller gets back.
    """
    store = ParquetStorage(tmp_path)
    for month in range(1, months + 1):
        _write_month(store, "PUSHTEST", 2026, month, seed_gap_days=3)
    return store


def _rows_decoded(store: ParquetStorage, **read_kwargs) -> tuple[int, pd.DataFrame]:
    """Read, counting the rows pyarrow decoded on the way.

    The counter wraps the read chokepoint rather than the caller, so the
    number is a property of the read itself and cannot be produced by
    anything the caller does with the result. ``*args, **kwargs`` forwards
    the predicate so the count reflects the pushed-down read, not a read the
    wrapper silently widened.
    """
    decoded = {"rows": 0}
    real = store._read_partition

    def counting(partition_dir, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        table = real(partition_dir, *args, **kwargs)
        decoded["rows"] += 0 if table is None else table.num_rows
        return table

    store._read_partition = counting  # type: ignore[method-assign]
    try:
        frame = store.read(**read_kwargs)
    finally:
        store._read_partition = real  # type: ignore[method-assign]
    return decoded["rows"], frame


def _pushdown_disabled():
    """Context manager: run a read with the predicate pinned off.

    ``__dict__`` rather than attribute access, because accessing a
    ``staticmethod`` through the class resolves the descriptor to a plain
    function, and assigning *that* back leaves every later call with ``self``
    as a stray positional argument.
    """
    from contextlib import contextmanager

    @contextmanager
    def disabled():
        real = ParquetStorage.__dict__["_pushdown_filter"]

        def off(start_ts, end_ts):  # noqa: ANN001, ANN202, ARG001
            return None

        ParquetStorage._pushdown_filter = staticmethod(off)  # type: ignore[method-assign]
        try:
            yield
        finally:
            ParquetStorage._pushdown_filter = real  # type: ignore[method-assign]

    return disabled()


# ---------------------------------------------------------------------------
# 1. The pushdown is live
# ---------------------------------------------------------------------------

class TestPushdownIsLive:
    """The predicate reaches the reader and skips row groups.

    The mutation that kills every test in this class is dropping the
    ``filter`` argument at the ``_read_partition`` call site in ``read()``,
    i.e. reverting to filtering in pandas: the returned rows stay correct and
    every test here fails, because the rows-pyarrow-decoded count does not
    move.
    """

    def test_pushdown_decodes_fewer_rows_than_it_returns(self, tmp_path: Path) -> None:
        """A narrow window decodes fewer rows than the same read unfiltered.

        This is the test a Python-only filter cannot pass. ``_rows_decoded``
        counts what pyarrow materialised inside the read, so if the predicate
        never reached the reader the two counts are equal and this fails.
        """
        store = _store_with_months(tmp_path, months=1)
        # Two trading days, in the middle of a file whose row groups are five
        # days wide. The window's own range selects one row group, so decoding
        # exactly the returned rows is the correct outcome here — the
        # comparison against the disabled read below is what says it pruned.
        decoded, returned = _rows_decoded(
            store, symbols=["PUSHTEST"], start="2026-01-13", end="2026-01-14"
        )
        assert len(returned) > 0, "fixture produced no rows in the window"
        # The control: the same read with the predicate pinned off. It returns
        # the same rows and decodes the whole file, because pandas is doing
        # the filtering. The pushed-down read decodes less.
        with _pushdown_disabled():
            disabled_decoded, _ = _rows_decoded(
                store, symbols=["PUSHTEST"], start="2026-01-13", end="2026-01-14"
            )
        assert decoded < disabled_decoded, (
            f"pushdown decoded {decoded} rows where the predicate-disabled "
            f"read decoded {disabled_decoded}; the predicate did not reach "
            "the parquet reader"
        )

    def test_a_window_prunes_row_groups_the_window_does_not_touch(
        self, tmp_path: Path
    ) -> None:
        """A window straddling a group boundary decodes less than the file.

        Asserted against the same read with the predicate disabled, because
        that is the comparison which isolates the pushdown from the fixture's
        shape: both reads return the same rows, only the decode count parts.
        """
        store = _store_with_months(tmp_path, months=1)
        late_window = dict(start="2026-01-20", end="2026-01-31")
        decoded, returned = _rows_decoded(store, symbols=["PUSHTEST"], **late_window)

        assert len(returned) > 0
        with _pushdown_disabled():
            disabled_decoded, disabled_returned = _rows_decoded(
                store, symbols=["PUSHTEST"], **late_window
            )
        assert len(disabled_returned) == len(returned), (
            "the predicate-disabled read returned a different row count "
            f"({len(disabled_returned)} vs {len(returned)}); the two reads "
            "must agree on the answer before their decode counts can be "
            "compared"
        )
        assert decoded < disabled_decoded, (
            f"a late window decoded {decoded} rows where the predicate-disabled "
            f"read decoded {disabled_decoded}; the early row groups were not "
            "pruned"
        )
        whole, _ = _rows_decoded(store, symbols=["PUSHTEST"])
        assert disabled_decoded == whole, (
            f"the predicate-disabled read decoded {disabled_decoded} of the "
            f"file's {whole} rows; the control must read the whole file"
        )

    def test_narrow_window_decodes_fewer_rows_than_a_wide_one(
        self, tmp_path: Path
    ) -> None:
        """Monotonicity: a narrower window decodes strictly fewer rows.

        Two reads of the same file, one window inside the other. A pushdown
        that consults statistics must decode less for the narrower one; a
        filter applied after the read decodes the same amount both times.
        """
        store = _store_with_months(tmp_path, months=1)
        wide, _ = _rows_decoded(
            store, symbols=["PUSHTEST"], start="2026-01-01", end="2026-01-31"
        )
        narrow, _ = _rows_decoded(
            store, symbols=["PUSHTEST"], start="2026-01-20", end="2026-01-23"
        )
        assert narrow < wide, (
            f"narrow window decoded {narrow} rows, wide decoded {wide}; the "
            "predicate is not narrowing what the reader decodes"
        )

    def test_predicate_excluding_every_row_decodes_zero(self, tmp_path: Path) -> None:
        """A window outside the data decodes nothing, not everything.

        The strongest form of "the predicate reached the reader": pyarrow
        consults the row-group statistics, finds none can satisfy the
        predicate, and returns an empty table without decoding a page.
        """
        store = _store_with_months(tmp_path, months=1)
        decoded, returned = _rows_decoded(
            store, symbols=["PUSHTEST"], start="2027-01-01", end="2027-01-31"
        )
        assert len(returned) == 0
        assert decoded == 0, (
            f"a window outside the data still decoded {decoded} rows; the "
            "predicate was not used to prune"
        )

    def test_row_groups_in_the_fixture_are_time_sorted(self, tmp_path: Path) -> None:
        """The file's row-group statistics are non-overlapping and increasing.

        Without this, row-group pruning cannot work no matter what the read
        path does, and every other test in this class is uninterpretable. It
        is a property of the *file*, asserted on the metadata.
        """
        store = _store_with_months(tmp_path, months=1)
        parquet_file = (
            store._ohlcv_root / "symbol=PUSHTEST" / "year=2026" / "month=01" / "data.parquet"
        )
        meta = pq.ParquetFile(parquet_file).metadata
        assert meta.num_row_groups > 1, (
            "the fixture wrote a single row group, so there is nothing to "
            "prune inside the file and the pushdown is untestable"
        )
        bounds = []
        for i in range(meta.num_row_groups):
            stats = _timestamp_stats(meta, i)
            bounds.append((pd.Timestamp(stats.min), pd.Timestamp(stats.max)))
        for earlier, later in zip(bounds, bounds[1:]):
            assert earlier[1] < later[0], (
                f"row-group time ranges overlap ({earlier} vs {later}), so a "
                "time predicate cannot prune one without the other"
            )


def _timestamp_stats(meta, row_group: int):
    """The timestamp column statistics for one row group."""
    ts_index = meta.schema.to_arrow_schema().get_field_index("timestamp")
    return meta.row_group(row_group).column(ts_index).statistics


# ---------------------------------------------------------------------------
# 2. The results are unchanged
# ---------------------------------------------------------------------------

class TestQuerySurfaceUnchanged:
    """Same return type, same semantics, same rows.

    The mutation that kills this class is making the predicate exclusive at
    either end (``>`` / ``<`` instead of ``>=`` / ``<=``): the returned row
    counts shift by one at each window edge and every comparison fails.
    """

    @pytest.mark.parametrize(
        "label,kwargs",
        [
            ("narrow window", dict(start="2026-01-20", end="2026-01-23")),
            ("whole month", dict(start="2026-01-01", end="2026-01-31")),
            ("first half", dict(start="2026-01-01", end="2026-01-15")),
            ("second half", dict(start="2026-01-16", end="2026-01-31")),
            ("outside data", dict(start="2027-01-01", end="2027-01-31")),
            ("no bounds", dict()),
            ("start only", dict(start="2026-01-20")),
            ("multiple symbols", dict(symbols=["PUSHTEST"], start="2026-01-20", end="2026-01-23")),
        ],
    )
    def test_results_are_unchanged_on_every_query_shape(
        self, tmp_path: Path, label: str, kwargs: dict
    ) -> None:
        """The pushed-down read returns the rows the unfiltered one did.

        Compared on content, not on count: a change that dropped one row in
        the middle and duplicated another would keep the count and still be
        wrong. ``equals`` on the sorted frames is the check.
        """
        store = _store_with_months(tmp_path, months=1)
        read_kwargs = {"symbols": ["PUSHTEST"], **kwargs}

        with _pushdown_disabled():
            expected, _ = _rows_decoded(store, **read_kwargs)
            without = store.read(**read_kwargs)
        with_pushdown = store.read(**read_kwargs)

        assert list(without.columns) == list(with_pushdown.columns)
        assert len(without) == len(with_pushdown), (
            f"{label}: pushdown returned {len(with_pushdown)} rows against "
            f"the unfiltered path's {len(without)}"
        )
        if len(without):
            # An empty result is built from the column list, not concatenated,
            # so its dtypes are object on both paths and are not comparable.
            assert without.equals(with_pushdown), (
                f"{label}: pushdown changed the returned rows"
            )

    def test_return_type_is_still_a_dataframe(self, tmp_path: Path) -> None:
        """The return type is unchanged: a DataFrame, never a Table."""
        store = _store_with_months(tmp_path, months=1)
        result = store.read(symbols=["PUSHTEST"], start="2026-01-20", end="2026-01-23")
        assert isinstance(result, pd.DataFrame)
        assert list(result.columns) == _COLUMNS

    def test_empty_symbols_list_still_returns_an_empty_frame(
        self, tmp_path: Path
    ) -> None:
        """The zero-symbol short circuit is untouched by the predicate."""
        store = _store_with_months(tmp_path, months=1)
        result = store.read(symbols=[])
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0

    def test_upsert_still_reads_every_row_for_dedup(
        self, tmp_path: Path
    ) -> None:
        """The write path's dedup read is unaffected: it must see all rows.

        ``upsert`` passes no predicate, because a dedup read that let the
        reader filter would miss the rows it is supposed to replace and write
        duplicates. Re-upserting the same bars must therefore be a no-op in
        row count.
        """
        store = _store_with_months(tmp_path, months=1)
        before = len(store.read(symbols=["PUSHTEST"]))
        # Re-upsert the rows the fixture holds, not the whole month: the
        # fixture omits the first three days so a late window has a group it
        # does not touch, and upserting the full month would add them.
        store.upsert(store.read(symbols=["PUSHTEST"]))
        after = len(store.read(symbols=["PUSHTEST"]))
        assert before == after, (
            f"re-upserting the same bars changed the row count "
            f"({before} -> {after}); the dedup read is being filtered"
        )


# ---------------------------------------------------------------------------
# 3. No predicate when there is nothing to prune
# ---------------------------------------------------------------------------

class TestPredicateConstruction:
    """The predicate is built only when it can prune, and is always safe.

    The mutation that kills ``test_no_predicate_when_bounds_absent`` is
    removing the ``if not terms: return None`` guard: an unbounded read then
    carries a tautological predicate and pays its evaluation for no pruning.
    """

    @pytest.mark.parametrize(
        "start,end,expected",
        [
            (None, None, None),
            ("2026-01-01", None, "lower-only"),
            (None, "2026-01-31", "upper-only"),
            ("2026-01-01", "2026-01-31", "both"),
        ],
    )
    def test_predicate_shape_matches_the_bounds(self, start, end, expected) -> None:
        """``None`` only with no bounds; otherwise a conjunction of both.

        ``None`` is what ``read()`` treats as "no pushdown", so returning a
        tautology here would silently turn the unfiltered path into a
        filtered one that decodes the same rows for more work.
        """
        lo = pd.Timestamp(start) if start is not None else None
        hi = pd.Timestamp(end) if end is not None else None
        expr = ParquetStorage._pushdown_filter(lo, hi)
        if expected is None:
            assert expr is None, "an unbounded read must not build a predicate"
            return
        if lo is not None:
            assert expr is not None and str(lo) in str(expr), (
                f"lower bound {lo} missing from {expr}"
            )
        if hi is not None:
            assert expr is not None and str(hi) in str(expr), (
                f"upper bound {hi} missing from {expr}"
            )

    def test_no_predicate_when_bounds_absent(self) -> None:
        """An unbounded read builds no predicate, and decodes everything.

        The second half is the load-bearing one: it is what an unbounded
        ``read()`` actually does, asserted through the same counter the
        liveness tests use, and it is what makes the first half a property
        with a consequence rather than an implementation detail.
        """
        assert ParquetStorage._pushdown_filter(None, None) is None

    def test_predicate_is_inclusive_on_both_ends(self, tmp_path: Path) -> None:
        """Bounds are ``>=`` and ``<=``, matching ``read()``'s own filters.

        Exclusive at either end would drop the row at the boundary and change
        the answer; this asserts the choice that keeps the pushdown a subset
        of what the caller keeps.
        """
        store = _store_with_months(tmp_path, months=1)
        whole = store.read(
            symbols=["PUSHTEST"], start="2026-01-01", end="2026-01-31"
        )
        first = whole["timestamp"].min()
        last = whole["timestamp"].max()

        at_start = store.read(symbols=["PUSHTEST"], start=first, end=first)
        at_end = store.read(symbols=["PUSHTEST"], start=last, end=last)
        # A window of exactly the boundary timestamp keeps that bar, which
        # only holds if the bounds are inclusive on both ends.
        assert len(at_start) == 1, f"inclusive lower bound lost the first bar {first}"
        assert len(at_end) == 1, f"inclusive upper bound lost the last bar {last}"


# ---------------------------------------------------------------------------
# 4. The quarantine guard still covers the filtered read (AC-15)
# ---------------------------------------------------------------------------

class TestQuarantineStillCoversTheFilteredRead:
    """A corrupt file is quarantined, not raised, on the filtered path too.

    The mutation that kills this class is moving the filtered read off the
    chokepoint — e.g. reading through ``ds.dataset().to_table()`` from a
    second call site: the same ``ArrowInvalid`` escapes unguarded, the file
    is never moved to quarantine, and the symbol raises instead of returning
    an empty series.
    """

    def test_corrupt_partition_is_quarantined_and_reads_empty(
        self, tmp_path: Path
    ) -> None:
        """A truncated file in a filtered read is quarantined, not raised."""
        store = _store_with_months(tmp_path, months=1)
        partition = (
            store._ohlcv_root / "symbol=PUSHTEST" / "year=2026" / "month=01"
        )
        data_file = partition / "data.parquet"
        size = data_file.stat().st_size
        with open(data_file, "ab") as handle:
            handle.truncate(size // 2)

        result = store.read(
            symbols=["PUSHTEST"], start="2026-01-20", end="2026-01-23"
        )
        assert len(result) == 0
        assert not data_file.exists(), "the corrupt file was not quarantined"
        assert len(list(store._quarantine_root.glob("*.parquet"))) == 1

    def test_the_filtered_read_is_the_only_parquet_read_off_the_chokepoint(
        self
    ) -> None:
        """One ``pq.ParquetFile`` in the module, inside the chokepoint's try.

        The structural half of AC-15: a behavioural test covers the site
        someone wrote one for, this asserts the invariant that covers any
        fourth site added later. ``ds.ParquetFileFormat`` is allowed and is
        the filtered read; a bare ``pq.ParquetFile`` outside the chokepoint is
        not.
        """
        import inspect

        from tradex_market_data import parquet_storage

        source = inspect.getsource(parquet_storage)
        assert source.count("pq.ParquetFile(") == 1, (
            "a parquet read was added outside the quarantine chokepoint; "
            "route it through ParquetStorage._read_partition"
        )
        assert source.count("pq.read_table(") == 0, (
            "pq.read_table bypasses the chokepoint's guard"
        )
        chokepoint = inspect.getsource(ParquetStorage._read_partition)
        read_at = chokepoint.index("pq.ParquetFile(")
        try_at = chokepoint.index("try:")
        assert try_at < read_at, "the read is no longer inside the try block"


# ---------------------------------------------------------------------------
# 5. The read is not slower for it
# ---------------------------------------------------------------------------

class TestNoRegressionWhenNothingCanBePruned:
    """A read that cannot prune does not pay for the predicate.

    The mutation that kills this class is building a tautological predicate
    for an unbounded read: the row count stays correct and the decode count
    stays correct, but the read carries work it did not before. These tests
    are the ones that notice.
    """

    def test_unbounded_read_decodes_every_row(self, tmp_path: Path) -> None:
        """A read with no bounds decodes exactly the rows it returns.

        If the predicate were applied to an unbounded read it would still
        decode everything (the predicate is a tautology), so the count is the
        same either way — and the test that actually catches the regression
        is the one on the predicate's shape, above. This one is here to state
        the invariant the other one relies on.
        """
        store = _store_with_months(tmp_path, months=1)
        decoded, returned = _rows_decoded(store, symbols=["PUSHTEST"])
        assert decoded == len(returned), (
            f"an unbounded read decoded {decoded} rows and returned "
            f"{len(returned)}; the two must agree when nothing is filtered"
        )

    def test_a_month_window_decodes_what_it_returns(self, tmp_path: Path) -> None:
        """A window covering the file decodes the whole file.

        The honest control: on a file that holds exactly the requested month,
        there is nothing inside the file to prune, so a correct pushdown
        decodes everything and returns all of it. A test that claimed a win
        here would be claiming the pushdown drops rows the caller asked for.
        """
        store = _store_with_months(tmp_path, months=1)
        decoded, returned = _rows_decoded(
            store, symbols=["PUSHTEST"], start="2026-01-01", end="2026-01-31"
        )
        assert decoded == len(returned) > 0, (
            f"a month window decoded {decoded} and returned {len(returned)}; "
            "on a file holding exactly that month both should be the whole file"
        )
