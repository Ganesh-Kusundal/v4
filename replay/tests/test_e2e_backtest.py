"""End-to-end backtest: datalake → loader → engine → report.

This is the integration seam behind the API's ``/api/charts/backtest`` route:
historical data flows from a real ``ParquetStorage`` through
``MarketWindowLoader`` into ``BacktestEngine``, which runs a strategy and
produces a ``BacktestResult``. No mocks — a mocked provider cannot demonstrate
that the datalake path works, which is the entire point of the file.

Three contracts pinned here, each of which contradicts a plausible assumption
and each of which a first draft of this file got wrong:

**Timestamps are IST and tz-naive.** ``ParquetStorage`` stores naive wall time,
and ``market_session_mask`` (``parquet_storage.py:132``) drops any bar outside
Mon–Fri 09:15–15:30 *as written*. A UTC-stamped fixture is therefore either
silently discarded — 09:15 UTC is 14:45 IST, inside the session but three hours
off, so it survives as a mislabelled bar — or dropped outright. Every seed here
is naive.

**The loader honours the requested window.** A three-session lake read with a
one-session window returns one session, not three. Reading across a weekend
returns only the weekdays written, because the weekend was never stored.

**An SMA crossover needs a crossover.** A monotone drift does not produce one:
by the first bar where both SMAs are defined the fast SMA is already above (or
below) the slow, and every later bar keeps it there. The ``flat`` and sawtooth
seeds exist to give the strategy something it can actually decide about.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from tradex_domain import Equity, Timeframe
from tradex_market_data.market_provider import ParquetMarketProvider
from tradex_market_data.parquet_storage import ParquetStorage
from tradex_market_data.window_loader import MarketWindowLoader
from tradex_replay.backtest import BacktestEngine, BacktestResult
from tradex_strategy.extensions.strategies.sma_cross import SmaCrossStrategy

#: 09:15 IST — the session open the write chokepoint admits, both bounds inclusive.
SESSION_OPEN = datetime(2026, 7, 1, 9, 15)
#: 15:30 IST — the session close.
SESSION_CLOSE = datetime(2026, 7, 1, 15, 30)
#: Minutes in one NSE session, 09:15 through 15:30 inclusive.
SESSION_MINUTES = 376
#: Wed 2026-07-01, Thu 07-02, Fri 07-03 — three consecutive weekdays. Starting
#: mid-week matters: a naive `+ timedelta(days=n)` seed that starts on a
#: Saturday writes only weekdays and silently lands on fewer sessions.
_FIRST_SESSION = datetime(2026, 7, 1, 9, 15)
#: The closing stamp of the third seeded session.
_LAST_SESSION = datetime(2026, 7, 3, 15, 30)


def _eq(symbol: str = "RELIANCE") -> Equity:
    return Equity.of("NSE", symbol)


def _seed(root: Path, *, shape: str = "sawtooth", days: int = 3) -> ParquetStorage:
    """Write *days* weekday sessions of one-minute bars, and return the store.

    ``sawtooth`` rises for 20 bars then falls for 20, repeating. A crossover
    strategy is handed a genuine crossing on every half-cycle in both
    directions — an uptrend or a downtrend alone gives it nothing to act on.

    ``flat`` is the negative control: bars the strategy must decline to trade.

    Prices are built so ``low <= open/close <= high`` everywhere, because
    ``_prepare_frame`` drops OHLC-inconsistent rows at the write chokepoint and
    a dropped seed reads as a broken loader.
    """
    store = ParquetStorage(str(root))
    rows: list[dict[str, object]] = []
    for day in range(days):
        base = pd.Timestamp(_FIRST_SESSION) + pd.Timedelta(days=day)
        for i in range(SESSION_MINUTES):
            if shape == "flat":
                close = 100.0
            else:
                phase = i % 40
                # A triangle wave: up over the first 20 minutes of each cycle,
                # down over the next 20, so the two SMAs cross twice a cycle.
                close = 100.0 + (phase if phase < 20 else 40 - phase) * 0.4
            rows.append(
                {
                    "symbol": "RELIANCE",
                    "exchange": "NSE",
                    "kind": "equity",
                    "timeframe": "1m",
                    "timestamp": base + pd.Timedelta(minutes=i),
                    "open": round(close - 0.2, 2),
                    "high": round(close + 0.5, 2),
                    "low": round(close - 0.5, 2),
                    "close": round(close, 2),
                    "volume": 1000 + i,
                }
            )
    store.upsert(pd.DataFrame(rows))
    return store


def _loader(store: ParquetStorage) -> MarketWindowLoader:
    return MarketWindowLoader(provider=ParquetMarketProvider(store))


@pytest.fixture
def instrument() -> Equity:
    return _eq("RELIANCE")


@pytest.fixture
def lake(tmp_path: Path):
    """A three-session sawtooth datalake, yielding ``(store, loader)``."""
    store = _seed(tmp_path / "lake")
    yield store, _loader(store)
    store.clear()


class TestDatalakeToBacktest:
    """The full path, over real parquet partitions."""

    def test_seeded_lake_round_trips_through_the_loader(
        self, lake, instrument: Equity
    ) -> None:
        """What was written is what the loader reads back, in session order."""
        _store, loader = lake
        window = loader.load(
            instrument, Timeframe.M1, _FIRST_SESSION, _LAST_SESSION
        )

        assert window.source.value == "datalake"
        # 376 bars x 3 sessions. The exact count is the claim: a phantom bar, a
        # dropped session, or an off-by-one session read would all move it.
        assert len(window.candles) == SESSION_MINUTES * 3
        stamps = [c.timestamp for c in window.candles]
        assert stamps == sorted(stamps), "loader returned bars out of order"
        assert stamps[0] == _FIRST_SESSION
        assert stamps[-1] == _LAST_SESSION
        assert len(set(stamps)) == len(stamps), "a timestamp appeared twice"
        # Three distinct weekdays, no weekend bars (they were never written).
        assert {s.date() for s in stamps} == {
            datetime(2026, 7, 1).date(),
            datetime(2026, 7, 2).date(),
            datetime(2026, 7, 3).date(),
        }

    def test_the_loader_clips_to_the_requested_window(
        self, lake, instrument: Equity
    ) -> None:
        """A one-session request returns one session, not the whole lake.

        The lake holds three sessions. Reading all three when asked for one
        would hand the backtest bars the caller never asked for and make every
        window argument meaningless.
        """
        _store, loader = lake
        window = loader.load(instrument, Timeframe.M1, SESSION_OPEN, SESSION_CLOSE)

        assert len(window.candles) == SESSION_MINUTES
        assert {s.date() for s in [c.timestamp for c in window.candles]} == {
            datetime(2026, 7, 1).date()
        }

    def test_a_crossing_produces_real_fills(self, lake, instrument: Equity) -> None:
        """The seeded crossings are traded, and every fill is a real one."""
        _store, loader = lake
        window = loader.load(
            instrument, Timeframe.M1, _FIRST_SESSION, _LAST_SESSION
        )

        strategy = SmaCrossStrategy(
            strategy_id="e2e-sma", instrument=instrument, fast=3, slow=6
        )
        result = BacktestEngine().run(strategy, list(window.candles))

        assert isinstance(result, BacktestResult)
        assert result.num_trades > 0, "1128 crossing bars produced no signal"
        assert result.fills, "signals were emitted but nothing filled"
        for fill in result.fills:
            assert fill["price"] > 0, "a fill at a non-positive price"
            assert fill["qty"] > 0, "a fill of zero quantity"
            assert fill["rejected"] is False
            assert fill["side"] in {"BUY", "SELL"}
            assert fill["instrument"] == "NSE:RELIANCE"

    def test_both_directions_are_traded(self, lake, instrument: Equity) -> None:
        """A sawtooth trades both sides, so the side comes from the decision.

        If the pipeline hardcoded a side, or if only one of the two signal
        paths were live, this would see exactly one.
        """
        _store, loader = lake
        window = loader.load(
            instrument, Timeframe.M1, _FIRST_SESSION, _LAST_SESSION
        )
        strategy = SmaCrossStrategy(
            strategy_id="e2e-sma-sides", instrument=instrument, fast=3, slow=6
        )
        result = BacktestEngine().run(strategy, list(window.candles))

        assert {f["side"] for f in result.fills} == {"BUY", "SELL"}

    def test_a_fill_prices_at_the_open_of_its_own_bar(self, lake, instrument: Equity) -> None:
        """The fill is at a bar's open — the AC-1 next-open rule, checked.

        The claim is that a signal decided on bar N fills at bar N+1's *open*,
        never at the signal bar's own close. The check reads the open of the
        bar each fill is stamped with and compares, so a run that filled at
        closes, or at midpoints, or at any other price, fails on arithmetic
        rather than passing because "a fill happened".
        """
        _store, loader = lake
        window = loader.load(
            instrument, Timeframe.M1, _FIRST_SESSION, _LAST_SESSION
        )
        candles = list(window.candles)
        open_of = {c.timestamp: float(c.ohlc.open.value) for c in candles}

        strategy = SmaCrossStrategy(
            strategy_id="e2e-sma-open", instrument=instrument, fast=3, slow=6
        )
        result = BacktestEngine().run(strategy, candles)

        checked = 0
        for fill in result.fills:
            stamp = fill["time"]
            assert stamp in open_of, f"fill stamped {stamp}, which is not a bar"
            assert fill["price"] == pytest.approx(open_of[stamp], rel=1e-9), (
                f"fill priced {fill['price']} against the {open_of[stamp]} open "
                f"of the bar it claims to have filled on"
            )
            checked += 1
        assert checked, "no fill carried a resolvable timestamp"

    def test_the_equity_curve_reflects_the_run(self, lake, instrument: Equity) -> None:
        """The curve is a series of real values, not a repeated constant."""
        _store, loader = lake
        window = loader.load(
            instrument, Timeframe.M1, _FIRST_SESSION, _LAST_SESSION
        )
        strategy = SmaCrossStrategy(
            strategy_id="e2e-sma-equity", instrument=instrument, fast=3, slow=6
        )
        result = BacktestEngine().run(strategy, list(window.candles))

        assert len(result.equity_curve) > SESSION_MINUTES
        assert len(set(result.equity_curve)) > 1, (
            "1128 bars of a moving market produced a constant equity curve"
        )
        assert result.equity_curve[0] == pytest.approx(100_000.0, rel=1e-6)


class TestBacktestDegenerateInputs:
    """The empty and no-trade paths, which are the ones nobody exercises."""

    def test_an_empty_window_runs_and_reports_nothing(
        self, tmp_path: Path, instrument: Equity
    ) -> None:
        """An empty lake is a zeroed report, not an error and not a made-up bar."""
        store = ParquetStorage(str(tmp_path / "empty"))
        window = _loader(store).load(
            instrument, Timeframe.M1, _FIRST_SESSION, _LAST_SESSION
        )
        assert window.candles == ()

        strategy = SmaCrossStrategy(
            strategy_id="e2e-empty", instrument=instrument, fast=3, slow=6
        )
        result = BacktestEngine().run(strategy, list(window.candles))

        assert result.num_trades == 0
        assert result.fills == []
        assert result.total_return == 0.0
        assert result.sharpe == 0.0
        store.clear()

    def test_a_window_outside_the_seeded_range_is_empty(
        self, lake, instrument: Equity
    ) -> None:
        """Reading a range the lake does not cover yields no bars, not an error."""
        _store, loader = lake
        window = loader.load(
            instrument,
            Timeframe.M1,
            datetime(2026, 9, 1, 9, 15),
            datetime(2026, 9, 1, 15, 30),
        )
        assert window.candles == ()

    def test_a_flat_series_produces_no_trades(
        self, tmp_path: Path, instrument: Equity
    ) -> None:
        """Bars with no crossing produce no trade — the negative control.

        Without this, "the sawtooth traded" would also be satisfied by a run
        that places an order on every single bar regardless of what the
        strategy decided.
        """
        store = _seed(tmp_path / "flat", shape="flat", days=2)
        window = _loader(store).load(
            instrument, Timeframe.M1, _FIRST_SESSION, _LAST_SESSION
        )
        assert len(window.candles) == SESSION_MINUTES * 2, "the flat seed wrote no bars"

        strategy = SmaCrossStrategy(
            strategy_id="e2e-flat", instrument=instrument, fast=3, slow=6
        )
        result = BacktestEngine().run(strategy, list(window.candles))

        assert result.num_trades == 0, "a flat series produced a crossing"
        assert result.fills == []
        assert result.total_return == 0.0
        store.clear()
