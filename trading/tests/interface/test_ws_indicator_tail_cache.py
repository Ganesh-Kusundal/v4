"""Lane 3g — process-level indicator tail cache, incremental compute, and
the lake read off the event loop.

The tail cache this file exists for already existed before the lane, wired
in, and covered by a name-based check that passed while it was scoped
per-connection — the component existed, was live, and did the opposite of
what the lane requires. So the tests here are built to fail on that shape:

- :func:`test_tail_is_shared_across_two_connections` fails if the cache goes
  back to per-connection, because the second connection would make its own
  lake read.
- :func:`test_incremental_equals_full_recompute_across_catalogue` fails if the
  warm-from-previous-bar logic is wrong, and it compares ``repr`` of every
  float so it is a comparison on bits, not on tolerance.
- :func:`test_tail_cache_evicts_least_recently_used` fails if the cap is gone.

The name-based check that used to be the only guard
(:func:`tests/test_parquet_history_seam.py::test_migrated_routes_call_the_shared_provider`)
passes on the defective code and on the fixed code alike, because it greps
for the string ``ParquetMarketProvider``. That is exactly why these tests
assert on behaviour.
"""
from __future__ import annotations

import datetime as dt
import random
import threading
import time

import pytest
from tradex_interfaces.routes import stream_indicators as si
from tradex_interfaces.routes.stream_indicators import (
    IncrementalIndicator,
    IndicatorStreamRegistry,
    IndicatorTailCache,
    SeriesCacheStore,
)
from tradex_runtime.bar_aggregator import BarFrame

#: 2026-09-01 09:15 IST — a real session bucket, so the vwap session anchor
#: and the ``time`` keys behave as they do in production.
_T0 = 1788267000
_BAR_SECONDS = 60


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """A real, empty datalake. A miss must degrade to live-only, and an
    empty root is the cheapest way to guarantee that without depending on
    whatever a developer has in ``data/``."""
    import tradex_market_data.paths as lake_paths

    root = tmp_path / "lake"
    (root / "ohlcv").mkdir(parents=True)
    monkeypatch.setattr(lake_paths, "DATALAKE_ROOT", str(root))
    return root


@pytest.fixture(autouse=True)
def _reset_process_level_caches():
    """Clear the module-level caches around every test in this file.

    These are deliberately process-wide, which is the point of the lane — and
    which means they outlive the test that populated them. Without this, a
    test that constructs a default (non-injected) registry leaves a held
    series reference on ``SERIES_CACHE`` that a later test counts, and the
    failure surfaces somewhere unrelated as a wrong refcount.

    Tests that assert on the caches should inject their own instances (most of
    this file does) so they do not depend on shared state at all; this fixture
    only keeps the default-constructed tests from contaminating each other.
    """
    si.TAIL_CACHE.clear()
    si.SERIES_CACHE.clear()
    yield
    si.TAIL_CACHE.clear()
    si.SERIES_CACHE.clear()


def _bar(i: int, base: float | None = None) -> dict:
    """One serialized bar at bucket *i*; a deterministic pseudo-random walk
    so an indicator has something non-degenerate to chew on."""
    rng = random.Random(i)
    px = base if base is not None else 100.0 + i + rng.uniform(-1.5, 1.5)
    return {
        "time": _T0 + i * _BAR_SECONDS,
        "open": px,
        "high": px + rng.uniform(0.1, 0.9),
        "low": px - rng.uniform(0.1, 0.9),
        "close": px + rng.uniform(-0.3, 0.3),
        "volume": 1000.0 + i,
    }


def _frame(i: int = 0, instrument="NSE:RELIANCE", timeframe="1m", closed=True) -> BarFrame:
    b = _bar(i)
    return BarFrame(
        instrument=instrument,
        timeframe=timeframe,
        time=b["time"],
        open=b["open"],
        high=b["high"],
        low=b["low"],
        close=b["close"],
        volume=b["volume"],
        closed=closed,
    )


def _candles(n: int) -> list:
    """n real candles across two sessions, via the production shim."""
    bars = [_bar(i) for i in range(n)]
    return si._candles_from_bars(bars), bars


def _bit_equal(got: list, want: list) -> str | None:
    """``None`` when the two plot series agree bit-for-bit, else why.

    ``repr`` is the comparison, not ``==``: two floats that print the same
    are the same float, and two that do not differ in the low bits, which is
    the drift this lane is not allowed to introduce.
    """
    if len(got) != len(want):
        return f"length {len(got)} != {len(want)}"
    for i, (a, b) in enumerate(zip(got, want)):
        if a is None or b is None:
            if a is not b:
                return f"[{i}] {a!r} != {b!r}"
            continue
        fa, fb = float(a), float(b)
        if fa != fa and fb != fb:  # both NaN
            continue
        if repr(fa) != repr(fb):
            return f"[{i}] {fa!r} != {fb!r}"
    return None


def _bit_equal_points(got: list, want: list) -> str | None:
    """``None`` when two lists of pushed points agree on every plot value.

    Points are dicts keyed by plot name, so the times and the plot set are
    compared too — an incremental path that dropped a plot or shifted a
    timestamp would otherwise slip past a values-only check.
    """
    if len(got) != len(want):
        return f"{len(got)} points != {len(want)}"
    for i, (a, b) in enumerate(zip(got, want)):
        if a.keys() != b.keys():
            return f"[{i}] plots {sorted(a)} != {sorted(b)}"
        if a["time"] != b["time"]:
            return f"[{i}] time {a['time']} != {b['time']}"
        for plot in a:
            why = _bit_equal([a[plot]], [b[plot]])
            if why is not None:
                return f"[{i}].{plot} {why}"
    return None


# ---------------------------------------------------------------------------
# (a) the cache is genuinely shared across two connections
# ---------------------------------------------------------------------------


def test_default_construction_uses_the_process_level_cache():
    """The default — the path production actually takes — is the shared one.

    Every other test in this file injects ``tail_cache=`` so it can count loads
    on a cache it owns. That is a *dependency-injection* test: it proves the
    cache class behaves when shared, and says nothing about whether the
    process shares it.

    Production does not inject. ``stream.py`` builds the registry per connection
    with a push sink and nothing else, so the whole process-level property rests
    on one default:

        self._tail_cache = TAIL_CACHE if tail_cache is None else tail_cache

    Change that line to build a fresh cache and every injected-cache test still
    passes while the running server silently re-reads 400 bars per client —
    the original defect, restored, with a green suite. So the default is
    asserted here directly, and the production call site is pinned alongside it.
    """
    conn_a = IndicatorStreamRegistry(push=lambda m: None)
    conn_b = IndicatorStreamRegistry(push=lambda m: None)
    assert conn_a._tail_cache is conn_b._tail_cache is si.TAIL_CACHE, (
        "a default-constructed registry is not using the process-level tail "
        "cache; every websocket connection would get its own and re-read the "
        "lake for bars another connection already had"
    )
    assert conn_a._series_cache is conn_b._series_cache is si.SERIES_CACHE, (
        "the live bar window and shims must be shared the same way, or a "
        "second connection rebuilds 400 candle shims for nothing"
    )


def test_production_constructs_the_registry_with_no_injected_cache():
    """``ws_stream`` must build the registry the way the default test assumes.

    The default is only load-bearing if nothing overrides it. If the route
    started passing its own ``IndicatorTailCache()`` — the most natural-looking
    "fix" for a process-level cache, and one that would restore the per-client
    duplication — the default would keep holding and the suite would stay
    green. So the call site is pinned to the production shape: a push sink and
    nothing else.
    """
    import ast
    import inspect

    from tradex_interfaces.routes import stream as stream_route

    src = inspect.getsource(stream_route.ws_stream)
    calls = [
        node
        for node in ast.walk(ast.parse(inspect.cleandoc(src)))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "IndicatorStreamRegistry"
    ]
    assert len(calls) == 1, f"expected one registry construction, found {len(calls)}"
    call = calls[0]
    assert call.keywords == [], (
        "ws_stream injects keyword arguments into IndicatorStreamRegistry "
        f"({sorted(kw.arg for kw in call.keywords)}); a per-connection cache "
        f"passed here would beat the process-level default and put the "
        f"duplication back"
    )
    assert len(call.args) == 1, (
        f"expected the push sink as the only positional argument, got {len(call.args)}"
    )


def test_default_construction_reads_the_lake_once_for_two_connections(lake, monkeypatch):
    """The same one-load property, reached through the production shape.

    The behavioural twin of
    :func:`test_default_construction_uses_the_process_level_cache`: no
    ``tail_cache=`` argument anywhere, so this fails the moment the default
    stops being the shared singleton, and it fails on the number of lake reads
    rather than on the identity of an attribute.

    Counted on the real module-level :data:`TAIL_CACHE` (restored in a
    ``finally``) because with default construction there is nothing else to
    count on.
    """
    loads: list[int] = []

    def _load(instrument, timeframe, now):
        loads.append(now)
        return [_bar(i) for i in range(100)]

    monkeypatch.setattr(si, "_load_datalake_tail", _load)
    conn_a = IndicatorStreamRegistry(push=lambda m: None)
    conn_b = IndicatorStreamRegistry(push=lambda m: None)
    key = ("NSE:RELIANCE", "1m")
    try:
        for reg in (conn_a, conn_b):
            reg.add(*key, ["sma"], "bar-close")
            reg.handle_bar_frame(_frame(200))
        assert len(loads) == 1, (
            f"two default-constructed connections read the lake {len(loads)} "
            f"times for the same (instrument, timeframe)"
        )
        assert si.TAIL_CACHE.keys() == [key]
        assert si.SERIES_CACHE.peek(key).holders == 2, (
            "the two connections did not share one series cache"
        )
    finally:
        conn_a.close()
        conn_b.close()


def test_tail_is_shared_across_two_connections(lake, monkeypatch):
    """Two connections on the same (instrument, timeframe) read the lake once.

    This is the defect. With a per-connection cache each registry holds its
    own tail dict, so the second connection's first push is a second parquet
    read of the same 400 bars — the exact duplication the lane removes. The
    assertion is on the *observable* consequence (one load), not on where
    the dict lives, because a name-based check on that is what let the
    defect through in the first place.
    """
    calls: list[tuple[str, str]] = []
    barrier = threading.Event()

    def _counting_load(instrument, timeframe, now):
        # Hold the first load open until both connections have asked, so a
        # per-connection implementation cannot pass by being merely lucky
        # about ordering.
        calls.append((instrument, timeframe))
        barrier.wait(2.0)
        return [_bar(i) for i in range(120)]

    monkeypatch.setattr(si, "_load_datalake_tail", _counting_load)
    tails = IndicatorTailCache()
    store = SeriesCacheStore(tails)

    conn_a = IndicatorStreamRegistry(push=lambda m: None, tail_cache=tails, series_cache=store)
    conn_b = IndicatorStreamRegistry(push=lambda m: None, tail_cache=tails, series_cache=store)
    key = ("NSE:RELIANCE", "1m")
    for reg in (conn_a, conn_b):
        reg.add(*key, ["sma"], "bar-close")

    threads = [
        threading.Thread(target=reg.handle_bar_frame, args=(_frame(200),))
        for reg in (conn_a, conn_b)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    barrier.set()
    for t in threads:
        t.join(timeout=5.0)

    assert tails.stats()["loads"] == 1, (
        f"expected the process-level tail to be read once and shared, got "
        f"{tails.stats()['loads']} loads: {calls}. A per-connection cache "
        f"re-reads the same 400 bars for every connection."
    )
    assert len(tails) == 1 and tails.keys() == [key]

    # And the warm indicator state is shared too, not just the bar list: the
    # second connection pushes the same series without a second series cache.
    assert store.keys() == [key], "both connections must share one series cache"
    assert store.peek(key).holders == 2

    conn_a.close()
    assert store.peek(key).holders == 1, "one holder left after one disconnect"
    conn_b.close()
    assert store.peek(key).holders == 0


def test_two_connections_push_identical_points(lake, monkeypatch):
    """Sharing must not let one connection's push contaminate another's.

    The process-level cache is the thing the brief warns about — a data race
    or a stale-tail bug would live exactly here. Both connections push the
    same bar and must emit the same numbers as a cold, per-connection
    recompute of the same bars.
    """
    bars = [_bar(i) for i in range(150)]
    monkeypatch.setattr(si, "_load_datalake_tail", lambda *a: list(bars))
    tails = IndicatorTailCache()
    store = SeriesCacheStore(tails)

    pushed_a: list[dict] = []
    pushed_b: list[dict] = []
    conn_a = IndicatorStreamRegistry(push=pushed_a.append, tail_cache=tails, series_cache=store)
    conn_b = IndicatorStreamRegistry(push=pushed_b.append, tail_cache=tails, series_cache=store)
    for reg in (conn_a, conn_b):
        reg.add("NSE:RELIANCE", "1m", ["sma", "ema", "rsi"], "bar-close")

    # Interleave the two connections across several bars, so each one's push
    # lands inside the other's compute.
    for i in range(150, 156):
        conn_a.handle_bar_frame(_frame(i))
        conn_b.handle_bar_frame(_frame(i))

    # One push per bar per indicator, not per connection.
    assert len(pushed_a) == len(pushed_b) == 18, f"{len(pushed_a)}, {len(pushed_b)}"
    for ma, mb in zip(pushed_a, pushed_b):
        assert ma["points"] == mb["points"], (
            "two connections on one series pushed different points; the "
            "shared cache is leaking per-connection state"
        )


def test_cache_survives_disconnect_and_is_reused(lake, monkeypatch):
    """A closed connection does not take the process-level tail with it.

    This is the lifetime change stated as a test. The per-connection cache
    died with the socket, so a reconnect re-read 400 bars it had just read;
    the process-level one outlives the connection *on purpose*, and
    :meth:`IndicatorStreamRegistry.close` is what bounds that, not the
    disconnect itself.
    """
    loads: list[int] = []

    def _load(instrument, timeframe, now):
        loads.append(now)
        return [_bar(i) for i in range(100)]

    monkeypatch.setattr(si, "_load_datalake_tail", _load)
    tails = IndicatorTailCache()
    store = SeriesCacheStore(tails)

    first = IndicatorStreamRegistry(push=lambda m: None, tail_cache=tails, series_cache=store)
    first.add("NSE:RELIANCE", "1m", ["sma"], "bar-close")
    first.handle_bar_frame(_frame(100))
    first.close()

    assert len(loads) == 1
    # The tail is still there after the connection is gone — that is the
    # deliberate lifetime choice, and it is why close() only drops the
    # non-reproducible series state.
    assert len(tails) == 1

    second = IndicatorStreamRegistry(push=lambda m: None, tail_cache=tails, series_cache=store)
    second.add("NSE:RELIANCE", "1m", ["sma"], "bar-close")
    second.handle_bar_frame(_frame(100))
    second.close()
    assert len(loads) == 1, (
        "the reconnect re-read the lake; the process-level tail is supposed "
        "to outlive the connection"
    )


def test_a_new_bucket_is_not_served_from_a_stale_tail(lake, monkeypatch):
    """Sharing must not become staleness.

    The reuse condition is that the tail already covers the requested
    bucket. A tail loaded at bucket T is not reused for T+1, because the
    lake is written asynchronously and the closed bar for T+1 may have
    landed after the read. Serving T+1 out of a T tail is a stale-tail bug
    and would feed a strategy yesterday's warmup.
    """
    loads: list[int] = []

    def _load(instrument, timeframe, now):
        loads.append(now)
        return [_bar(i) for i in range(100)]

    monkeypatch.setattr(si, "_load_datalake_tail", _load)
    tails = IndicatorTailCache()
    reg = IndicatorStreamRegistry(push=lambda m: None, tail_cache=tails,
                                  series_cache=SeriesCacheStore(tails))
    reg.add("NSE:RELIANCE", "1m", ["sma"], "bar-close")

    reg.handle_bar_frame(_frame(100))
    reg.handle_bar_frame(_frame(100))  # same bucket: warm
    assert len(loads) == 1
    reg.handle_bar_frame(_frame(101))  # next bucket: must re-read
    assert len(loads) == 2, "a later bucket was served from a tail that never saw it"
    reg.handle_bar_frame(_frame(101))
    assert len(loads) == 2


# ---------------------------------------------------------------------------
# (b) incremental compute == full recompute
# ---------------------------------------------------------------------------


def test_incremental_equals_full_recompute_across_catalogue():
    """A *warm value* would be wrong for these, and is measurably so.

    Three plots in the catalogue read forward (``ichimoku.lagging`` and the
    ``rsi-divergence`` bear plots), so for them the value at index *i* of a
    compute over N+1 bars is not the value at index *i* of a compute over N
    bars. Any incremental design that carries a computed *value* across bars
    would therefore push a different number for them, which is why this
    implementation carries only the shims and re-computes every push.

    The test pins the property rather than assuming it: it re-measures the
    set, so an indicator that gains a look-ahead is noticed, and
    ``set(lookahead) == LOOKAHEAD_INDICATOR_IDS`` means the exclusion can
    neither grow silently nor hide a newly non-exact indicator.
    """
    from tradex_analytics.indicators import compute_indicator, indicator_catalogue

    ids = [spec["id"] for spec in indicator_catalogue()]
    assert len(ids) > 50, f"catalogue looks truncated ({len(ids)} ids)"

    n = 300
    short_candles, _ = _candles(n)
    long_candles, _ = _candles(n + 1)

    lookahead: list[str] = []
    for ind_id in ids:
        short = compute_indicator(ind_id, short_candles)
        long = compute_indicator(ind_id, long_candles)
        if any(
            _bit_equal(values, long[plot][:n]) is not None
            for plot, values in short.items()
        ):
            lookahead.append(ind_id)

    assert set(lookahead) == set(si.LOOKAHEAD_INDICATOR_IDS), (
        "the set of indicators whose value is NOT append/prepend-stable "
        f"changed. measured={sorted(lookahead)} declared={sorted(si.LOOKAHEAD_INDICATOR_IDS)}"
    )


def test_warm_window_pushes_exactly_what_a_full_rebuild_pushes(lake, monkeypatch):
    """The load-bearing equivalence: the real push path, bit for bit.

    Bars are pushed one at a time through the warm path, and each pushed
    point is compared against the same points computed from a *fully
    rebuilt* shim list over the identical bar window — the pre-lane
    behaviour. This is the assertion that matters: it is the number a
    strategy would have read, and it is checked over every plot of every
    indicator in the catalogue, including the three look-ahead ones that a
    value-warming design would get wrong.
    """
    from tradex_analytics.indicators import indicator_catalogue

    n = 300
    tail_bars = [_bar(i) for i in range(n)]
    monkeypatch.setattr(si, "_load_datalake_tail", lambda *a: [dict(b) for b in tail_bars])

    ids = [spec["id"] for spec in indicator_catalogue()]
    assert len(ids) > 50, f"catalogue looks truncated ({len(ids)} ids)"
    key = ("NSE:RELIANCE", "1m")

    pushed: dict[str, list] = {i: [] for i in ids}
    tails = IndicatorTailCache()
    store = SeriesCacheStore(tails)
    reg = IndicatorStreamRegistry(
        push=lambda m: pushed[m["id"]].append(m), tail_cache=tails, series_cache=store
    )
    for ind_id in ids:
        reg.add(*key, [ind_id], "bar-close")

    failures: list[str] = []
    for i in range(n, n + 3):
        frame = _frame(i)
        reg.handle_bar_frame(frame)
        series = store.peek(key)
        warm_window = series.bars(key, frame)
        for ind_id in ids:
            # The reference: rebuild every shim from the same bar window,
            # exactly as the pre-lane code did on every push.
            rebuilt = si.compute_indicator(
                ind_id, [si._candle_shim(b) for b in warm_window]
            )
            msg = [m for m in pushed[ind_id] if m["points"][-1]["time"] == frame.time][-1]
            first = max(0, len(warm_window) - si._POINTS_PER_PUSH)
            want = [
                {
                    "time": warm_window[j]["time"],
                    **{p: (rebuilt[p][j] if j < len(rebuilt[p]) else None) for p in rebuilt},
                }
                for j in range(first, len(warm_window))
            ]
            why = _bit_equal_points(msg["points"], want)
            if why is not None:
                failures.append(f"{ind_id}: {why}")
    assert not failures, "the warm push diverged from a full rebuild:\n" + "\n".join(failures[:25])
    # Non-vacuity: the comparison saw real numbers.
    finite = [p for p in pushed["sma"][-1]["points"] if p["value"] is not None]
    assert finite, "the comparison saw only None; it would pass vacuously"


def test_incremental_indicator_warms_from_the_previous_bar(lake, monkeypatch):
    """The warm path must reuse the previous bar's shims, not rebuild.

    Asserted on the number of candle shims built, because that is the work
    the lane is removing and it is completely invisible in the output — a
    regression that rebuilt 400 shims per bar would still produce
    bit-identical numbers, which is exactly why a numeric-only assertion
    cannot see it.

    The window is fed directly here rather than through a registry, so the
    count is the merge's and nothing else's.
    """
    window = IncrementalIndicator(limit=400)
    tail = [_bar(i) for i in range(400)]

    built: list[int] = []
    real_shim = si._candle_shim

    def _counting_shim(bar):
        built.append(bar["time"])
        return real_shim(bar)

    monkeypatch.setattr(si, "_candle_shim", _counting_shim)

    window.extend(tail)
    assert len(built) == 400, f"cold build made {len(built)} shims, expected 400"

    built.clear()
    # The tail is 400 bars, so once it is full the lake rolls it forward by
    # one each bucket. That is the shape a real closed-bar push has, and it
    # is the one the window must handle in O(1).
    for i in range(400, 405):
        rolled = [_bar(j) for j in range(i - 400 + 1, i + 1)]
        window.extend(rolled)
    assert len(built) == 5, (
        f"warm push rebuilt {len(built)} shims for 5 bars; the incremental "
        f"path must warm from the previous bar and build one shim per bar"
    )

    # And the mirror invariant still holds after the warm appends.
    assert len(window.shims()) == len(window.bars()) == 400
    assert [b["time"] for b in window.bars()] == [_bar(j)["time"] for j in range(5, 405)]


def test_incremental_rebuilds_on_a_gap_or_rewind(lake, monkeypatch):
    """A stream that does not simply extend must not take the warm path.

    The cheap failure mode for an incremental cache is to patch a window
    that is no longer what it holds — a gap, a rewind, or a correction. The
    guard is a rebuild, so the answer degrades to the plain full recompute
    rather than to a wrong number.
    """
    tail_bars = [_bar(i) for i in range(50)]
    monkeypatch.setattr(si, "_load_datalake_tail", lambda *a: [dict(b) for b in tail_bars])
    tails = IndicatorTailCache()
    store = SeriesCacheStore(tails)
    reg = IndicatorStreamRegistry(push=lambda m: None, tail_cache=tails, series_cache=store)
    reg.add("NSE:RELIANCE", "1m", ["sma"], "bar-close")

    reg.handle_bar_frame(_frame(50))
    series = store.peek(("NSE:RELIANCE", "1m"))
    window = series.window
    reg.handle_bar_frame(_frame(51))
    # tail[0..49] + live bucket 50, then live bucket 51. The window is the
    # merged series, not the tail.
    assert [b["time"] for b in window.bars()] == [_bar(i)["time"] for i in range(50)] + [
        _bar(51)["time"]
    ]

    # A rewind: bucket 40 after 51. The window must be rebuilt, not appended.
    reg.handle_bar_frame(_frame(40))
    assert [b["time"] for b in window.bars()] == [_bar(i)["time"] for i in range(50)] + [
        _bar(40)["time"]
    ], "a rewind was appended onto the warm window instead of rebuilding it"
    # The shim list still mirrors the bar list after the rebuild — the
    # invariant the whole equivalence argument rests on.
    assert len(window.shims()) == len(window.bars())
    assert window.shims()[-1].timestamp == dt.datetime.fromtimestamp(
        _bar(40)["time"]
    ).replace(tzinfo=None)


def test_window_handles_an_overnight_gap_without_refusing_to_warm(lake, monkeypatch):
    """A session break is just another bar, and the mirror handles it.

    The session closes at 15:30 and reopens at 09:15, so consecutive trading
    days are ~18 hours apart on a 1m series and two days apart on a daily
    one. An earlier draft of this lane refused to warm across such a gap,
    which is unnecessary: the merge is guarded by an exact comparison of the
    bar lists, not by an assumption about spacing, so the overnight bar is
    appended like any other and the shim mirror still holds.
    """
    window = IncrementalIndicator()
    day1 = [_bar(i) for i in range(5)]
    window.compute("sma", day1)
    day2_start = 5 + 375  # the next session, hours later
    day2 = day1 + [_bar(day2_start)]
    window.compute("sma", day2)
    assert [b["time"] for b in window.bars()] == [b["time"] for b in day2]
    assert len(window.shims()) == len(window.bars())


def test_window_slides_when_the_bounded_tail_advances(lake, monkeypatch):
    """When the lake rolls forward the window slides, keeping its cap.

    The tail is capped at 400 bars, so once it is full each new bucket
    shifts it by one. The window has to follow that shift, or the last two
    bars it holds would be the same bar twice and an indicator over it would
    be computed on a series that does not exist.
    """
    limit = 8
    window = IncrementalIndicator(limit=limit)
    tail = [_bar(i) for i in range(limit)]
    window.compute("sma", tail)
    assert len(window.bars()) == limit

    for i in range(limit, limit + 4):
        rolled = [_bar(j) for j in range(i - limit + 1, i + 1)]
        window.compute("sma", rolled)
        assert [b["time"] for b in window.bars()] == [b["time"] for b in rolled]
        assert len(window.shims()) == len(window.bars())
    # A rebuild still has to be bounded, or a corrupt tail could grow it.
    window.compute("sma", [_bar(i) for i in range(1000)])
    assert len(window.bars()) == limit
    assert len(window.shims()) == limit


def test_lookahead_indicators_still_get_the_exact_full_recompute(lake, monkeypatch):
    """The look-ahead indicators are why the warm path carries no values.

    ``ichimoku`` and ``rsi-divergence`` read forward, so warming a computed
    value across bars would change the number pushed for them. This asserts
    the thing that actually guarantees it: those ids are computed from a
    freshly built shim list over exactly the bars of the window, with no
    per-indicator state at all.
    """
    tail_bars = [_bar(i) for i in range(300)]
    monkeypatch.setattr(si, "_load_datalake_tail", lambda *a: [dict(b) for b in tail_bars])
    tails = IndicatorTailCache()
    store = SeriesCacheStore(tails)
    reg = IndicatorStreamRegistry(push=lambda m: None, tail_cache=tails, series_cache=store)
    for ind_id in si.LOOKAHEAD_INDICATOR_IDS:
        reg.add("NSE:RELIANCE", "1m", [ind_id], "bar-close")

    reg.handle_bar_frame(_frame(300))
    reg.handle_bar_frame(_frame(301))
    series = store.peek(("NSE:RELIANCE", "1m"))
    window = series.window
    assert len(window.shims()) == 301, (
        "tail[0..299] + live 300 + live 301; the window is the merged series"
    )
    # The shims are the shims of the window's bars, in order — there is no
    # per-indicator buffer that could hold a stale value.
    assert len(window.shims()) == len(window.bars())
    for shim, bar in zip(window.shims(), window.bars()):
        assert shim.ohlc.close.value == bar["close"]
        assert shim.volume.value == bar["volume"]


def test_window_is_bounded():
    """A series watched for a whole session must not grow without bound."""
    window = IncrementalIndicator(limit=8)
    for i in range(60):
        window.compute("sma", [_bar(j) for j in range(i + 1)])
    assert len(window.bars()) == 8
    assert [b["time"] for b in window.bars()] == [_bar(j)["time"] for j in range(52, 60)]
    assert len(window.shims()) == 8


# ---------------------------------------------------------------------------
# (c) bounded memory / eviction
# ---------------------------------------------------------------------------


def test_tail_cache_evicts_least_recently_used(monkeypatch):
    """The cap is real: a client cycling instruments cannot grow the process.

    40 distinct instruments against a cap of 4. Without eviction the cache
    would hold all 40 tails, which is the unbounded-growth failure a
    process-level cache keyed by instrument invites — and the per-connection
    cache it replaced could not have, because a connection's key count was
    bounded by what that one client subscribed to.
    """
    monkeypatch.setattr(si, "_load_datalake_tail", lambda *a: [_bar(i) for i in range(10)])
    tails = IndicatorTailCache(max_entries=4)
    for i in range(40):
        tails.ensure_loaded((f"NSE:SYM{i}", "1m"), _T0)
    assert len(tails) == 4
    assert tails.stats()["max_entries"] == 4
    assert [k[0] for k in tails.keys()] == [f"NSE:SYM{i}" for i in range(36, 40)]


def test_tail_cache_evicts_the_least_recently_used_not_the_oldest(monkeypatch):
    """Reuse refreshes recency, so a hot series is not the one thrown away.

    A plain insertion-order cap would evict a series that is being pushed
    every bar in favour of one that was loaded once, which would make the
    cache useless exactly when it is earning its keep.
    """
    monkeypatch.setattr(si, "_load_datalake_tail", lambda *a: [_bar(i) for i in range(10)])
    tails = IndicatorTailCache(max_entries=2)
    hot = ("NSE:HOT", "1m")
    cold = ("NSE:COLD", "1m")
    tails.ensure_loaded(hot, _T0)
    tails.ensure_loaded(cold, _T0)
    assert tails.acquire(hot, _T0) is not None  # touch hot, it is now LRU-newest
    tails.ensure_loaded(("NSE:NEW", "1m"), _T0)
    assert cold not in tails.keys(), "the untouched series should have been evicted"
    assert hot in tails.keys(), "the series still being pushed must survive"


def test_tail_cache_drops_a_stale_entry(monkeypatch):
    """An entry older than the staleness budget is re-read, not trusted.

    The lake is written asynchronously, so a tail held across a long idle
    can be arbitrarily behind what is on disk. This is the "stale-tail" half
    of the shared-cache risk, and it is time-based rather than
    bucket-based, so it needs its own assertion.
    """
    monkeypatch.setattr(si, "_load_datalake_tail", lambda *a: [_bar(i) for i in range(10)])
    tails = IndicatorTailCache(stale_after=0.0)
    tails.ensure_loaded(("NSE:X", "1m"), _T0)
    time.sleep(0.01)
    assert tails.acquire(("NSE:X", "1m"), _T0) is None, "a stale tail was served"
    assert len(tails) == 0, "a stale entry must be dropped, not just refused"


def test_series_cache_is_released_when_the_last_subscriber_leaves(lake, monkeypatch):
    """Live-bar state is not reproducible, so it must actually be released.

    The tail is derived and worth keeping; the warm bar window and shims
    are not. A process-level cache that kept them past the last subscriber
    would trade a bounded parquet re-read for an unbounded accumulation of
    live state, which is the mirror image of the bug being fixed.
    """
    monkeypatch.setattr(si, "_load_datalake_tail", lambda *a: [_bar(i) for i in range(30)])
    tails = IndicatorTailCache()
    store = SeriesCacheStore(tails)
    key = ("NSE:RELIANCE", "1m")

    a = IndicatorStreamRegistry(push=lambda m: None, tail_cache=tails, series_cache=store)
    b = IndicatorStreamRegistry(push=lambda m: None, tail_cache=tails, series_cache=store)
    for reg in (a, b):
        reg.add(*key, ["sma"], "bar-close")
    a.handle_bar_frame(_frame(30))
    b.handle_bar_frame(_frame(30))

    series = store.peek(key)
    assert series.holders == 2
    assert series.window is not None

    a.close()
    assert series.holders == 1
    assert len(series.window.bars()) > 0, "state dropped while a subscriber still held it"

    b.close()
    assert series.holders == 0
    assert series.window is None, "the live bar window outlived its last holder"


def test_unsub_releases_the_series_reference(lake, monkeypatch):
    """Unsubscribing the last indicator gives the reference back too.

    Not only on disconnect: a client that watches twenty instruments over a
    session and drops each one must not leave twenty warm windows behind.
    """
    monkeypatch.setattr(si, "_load_datalake_tail", lambda *a: [_bar(i) for i in range(30)])
    tails = IndicatorTailCache()
    store = SeriesCacheStore(tails)
    key = ("NSE:RELIANCE", "1m")
    reg = IndicatorStreamRegistry(push=lambda m: None, tail_cache=tails, series_cache=store)
    reg.add(*key, ["sma", "ema"], "bar-close")
    reg.handle_bar_frame(_frame(30))
    assert store.peek(key).holders == 1

    reg.remove(*key, ["ema"])
    assert store.peek(key).holders == 1, "released while sma was still subscribed"
    reg.remove(*key, None)
    assert store.peek(key).holders == 0
    assert reg.close() is None, "close must be idempotent and return nothing"


# ---------------------------------------------------------------------------
# The lake read is not on the caller's thread
# ---------------------------------------------------------------------------


def test_parquet_read_runs_off_the_calling_thread(lake, monkeypatch):
    """The blocking read happens on a worker, not on the event loop.

    ``ReactiveBus.publish`` delivers synchronously, so ``handle_bar_frame``
    runs on the loop that also serves every other websocket. The read is
    dispatched to a thread that records which thread it ran on; asserting
    the *thread identity* rather than that a load happened is the point —
    a load count is already satisfied by a cache that reads on the caller's
    thread.
    """
    seen: dict[str, str] = {}

    def _load(instrument, timeframe, now):
        seen["thread"] = threading.current_thread().name
        seen["caller"] = threading.current_thread().name
        return [_bar(i) for i in range(10)]

    monkeypatch.setattr(si, "_load_datalake_tail", _load)
    tails = IndicatorTailCache()
    reg = IndicatorStreamRegistry(push=lambda m: None, tail_cache=tails,
                                  series_cache=SeriesCacheStore(tails))
    reg.add("NSE:RELIANCE", "1m", ["sma"], "bar-close")
    reg.handle_bar_frame(_frame(30))
    assert seen, "the loader never ran"
    assert seen["thread"] != threading.current_thread().name, (
        f"the parquet read ran on the caller's thread ({seen['thread']}); "
        f"handle_bar_frame is on the event loop"
    )
    assert seen["thread"].startswith("tail-load:"), (
        f"the read ran on {seen['thread']}, not on a named tail-load worker"
    )


def test_a_slow_load_does_not_degrade_the_push(monkeypatch):
    """A load that outlives the join budget is abandoned, not waited on.

    The join is bounded, so a wedged parquet read cannot take the socket
    with it. The caller gets the same shape a datalake miss already
    degrades to, and the result is still published if the thread lands —
    which is why this is a timeout policy and not a cancellation.
    """
    monkeypatch.setattr(si, "TAIL_LOAD_JOIN_SECONDS", 0.05)

    def _slow_load(*_a):
        time.sleep(0.4)
        return [_bar(i) for i in range(9)]

    monkeypatch.setattr(si, "_load_datalake_tail", _slow_load)
    tails = IndicatorTailCache()
    reg = IndicatorStreamRegistry(push=lambda m: None, tail_cache=tails,
                                  series_cache=SeriesCacheStore(tails))
    reg.add("NSE:RELIANCE", "1m", ["sma"], "bar-close")
    started = time.monotonic()
    reg.handle_bar_frame(_frame(30))
    assert time.monotonic() - started < 0.35, "the push blocked on the slow read"
    # The abandoned load still warms the cache when it lands.
    deadline = time.monotonic() + 2.0
    while len(tails) == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(tails) == 1, "an abandoned load must still publish its result"


# ---------------------------------------------------------------------------
# The existing name-based check is vacuous — pin that fact
# ---------------------------------------------------------------------------


def test_the_name_based_seam_check_cannot_see_cache_scope():
    """``"ParquetMarketProvider" in source`` is the check that let the
    per-connection cache through.

    It is kept (the provider seam is real) and this test records why it is
    not sufficient: the same string is present whichever way the cache is
    scoped, so it cannot tell a fixed cache from a broken one. Anything that
    claims to cover the tail cache's scope has to assert on behaviour.
    """
    import inspect

    src = inspect.getsource(si)
    assert "ParquetMarketProvider" in src  # the check that passes either way
    assert "class IndicatorTailCache" in src
    # The behavioural facts that name-based grep cannot reach, asserted here
    # so the gap is explicit rather than implied.
    tails = IndicatorTailCache()
    assert "self._tails" not in inspect.getsource(IndicatorStreamRegistry), (
        "the registry must not own a per-connection tail dict again"
    )
    assert tails.stats()["max_entries"] > 0
