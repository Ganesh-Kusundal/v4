"""AC-11: one bar grid for the whole platform, asserted on all three sites.

The live aggregator was anchored to the 09:15 session open. The two history
sites were not, so for one product the live feed and the chart read different
grids: a 30m backtest bar began at 09:00 where a 30m live bar began at 09:15,
and an hourly one at 08:30 rather than 09:15 — an hourly bar holding fifteen
minutes of a session's first hour. The 1m/5m/15m widths were right under both
grids by accident (the IST offset 19,800 and the open's 33,300 are whole
multiples of 60, 300 and 900, and not of 1800 or 3600), which is what made the
split invisible until someone ran a 30 minute chart next to a 30 minute
backtest.

Three sites bucket M1 into wider bars, and each is driven here through its own
public seam rather than through the shared helper — a table that called the
helper three times would pass no matter what the callers did:

  * live      ``BarAggregator.on_quote``             -> ``BarFrame.time``
  * resample  ``HistoricalSeries.resample``          -> ``Candle.timestamp``
  * bulk      ``BulkPrefetchMarketProvider.history`` -> ``Candle.timestamp``
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal

import pandas as pd
import pytest
from tradex_domain import OHLC, Candle, Equity, HistoricalSeries, Price, Quantity, Timeframe
from tradex_domain import timeframe as timeframe_registry
from tradex_domain.market_calendar import IST, MARKET_CLOSE, MARKET_OPEN
from tradex_market_data.market_provider import BulkPrefetchMarketProvider
from tradex_market_data.parquet_storage import ParquetStorage
from tradex_runtime.bar_aggregator import BarAggregator

#: A plain weekday session: 09:15 to 15:29, the 375 bars the datalake holds.
_DAY = datetime(2026, 7, 15)
OPENING = _DAY + timedelta(hours=MARKET_OPEN.hour, minutes=MARKET_OPEN.minute)
CLOSE = _DAY + timedelta(hours=MARKET_CLOSE.hour, minutes=MARKET_CLOSE.minute)
SESSION = [OPENING + timedelta(minutes=m) for m in range(375)]

#: Every intraday width the canonical registry serves, in registry order, with
#: the number of bars one session of that width produces (the last is a stub).
INTRADAY_WIDTHS = {60: 375, 300: 75, 900: 25, 1800: 13, 3600: 7}

_EQ = Equity.of("NSE", "RELIANCE")
_SYMBOL = "RELIANCE"


def _tf(seconds: int) -> Timeframe:
    for tf in Timeframe:
        if timeframe_registry.bucket_seconds(tf) == seconds:
            return tf
    raise AssertionError(f"no registered timeframe is {seconds}s wide")


# ---------------------------------------------------------------- the sites
#
# Each helper returns the session's bar grid — the distinct bucket starts, in
# order — as that site reports it.


def _live_grid(seconds: int) -> list[datetime]:
    frames: list = []
    agg = BarAggregator(f"NSE:{_SYMBOL}", _tf(seconds), on_frame=frames.append)
    for ts in SESSION:
        agg.on_quote(ts, Decimal("100"), Decimal("1"))
    agg.flush()
    starts = {datetime.fromtimestamp(f.time, tz=IST).replace(tzinfo=None) for f in frames}
    return sorted(starts)


def _candle(ts: datetime) -> Candle:
    return Candle(
        instrument=_EQ,
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=Decimal("100")),
            high=Price(value=Decimal("101")),
            low=Price(value=Decimal("99")),
            close=Price(value=Decimal("100")),
        ),
        volume=Quantity(value=Decimal("1")),
        timestamp=ts,
    )


def _series() -> HistoricalSeries:
    candles = [_candle(ts) for ts in SESSION]
    return HistoricalSeries(
        instrument=_EQ,
        timeframe=Timeframe.M1,
        candles=candles,
        start=candles[0].timestamp,
        end=candles[-1].timestamp,
    )


def _resample_grid(seconds: int) -> list[datetime]:
    return [c.timestamp for c in _series().resample(_tf(seconds)).candles]


def _bulk_grid(tmp_path, seconds: int) -> list[datetime]:
    # Naive IST stamps, like the other two sites: this one used to render a
    # resampled bucket as a tz-aware UTC stamp whose wall clock was the bar's
    # start, so a consumer reading the field as wall clock agreed and one
    # comparing it to a `Candle` from elsewhere did not. The grid comparisons
    # below are exact, type included, which is the intent.
    store = ParquetStorage(tmp_path)
    store.upsert(
        pd.DataFrame(
            [
                dict(
                    symbol=_SYMBOL,
                    exchange="NSE",
                    kind="equity",
                    timeframe="1m",
                    timestamp=ts,
                    open=100,
                    high=101,
                    low=99,
                    close=100,
                    volume=10,
                )
                for ts in SESSION
            ]
        )
    )
    # A window wide enough to hold every bucket start: this provider filters
    # resampled bars on their own stamp, and a daily bar opens at 00:00 and a
    # weekly one on the previous Monday.
    lo, hi = _DAY - timedelta(days=7), _DAY + timedelta(days=1)
    provider = BulkPrefetchMarketProvider([_SYMBOL], lo, hi, store=store)
    series = provider.history(_EQ, _tf(seconds), lo, hi)
    return [c.timestamp for c in series.candles]


#: The grid the session open defines: the open, then whole bars from it, with
#: the final one cut at the close.
def _anchored_grid(seconds: int) -> list[datetime]:
    starts = [OPENING + timedelta(seconds=k * seconds) for k in range(INTRADAY_WIDTHS[seconds])]
    assert starts[-1] <= CLOSE
    return starts


def _epoch_floor(ts: datetime, seconds: int) -> datetime:
    """The grid this lane replaced: a fixed-length epoch floor, read as IST.

    Kept here as the control rather than trusted to a comment. 19,800 (the IST
    offset) and 33,300 (the open's distance from IST midnight) are both whole
    multiples of 60, 300 and 900 and neither is of 1800 or 3600, so the two
    grids agree at 1m/5m/15m and part company at 30m and hourly.
    """
    epoch = int(ts.replace(tzinfo=IST).timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=IST).replace(tzinfo=None)


def _epoch_floor_grid(seconds: int) -> list[datetime]:
    return sorted({_epoch_floor(t, seconds) for t in SESSION})


# ------------------------------------------------------------- control one


def test_the_three_sites_anchor_every_intraday_width_at_the_session_open(tmp_path):
    """One row per width, all three sites, one table of answers.

    The bar that holds the 09:15 print is the bar that begins at 09:15, at
    every width the registry serves. Under the epoch floor that is false for
    30m (09:00) and hourly (08:30), so a site that regresses fails its own
    answer rather than quietly agreeing with two correct neighbours.
    """
    grids = {
        "live": {w: _live_grid(w) for w in INTRADAY_WIDTHS},
        "resample": {w: _resample_grid(w) for w in INTRADAY_WIDTHS},
        "bulk": {w: _bulk_grid(tmp_path, w) for w in INTRADAY_WIDTHS},
    }
    for width, count in INTRADAY_WIDTHS.items():
        assert grids["live"][width] == _anchored_grid(width), f"live {width}s"
        assert grids["resample"][width] == _anchored_grid(width), f"resample {width}s"
        assert grids["bulk"][width] == _anchored_grid(width), f"bulk {width}s"
        assert len(grids["live"][width]) == count

    # And the grid this replaced, so the table above is a control and not a
    # restatement: the two part company at exactly the widths the open breaks.
    assert {w: _epoch_floor_grid(w) for w in (1800, 3600)} != {
        w: _anchored_grid(w) for w in (1800, 3600)
    }
    assert _epoch_floor(OPENING, 1800) == OPENING - timedelta(minutes=15)
    assert _epoch_floor(OPENING, 3600) == OPENING - timedelta(minutes=45)


# ------------------------------------------------------------- control two


@pytest.mark.parametrize("width", [60, 300, 900])
def test_one_five_and_fifteen_minutes_are_unmoved(tmp_path, width):
    """The widths the epoch floor already got right must not have moved.

    Asserted bar for bar against the old grid itself, over a whole session: the
    fix re-anchors 30m and hourly, and a rewrite that disturbed the 1m grid
    shows up here as a difference rather than as a subtly different chart.
    """
    assert _anchored_grid(width) == _epoch_floor_grid(width)
    assert _live_grid(width) == _epoch_floor_grid(width)
    assert _resample_grid(width) == _epoch_floor_grid(width)
    assert _bulk_grid(tmp_path, width) == _epoch_floor_grid(width)


# ------------------------------------------------------------ daily/weekly


def test_daily_bars_open_at_ist_midnight_on_every_site(tmp_path):
    """The daily bucket is the IST day, on all three sites.

    A fixed-length floor drew the day boundary at 00:00 UTC, which is 05:30 IST
    — inside the trading day — so a daily bar built from a session was stamped
    05:30 while the live aggregator stamped it 00:00, and a consumer matching a
    live daily bar to its history bar by timestamp found nothing.
    """
    assert _live_grid(86_400) == [_DAY]
    assert _resample_grid(86_400) == [_DAY]
    assert _bulk_grid(tmp_path, 86_400) == [_DAY]
    assert _epoch_floor(OPENING, 86_400) == _DAY + timedelta(hours=5, minutes=30)
    # What did not change: a session was one daily bar under the old boundary
    # too, because 05:30 and midnight both fall before the open. Only the label
    # moved, and it moved onto the one the live aggregator already used.
    assert len(_epoch_floor_grid(86_400)) == 1


def test_weekly_bars_open_on_the_ist_monday_on_every_site(tmp_path):
    """The weekly bucket is the ISO week, Monday 00:00 IST, on all three sites.

    604800 is divisible by seven days but the epoch origin is a Thursday, so a
    fixed-length floor lands mid-week and every weekly bar is off by a weekday
    — the same class of defect as the intraday skew, one level up.
    """
    monday = _DAY - timedelta(days=_DAY.weekday())
    assert monday == datetime(2026, 7, 13)
    assert _live_grid(604_800) == [monday]
    assert _resample_grid(604_800) == [monday]
    assert _bulk_grid(tmp_path, 604_800) == [monday]
    # The floor this replaced landed on a Thursday, five and a half hours in.
    stale = _epoch_floor(OPENING, 604_800)
    assert stale != monday
    assert stale.weekday() == 3 and stale.time() == dtime(5, 30)
    # Which is not only a mislabel: the old window ran Thursday to Wednesday,
    # so the bar for this Wednesday's session had already swallowed the previous
    # Thursday and Friday, and the ISO week's Sunday would open the next one.
    assert stale == monday - timedelta(days=3, hours=18, minutes=30)
    assert monday <= OPENING < monday + timedelta(days=7)


# ----------------------------------------------------------- control three


def test_an_aware_stamp_buckets_as_the_ist_wall_clock_it_names():
    """Timezone-aware input is converted, not read as a wall clock.

    The datalake stores tz-naive IST, but a broker-sourced series can carry an
    aware stamp. Reading ``12:00Z``'s wall clock as if it were IST would file it
    two and a half hours early — the epoch floor this lane replaced did exactly
    that, by treating an aware stamp's wall clock as IST.
    """
    aware = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    naive = aware.astimezone(IST).replace(tzinfo=None)
    assert naive == datetime(2026, 7, 15, 17, 30)
    for width in (1800, 3600):
        assert timeframe_registry.bucket_start(aware, width) == (
            timeframe_registry.bucket_start(naive, width)
        )

    def _one(ts: datetime, width: int) -> datetime:
        series = HistoricalSeries(
            instrument=_EQ, timeframe=Timeframe.M1, candles=[_candle(ts)], start=ts, end=ts
        )
        return series.resample(_tf(width)).candles[0].timestamp

    assert _one(aware, 3600) == _one(naive, 3600) == datetime(2026, 7, 15, 17, 15)


def test_the_runtime_uses_the_domain_definition_rather_than_its_own_copy():
    """One implementation, provable by identity and not by a docstring.

    ``bar_aggregator`` re-exports the domain helper, so the aggregator and the
    two history sites cannot hold two grids. A second copy in the runtime fails
    this on object identity even if it agreed at every width asserted above.
    """
    from tradex_runtime import bar_aggregator

    assert bar_aggregator.bucket_start is timeframe_registry.bucket_start
    assert bar_aggregator.bucket_start.__module__ == "tradex_domain.timeframe"


def test_reverting_the_domain_rule_to_an_epoch_floor_moves_all_three_sites(
    tmp_path, monkeypatch
):
    """The fix is load bearing, and all three sites hang off the same rule.

    The old grid is reinstated by replacing the one primitive every site
    resolves (``bucket_wall_seconds``) rather than by editing three files. If
    any site kept a private copy of the grid it would still answer 09:15 here,
    and the reintroduction would be a second, invisible defect.
    """
    ist_offset = int(IST.utcoffset(None).total_seconds())

    def _epoch_floor_wall(wall: int, seconds: int) -> int:
        epoch = wall + ist_offset
        return epoch - (epoch % seconds) - ist_offset

    monkeypatch.setattr(
        timeframe_registry, "bucket_wall_seconds", _epoch_floor_wall, raising=True
    )
    opening_wall = int((OPENING - datetime(1970, 1, 1)).total_seconds())
    assert _epoch_floor_wall(opening_wall, 3600) == opening_wall - 45 * 60
    assert _live_grid(3600) == _epoch_floor_grid(3600)
    assert _resample_grid(3600) == _epoch_floor_grid(3600)
    assert _bulk_grid(tmp_path, 3600) == _epoch_floor_grid(3600)
