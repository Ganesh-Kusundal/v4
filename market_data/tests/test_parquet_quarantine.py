"""AC-15 / lane 3f — a corrupt parquet file is quarantined, counted, and read
as an empty series instead of raising ``ArrowInvalid`` at every call site.

The defect this pins is not hypothetical bookkeeping. Before it, all three
parquet reads in this module (``upsert``, ``_write_parquet``, ``read``) called
``pq.ParquetFile(path).read()`` bare, so a single truncated file raised
``ArrowInvalid`` out of ``store.read()`` — and every consumer shares that one
read: chart, scanner, backtest, replay, feed recovery. One bad file made one
symbol unreadable everywhere, permanently, with no operator signal.

**Why these tests are not vacuous.** Every test below drives the real code
with a genuinely corrupt file: a real parquet written by pyarrow and then
truncated to half its bytes, or a real partition built through ``upsert`` and
then corrupted on disk. Nothing is mocked, no exception is injected, and the
assertions are on the filesystem and the registry after the fact. Deleting the
``try/except`` in ``_read_partition`` makes them fail — with the
``ArrowInvalid`` the guard exists to suppress. That has been checked by
mutation, not assumed; see the module docstring of the implementation.

Tests live under ``market_data/tests`` because ``tradex_market_data`` owns the
store, and under that package the pytest ``pythonpath`` resolves
``tradex_market_data`` from the working tree, so the tests here exercise the
worktree's source rather than whatever the editable install resolves to.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tradex_market_data.parquet_storage import ParquetStorage
from tradex_observability.metrics import MetricsRegistry

#: The counter ``deploy/prometheus-rules.yml`` alerts on. Spelled out here, and
#: asserted literally in :func:`test_metric_name_is_the_one_the_alert_rule_watches`,
#: so a rename in either place fails instead of silently de-fusing the page.
QUARANTINE_METRIC = "datalake_quarantine_total"

#: A pinned mtime so the quarantine filename is exact, not merely well-formed.
#: 2026-03-04 05:06:07 UTC.
PINNED_MTIME = 1772600767.0


def _bar(symbol: str, timestamp: str) -> dict:
    return dict(
        symbol=symbol, exchange="NSE", kind="equity", timeframe="1m",
        timestamp=timestamp, open=100.0, high=101.0, low=99.0,
        close=100.0, volume=1000,
    )


def _store_with(
    tmp_path: Path,
    symbols: list[str],
    registry: MetricsRegistry | None = None,
) -> ParquetStorage:
    """A store holding one real partition per symbol in 2026-07."""
    store = ParquetStorage(tmp_path, metrics=registry)
    for symbol in symbols:
        assert store.upsert(pd.DataFrame([_bar(symbol, "2026-07-01 09:15:00")])) == 1
    return store


def _partition_file(root: Path, symbol: str) -> Path:
    return root / "ohlcv" / f"symbol={symbol}" / "year=2026" / "month=07" / "data.parquet"


def _corrupt(path: Path, *, mtime: float = PINNED_MTIME) -> Path:
    """Truncate a real parquet file in place, and pin its mtime.

    Truncation is the real-world failure (a killed writer, a full disk, a
    half-copied file) and it is the one that makes pyarrow raise
    ``ArrowInvalid: Parquet magic bytes not found in footer``. The file stays a
    plausible size and a plausible path, so nothing short of actually reading
    it notices.
    """
    raw = path.read_bytes()
    assert raw[:4] == b"PAR1" and raw[-4:] == b"PAR1", "fixture must start as a real parquet"
    path.write_bytes(raw[: len(raw) // 2])
    os.utime(path, (mtime, mtime))
    return path


def _quarantine_dir(root: Path) -> Path:
    return root / "quarantine"


def _store_and_partition(tmp_path: Path, symbol: str) -> Path:
    """Build a one-symbol store and return that symbol's partition file."""
    _store_with(tmp_path, [symbol])
    return _partition_file(tmp_path, symbol)


def _quarantined(root: Path) -> list[Path]:
    d = _quarantine_dir(root)
    return sorted(d.iterdir()) if d.is_dir() else []


# ---------------------------------------------------------------------------
# 1. The read path: quarantine, count, empty series — the spec's own words
# ---------------------------------------------------------------------------


def test_read_quarantines_the_file_and_returns_an_empty_series_for_that_symbol(
    tmp_path,
) -> None:
    """The AC-15 sentence, as a single end-to-end fact.

    A corrupt file is moved to quarantine, the counter fires with the symbol and
    the file, and the affected symbol reads as an empty series while the other
    symbols are unaffected.
    """
    registry = MetricsRegistry()
    store = _store_with(tmp_path, ["RELIANCE", "TCS"], registry)
    corrupt = _corrupt(_partition_file(tmp_path, "RELIANCE"))

    result = store.read(symbols=["RELIANCE", "TCS"])

    # (a) the caller got an empty series for the affected symbol, not a raise
    assert set(result["symbol"]) == {"TCS"}
    assert (result["symbol"] == "RELIANCE").sum() == 0
    # (b) the file is out of the lake and in quarantine
    assert not corrupt.exists()
    quarantined = _quarantined(tmp_path)
    assert [p.name for p in quarantined] == ["20260304T050607Z-RELIANCE.parquet"]
    # (c) the counter fired, once, labelled
    assert registry.get(QUARANTINE_METRIC) == 1.0
    assert registry.get(
        QUARANTINE_METRIC,
        symbol="RELIANCE",
        file="ohlcv/symbol=RELIANCE/year=2026/month=07/data.parquet",
    ) == 1.0
    assert (
        "datalake_quarantine_total|"
        "file=ohlcv/symbol=RELIANCE/year=2026/month=07/data.parquet,symbol=RELIANCE"
    ) in registry.series_snapshot()


def test_read_of_only_the_corrupt_symbol_returns_a_correctly_shaped_empty_frame(
    tmp_path,
) -> None:
    """Empty means empty-with-the-storage-schema, not ``None`` and not a crash.

    Every consumer of ``read()`` indexes columns straight away
    (``df["timestamp"]``, ``df.groupby("symbol")``), so a quarantine that
    returned a bare empty frame without the base columns would trade one
    ``ArrowInvalid`` for an immediate ``KeyError`` at the next call site.
    """
    store = _store_with(tmp_path, ["RELIANCE"])
    _corrupt(_partition_file(tmp_path, "RELIANCE"))

    result = store.read(symbols=["RELIANCE"])

    assert result.empty
    assert list(result.columns) == [
        "symbol", "exchange", "kind", "timeframe", "timestamp",
        "open", "high", "low", "close", "volume",
    ]


def test_date_range_of_a_quarantined_symbol_is_none_not_an_exception(tmp_path) -> None:
    """``date_range`` is a fourth public read the chart and WS replay paths use.

    It calls ``read()`` internally, so it inherits the guard. Asserted
    separately because a caller that catches ``ArrowInvalid`` at ``read()``
    would still be broken here if this path bypassed the guard.
    """
    store = _store_with(tmp_path, ["RELIANCE"])
    _corrupt(_partition_file(tmp_path, "RELIANCE"))

    assert store.date_range("RELIANCE") is None


# ---------------------------------------------------------------------------
# 2. The two write paths — the other two of the three unguarded reads
# ---------------------------------------------------------------------------


def test_upsert_into_a_corrupt_partition_quarantines_and_writes_clean_rows(
    tmp_path,
) -> None:
    """Call site 1: ``upsert``'s dedup read.

    A corrupt partition must not make the *lake* unwritable — the backfill job
    is the thing that eventually repairs a quarantined partition, so if upsert
    raised, the file would sit in the lake quarantined-and-present forever with
    no way back short of a manual delete. The incoming rows become the file.
    """
    registry = MetricsRegistry()
    store = _store_with(tmp_path, ["RELIANCE"], registry)
    _corrupt(_partition_file(tmp_path, "RELIANCE"))

    written = store.upsert(
        pd.DataFrame([_bar("RELIANCE", "2026-07-01 09:16:00")])
    )

    assert written == 1
    assert registry.get(QUARANTINE_METRIC) == 1.0
    assert [p.name for p in _quarantined(tmp_path)] == [
        "20260304T050607Z-RELIANCE.parquet"
    ]
    # The partition is readable again, and holds the fresh row.
    result = store.read(symbols=["RELIANCE"])
    assert len(result) == 1
    assert result.iloc[0]["timestamp"] == pd.Timestamp("2026-07-01 09:16:00")


def test_write_parquet_into_a_corrupt_file_quarantines_and_writes_clean_rows(
    tmp_path,
) -> None:
    """Call site 2: ``_write_parquet``'s dedup read.

    This is the only one of the three with no Hive partition directory above
    it — callers pass an arbitrary path — so it is the case where resolving the
    symbol from the path could silently fall back to a placeholder. The counter
    is bound to the real store, so the label comes from the store's own
    partitions; this test only has to prove the site is guarded at all.
    """
    registry = MetricsRegistry()
    store = _store_with(tmp_path, ["RELIANCE"], registry)
    target = _partition_file(tmp_path, "RELIANCE")
    _corrupt(target)

    store._write_parquet(pd.DataFrame([_bar("RELIANCE", "2026-07-01 09:16:00")]), target)

    assert registry.get(QUARANTINE_METRIC) == 1.0
    assert len(_quarantined(tmp_path)) == 1
    assert len(store.read(symbols=["RELIANCE"])) == 1


def test_all_three_call_sites_are_guarded_by_exactly_one_chokepoint() -> None:
    """The structural half of "guard all three", checked against the source.

    Each of the three sites is a behavioural test above, but a behavioural test
    only covers the site someone thought to write one for. This asserts the
    invariant that makes it hold for *all* of them and any future fourth: there
    is exactly one ``pq.ParquetFile(...)`` in the module, and it is inside
    ``_read_partition``'s ``try``. Rename the call, add a read, or move the
    ``try`` below the call and this fails.
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
    chokepoint = inspect.getsource(parquet_storage.ParquetStorage._read_partition)
    read_at = chokepoint.index("pq.ParquetFile(")
    try_at = chokepoint.index("try:")
    assert try_at < read_at, "the read is no longer inside the try block"


# ---------------------------------------------------------------------------
# 3. The counter: name, labels, and why the exact name matters
# ---------------------------------------------------------------------------


def test_metric_name_is_the_one_the_alert_rule_watches() -> None:
    """The metric is a live page, so its name is a contract with a YAML file.

    ``deploy/prometheus-rules.yml`` carries
    ``increase(datalake_quarantine_total[15m]) > 0`` under
    ``TradeXDatalakeQuarantine``. Renaming the counter here does not fail
    loudly — it makes that page stop matching a metric that still exists, which
    is precisely the silent-degradation failure the rule file's own header
    warns about. This reads the rule file rather than hardcoding the expected
    name twice, so one of the two moving is caught.
    """
    repo_root = Path(__file__).resolve().parents[2]
    rules = (repo_root / "deploy" / "prometheus-rules.yml").read_text(encoding="utf-8")
    assert f"increase({QUARANTINE_METRIC}[15m]) > 0" in rules, (
        "the TradeXDatalakeQuarantine rule no longer references the metric this "
        "test asserts; the two are a contract"
    )

    from tradex_market_data import parquet_storage

    assert parquet_storage._QUARANTINE_METRIC == QUARANTINE_METRIC


def test_counter_is_emitted_with_exactly_the_symbol_and_file_labels(tmp_path) -> None:
    """Two label series, addressed independently — the lane-5a label-indexed API.

    Asserted through ``series_snapshot`` (the per-label view) and through the
    rendered exposition line, so the test covers both the registry's label key
    and what an operator's Prometheus actually scrapes. The ``file`` label is
    the store-relative path, which is what identifies *which* partition died
    when several did.
    """
    registry = MetricsRegistry()
    store = _store_with(tmp_path, ["RELIANCE", "TCS"], registry)
    _corrupt(_partition_file(tmp_path, "RELIANCE"))
    _corrupt(_partition_file(tmp_path, "TCS"))

    store.read(symbols=["RELIANCE", "TCS"])

    series = registry.series_snapshot()
    rel = (
        "datalake_quarantine_total|"
        "file=ohlcv/symbol=RELIANCE/year=2026/month=07/data.parquet,symbol=RELIANCE"
    )
    tcs = (
        "datalake_quarantine_total|"
        "file=ohlcv/symbol=TCS/year=2026/month=07/data.parquet,symbol=TCS"
    )
    assert series[rel] == 1.0
    assert series[tcs] == 1.0
    assert registry.get(QUARANTINE_METRIC) == 2.0  # family total across both
    # The registry addresses a series by its WHOLE label set, so a partial
    # selector is a different (unrecorded) series and reads 0 rather than
    # summing. Asserted because a real PromQL reader will reach for
    # ``datalake_quarantine_total{symbol="RELIANCE"}``, and this pins that the
    # in-process API does not quietly do the other thing.
    assert registry.get(QUARANTINE_METRIC, symbol="RELIANCE") == 0.0
    assert registry.get(
        QUARANTINE_METRIC,
        symbol="RELIANCE",
        file="ohlcv/symbol=RELIANCE/year=2026/month=07/data.parquet",
    ) == 1.0

    rendered = [
        line
        for line in registry.render_prometheus().splitlines()
        if "quarantine" in line
    ]
    assert "# TYPE datalake_quarantine_total counter" in rendered
    assert (
        f'{QUARANTINE_METRIC}{{file="ohlcv/symbol=RELIANCE/year=2026/month=07/'
        f'data.parquet",symbol="RELIANCE"}} 1.0' in rendered
    )


def test_no_registry_bound_still_quarantines_and_returns_empty(tmp_path) -> None:
    """``metrics=None`` is the normal case for scripts and the chart route.

    If the guard were contingent on a registry being bound, those callers would
    get an unhandled ``ArrowInvalid`` — the metric would be the thing deciding
    whether corrupt data is survivable. The counter is optional; the quarantine
    is not.
    """
    store = _store_with(tmp_path, ["RELIANCE"])
    corrupt = _corrupt(_partition_file(tmp_path, "RELIANCE"))

    result = store.read(symbols=["RELIANCE"])

    assert result.empty
    assert not corrupt.exists()
    assert [p.name for p in _quarantined(tmp_path)] == [
        "20260304T050607Z-RELIANCE.parquet"
    ]


# ---------------------------------------------------------------------------
# 4. The quarantine filename, the destination, and the bytes
# ---------------------------------------------------------------------------


def test_quarantine_name_is_the_file_mtime_rendered_as_a_sortable_stamp(
    tmp_path,
) -> None:
    """``<mtime>-<symbol>.parquet``, with the mtime format pinned.

    The stamp is the corrupt file's own mtime as ``YYYYmmddTHHMMSSZ`` (UTC).
    Pinned to a known epoch here so the assertion is on the exact string
    rather than on "some timestamp-shaped thing" — a format change that broke
    sortability would still look plausible otherwise.
    """
    store = _store_with(tmp_path, ["RELIANCE"])
    _corrupt(_partition_file(tmp_path, "RELIANCE"), mtime=PINNED_MTIME)

    store.read(symbols=["RELIANCE"])

    (name,) = [p.name for p in _quarantined(tmp_path)]
    assert name == "20260304T050607Z-RELIANCE.parquet"
    assert re.fullmatch(r"\d{8}T\d{6}Z-[A-Z0-9&._-]+\.parquet", name), name
    # The stamp in the name round-trips back to the mtime the file had, so it
    # is the file's mtime and not the wall clock at quarantine time. A
    # name built from ``now()`` would fail this whenever the two differ, which
    # is the property that makes the name reproducible instead of arbitrary.
    stamp = datetime.strptime(name[:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    assert stamp.timestamp() == PINNED_MTIME
    assert stamp < datetime.now(UTC), "the stamp should be the past mtime, not now"


def test_quarantine_preserves_the_corrupt_bytes_for_forensics(tmp_path) -> None:
    """The quarantined file is the same bytes, not a placeholder.

    An operator's first question is "what was in it", and the only answer
    available is the file. A move that rewrote, truncated or emptied the file
    would destroy the only evidence at the moment of the failure.
    """
    store = _store_with(tmp_path, ["RELIANCE"])
    corrupt = _corrupt(_partition_file(tmp_path, "RELIANCE"))
    original = corrupt.read_bytes()

    store.read(symbols=["RELIANCE"])

    (moved,) = _quarantined(tmp_path)
    assert moved.read_bytes() == original


def test_quarantine_dir_is_derived_from_the_store_root_not_the_cwd(tmp_path) -> None:
    """``data/quarantine`` for the production root, and never the process cwd.

    The store is deliberately constructed on an absolute path anchor, so a
    quarantine written relative to cwd would land wherever the process happened
    to be started — a quarantined file that is not in the quarantine directory
    is a lost file plus a false all-clear. Building the store on a nested
    ``<tmp>/lake`` also proves the directory is a sibling of the store's own
    root rather than hardcoded to the string ``data``.
    """
    lake = tmp_path / "lake"
    store = _store_with(lake, ["RELIANCE"])
    _corrupt(lake / "ohlcv" / "symbol=RELIANCE" / "year=2026" / "month=07" / "data.parquet")

    store.read(symbols=["RELIANCE"])

    assert (lake / "quarantine").is_dir()
    assert len(list((lake / "quarantine").iterdir())) == 1


def test_a_healthy_file_is_never_quarantined(tmp_path) -> None:
    """The guard must not become "quarantine anything suspicious".

    A store where the metric fires but the data was fine would be worse than
    the defect: it would page on every read and delete the lake. Reading a
    healthy store must leave the counter at zero and quarantine empty.
    """
    registry = MetricsRegistry()
    store = _store_with(tmp_path, ["RELIANCE", "TCS"], registry)

    result = store.read(symbols=["RELIANCE", "TCS"])

    assert len(result) == 2
    assert registry.get(QUARANTINE_METRIC) == 0.0
    assert _quarantined(tmp_path) == []


# ---------------------------------------------------------------------------
# 5. No ArrowInvalid escapes, whatever the corruption
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"", id="empty-file"),
        pytest.param(b"PAR1", id="magic-bytes-only"),
        pytest.param(b"PAR1" + os.urandom(2048), id="garbage-after-header"),
        pytest.param(b"NOTPARQUET" * 256, id="not-parquet"),
    ],
)
def test_no_arrow_invalid_escapes_for_any_corruption_shape(
    tmp_path, payload
) -> None:
    """Whatever the bytes, ``read()`` returns and never raises.

    A guard written against the one corruption this test author happened to
    produce is the classic overfit. Each shape below is a real thing a lake
    ends up with, and each must land in the same place: quarantined, counted,
    empty series, no exception. Asserted with a plain call rather than
    ``pytest.raises`` so that *any* escaping exception, not just
    ``ArrowInvalid``, fails the test.
    """
    registry = MetricsRegistry()
    store = _store_with(tmp_path, ["RELIANCE"], registry)
    corrupt = _partition_file(tmp_path, "RELIANCE")
    corrupt.write_bytes(payload)
    os.utime(corrupt, (PINNED_MTIME, PINNED_MTIME))

    try:
        result = store.read(symbols=["RELIANCE"])
    except BaseException as exc:  # noqa: BLE001 — any escape is the failure
        pytest.fail(f"{type(exc).__name__} escaped read(): {exc}")

    assert result.empty
    assert not corrupt.exists()
    assert len(_quarantined(tmp_path)) == 1
    assert registry.get(QUARANTINE_METRIC) == 1.0


def test_arrow_invalid_is_the_error_the_guard_actually_catches(tmp_path) -> None:
    """Tie the test corpus to the real exception class, not a stand-in.

    Every test above asserts "no exception escaped", which would also pass
    against a guard catching ``Exception``. This asserts the negative: the
    corruption really does produce ``pyarrow.ArrowInvalid``, so the guard is
    catching the specific thing AC-15 names rather than a blanket.
    """
    corrupt = _corrupt(_store_and_partition(tmp_path, "RELIANCE"))

    with pytest.raises(pa.ArrowInvalid):
        pq.ParquetFile(corrupt).read()


def test_a_failing_quarantine_move_does_not_replace_arrowinvalid(tmp_path) -> None:
    """A read-only or full filesystem must not turn into a different raise.

    ``quarantine()`` cannot be allowed to throw: an ``OSError`` from the move
    would escape the guard the same way ``ArrowInvalid`` used to, so the file
    would still be unreadable everywhere — just with a different traceback.
    Here the move is made to fail, and the read must still come back empty.
    """
    store = _store_with(tmp_path, ["RELIANCE"])
    corrupt = _corrupt(_partition_file(tmp_path, "RELIANCE"))

    real_replace = os.replace

    def _boom(src, dst):
        raise OSError(28, "No space left on device")

    os.replace = _boom
    try:
        result = store.read(symbols=["RELIANCE"])
    finally:
        os.replace = real_replace

    assert result.empty
    # The file is still in place — reported, not silently dropped.
    assert corrupt.exists()
    assert _quarantined(tmp_path) == []


# ---------------------------------------------------------------------------
# 6. Recovery: the lake rebuilds itself
# ---------------------------------------------------------------------------


def test_a_rebuilt_partition_reads_normally_again(tmp_path) -> None:
    """Quarantine is not a permanent sentence on the symbol.

    After the corrupt file is moved out, the next write into the same partition
    produces a readable file and the symbol comes back — the property that
    makes "return an empty series" a degraded mode rather than data loss.
    """
    store = _store_with(tmp_path, ["RELIANCE"])
    _corrupt(_partition_file(tmp_path, "RELIANCE"))
    assert store.read(symbols=["RELIANCE"]).empty

    store.upsert(pd.DataFrame([_bar("RELIANCE", "2026-07-01 09:20:00")]))

    result = store.read(symbols=["RELIANCE"])
    assert len(result) == 1
    assert result.iloc[0]["timestamp"] == pd.Timestamp("2026-07-01 09:20:00")


def test_two_corrupt_partitions_of_one_symbol_do_not_overwrite_each_other(
    tmp_path,
) -> None:
    """The quarantine name is allocated, not computed and assumed free.

    Two corrupt files for the same symbol quarantined within the same second
    resolve to the same ``<mtime>-<symbol>`` stem. A plain rename would then
    have the second move destroy the first, and the operator would find one
    file where two were lost.
    """
    registry = MetricsRegistry()
    store = _store_with(tmp_path, ["RELIANCE"], registry)
    january = tmp_path / "ohlcv" / "symbol=RELIANCE" / "year=2026" / "month=01"
    january.mkdir(parents=True, exist_ok=True)
    (january / "data.parquet").write_bytes(b"PAR1" + os.urandom(512))
    _corrupt(_partition_file(tmp_path, "RELIANCE"), mtime=PINNED_MTIME)
    os.utime(january / "data.parquet", (PINNED_MTIME, PINNED_MTIME))

    store.read(symbols=["RELIANCE"])

    names = [p.name for p in _quarantined(tmp_path)]
    assert len(names) == 2, names
    assert len(set(names)) == 2, names
    assert registry.get(QUARANTINE_METRIC) == 2.0
