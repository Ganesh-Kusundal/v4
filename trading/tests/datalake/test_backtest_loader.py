"""Tests for ParquetBacktestLoader — datalake-backed backtest data loader."""

from __future__ import annotations

import warnings
from datetime import datetime
from decimal import Decimal

import pandas as pd
import pytest
from tradex_domain import OrderSide, Signal
from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import Candle
from tradex_domain.strategy import StrategyContext

from tradex_trading.datalake.backtest_loader import ParquetBacktestLoader
from tradex_trading.datalake.parquet_storage import ParquetStorage
from tradex_trading.replay.backtest import BacktestEngine


def _upsert_bars(store: ParquetStorage, symbol: str, days: tuple[int, ...]) -> None:
    """Write two 1m bars per day for *symbol* in July 2026."""
    rows = []
    for day in days:
        for minute in (15, 16):  # 09:15 and 09:16
            ts = f"2026-07-{day:02d} 09:{minute:02d}:00"
            rows.append(
                dict(symbol=symbol, exchange="NSE", kind="equity", timeframe="1m",
                     timestamp=ts, open=100, high=102, low=99, close=101, volume=1000)
            )
    store.upsert(pd.DataFrame(rows))


def _upsert_two_symbols(store: ParquetStorage) -> None:
    _upsert_bars(store, "RELIANCE", (1, 2))
    _upsert_bars(store, "TCS", (1, 2))


def _upsert_series(store: ParquetStorage, symbol: str, closes: list[float]) -> None:
    """Write one 1m bar per *business* day with the given close values.

    Uses business days from 2026-07-01 so the write-path weekday guard does
    not drop bars (day numbers 4/5 of July 2026 fall on Sat/Sun).
    """
    dates = list(pd.bdate_range("2026-07-01", periods=len(closes)))
    rows = [
        dict(symbol=symbol, exchange="NSE", kind="equity", timeframe="1m",
             timestamp=f"{d.date()} 09:15:00", open=close, high=close,
             low=close, close=close, volume=1000)
        for d, close in zip(dates, closes)
    ]
    store.upsert(pd.DataFrame(rows))


_WINDOW = (datetime(2026, 7, 1), datetime(2026, 7, 31))


class _SignalEveryBar:
    """Tiny strategy: emits one BUY signal per bar — for e2e engine wiring."""

    def __init__(self, instrument) -> None:
        self._instrument = instrument
        self._signals: list = []

    @property
    def strategy_id(self) -> str:
        return "signal-every-bar"

    @property
    def signals(self) -> list:
        return list(self._signals)

    def on_bar(self, context: StrategyContext, candle) -> Signal | None:
        signal = Signal(
            instrument=candle.instrument,
            direction=OrderSide.BUY,
            strength=1.0,
            reason="test",
        )
        self._signals.append(signal)
        return signal

    def on_quote(self, context: StrategyContext, quote) -> Signal | None:
        return None

    def on_depth(self, context: StrategyContext, depth) -> Signal | None:
        return None

    def on_fill(self, context: StrategyContext, fill) -> None:
        pass


class _BuyThenSell:
    """BUY on the first bar, SELL on the second — a deterministic round trip."""

    def __init__(self, instrument) -> None:
        self._instrument = instrument
        self._signals: list = []
        self._bar = 0

    @property
    def strategy_id(self) -> str:
        return "buy_then_sell"

    @property
    def signals(self) -> list:
        return list(self._signals)

    def on_bar(self, context: StrategyContext, candle) -> Signal | None:
        self._bar += 1
        if self._bar == 1:
            signal = Signal(
                instrument=candle.instrument, direction=OrderSide.BUY,
                strength=1.0, reason="buy",
            )
        elif self._bar == 2:
            signal = Signal(
                instrument=candle.instrument, direction=OrderSide.SELL,
                strength=1.0, reason="sell",
            )
        else:
            return None
        self._signals.append(signal)
        return signal

    def on_quote(self, context: StrategyContext, quote) -> Signal | None:
        return None

    def on_depth(self, context: StrategyContext, depth) -> Signal | None:
        return None

    def on_fill(self, context: StrategyContext, fill) -> None:
        pass


class TestParquetBacktestLoader:
    def test_load_multi_symbol_m1_time_ordered(self, tmp_path) -> None:
        store = ParquetStorage(tmp_path)
        _upsert_two_symbols(store)
        loader = ParquetBacktestLoader(store=store)

        candles = loader.load(start=_WINDOW[0], end=_WINDOW[1])

        assert len(candles) == 8  # 2 symbols × 2 days × 2 bars
        assert all(isinstance(c, Candle) for c in candles)
        assert all(c.timeframe == Timeframe.M1 for c in candles)
        # time-ordered interleaving across symbols
        stamps = [c.timestamp for c in candles]
        assert stamps == sorted(stamps)
        symbols = {str(c.instrument.instrument_id) for c in candles}
        assert symbols == {"NSE:RELIANCE", "NSE:TCS"}

    def test_load_explicit_instruments(self, tmp_path) -> None:
        store = ParquetStorage(tmp_path)
        _upsert_two_symbols(store)
        loader = ParquetBacktestLoader(store=store)

        candles = loader.load(
            instruments=[Equity.of("NSE", "TCS")], start=_WINDOW[0], end=_WINDOW[1]
        )
        assert len(candles) == 4
        assert {str(c.instrument.instrument_id) for c in candles} == {"NSE:TCS"}

    def test_load_universe_delegates_to_load_universe(self, tmp_path, monkeypatch) -> None:
        store = ParquetStorage(tmp_path)
        _upsert_two_symbols(store)
        monkeypatch.setattr(
            "tradex_trading.datalake.backtest_loader.load_universe",
            lambda name: [Equity.of("NSE", "RELIANCE")],
        )
        loader = ParquetBacktestLoader(store=store)

        candles = loader.load(universe="nifty50", start=_WINDOW[0], end=_WINDOW[1])
        assert len(candles) == 4
        assert {str(c.instrument.instrument_id) for c in candles} == {"NSE:RELIANCE"}

    def test_load_instruments_and_universe_mutually_exclusive(self, tmp_path) -> None:
        store = ParquetStorage(tmp_path)
        loader = ParquetBacktestLoader(store=store)
        with pytest.raises(ValueError):
            loader.load(
                instruments=[Equity.of("NSE", "TCS")],
                universe="nifty50",
                start=_WINDOW[0],
                end=_WINDOW[1],
            )

    def test_load_resamples_to_d1(self, tmp_path) -> None:
        store = ParquetStorage(tmp_path)
        _upsert_two_symbols(store)
        loader = ParquetBacktestLoader(store=store)

        candles = loader.load(
            instruments=[Equity.of("NSE", "RELIANCE")],
            timeframe=Timeframe.D1,
            start=_WINDOW[0],
            end=_WINDOW[1],
        )
        assert len(candles) == 2  # one per day
        assert all(c.timeframe == Timeframe.D1 for c in candles)

    def test_load_default_range_derived_from_store(self, tmp_path) -> None:
        store = ParquetStorage(tmp_path)
        _upsert_two_symbols(store)
        loader = ParquetBacktestLoader(store=store)

        candles = loader.load()
        assert len(candles) == 8  # no window → per-symbol date ranges

    def test_load_skips_symbols_without_data(self, tmp_path) -> None:
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2))
        loader = ParquetBacktestLoader(store=store)

        candles = loader.load(
            instruments=[Equity.of("NSE", "RELIANCE"), Equity.of("NSE", "NONEXISTENT")],
            start=_WINDOW[0],
            end=_WINDOW[1],
        )
        assert len(candles) == 4
        assert {str(c.instrument.instrument_id) for c in candles} == {"NSE:RELIANCE"}

    def test_load_warns_on_skipped_symbols(self, tmp_path) -> None:
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2))
        loader = ParquetBacktestLoader(store=store)

        with pytest.warns(UserWarning, match="NONEXISTENT"):
            loader.load(
                instruments=[
                    Equity.of("NSE", "RELIANCE"),
                    Equity.of("NSE", "NONEXISTENT"),
                ],
                start=_WINDOW[0],
                end=_WINDOW[1],
            )

    def test_load_empty_store(self, tmp_path) -> None:
        store = ParquetStorage(tmp_path)
        loader = ParquetBacktestLoader(store=store)
        assert loader.load(start=_WINDOW[0], end=_WINDOW[1]) == []

    def test_load_parallel_matches_sequential(self, tmp_path) -> None:
        """max_workers > 1 produces the identical candle list."""
        store = ParquetStorage(tmp_path)
        _upsert_two_symbols(store)
        loader = ParquetBacktestLoader(store=store)

        seq = loader.load(start=_WINDOW[0], end=_WINDOW[1])
        par = loader.load(start=_WINDOW[0], end=_WINDOW[1], max_workers=4)

        def _key(c: Candle) -> tuple:
            return (c.timestamp, str(c.instrument.instrument_id), float(c.ohlc.close.value))

        assert len(seq) == len(par) == 8
        assert [_key(c) for c in seq] == [_key(c) for c in par]

    def test_run_convenience_backtests_strategy(self, tmp_path) -> None:
        """loader.run() loads candles and executes BacktestEngine in one call."""
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2))
        loader = ParquetBacktestLoader(store=store)

        strategy = _SignalEveryBar(Equity.of("NSE", "RELIANCE"))
        result = loader.run(
            strategy,
            instruments=[Equity.of("NSE", "RELIANCE")],
            timeframe=Timeframe.D1,
            start=_WINDOW[0],
            end=_WINDOW[1],
        )
        assert result.num_trades == 2

    def test_run_accepts_configured_engine(self, tmp_path) -> None:
        """A pre-configured engine (fees) flows through loader.run()."""
        from tradex_trading.execution.fees import FeeCalculator

        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2))
        loader = ParquetBacktestLoader(store=store)

        strategy = _SignalEveryBar(Equity.of("NSE", "RELIANCE"))
        result = loader.run(
            strategy,
            engine=BacktestEngine(fee_calculator=FeeCalculator()),
            instruments=[Equity.of("NSE", "RELIANCE")],
            timeframe=Timeframe.D1,
            start=_WINDOW[0],
            end=_WINDOW[1],
        )
        assert result.num_trades == 2
        assert result.total_fees > 0

    def test_multi_symbol_strategy_runs_via_loader(self, tmp_path) -> None:
        """A portfolio-style strategy (one instance, every instrument) works
        through ``loader.run()`` over two symbols in a single call."""
        from tradex_trading.strategy.extensions.strategies.multi_symbol_sma_cross import (
            MultiSymbolSmaCross,
        )

        store = ParquetStorage(tmp_path)
        _upsert_series(store, "RELIANCE", [10, 11, 10, 9, 15])
        _upsert_series(store, "TCS", [10, 12, 10, 8, 16])
        loader = ParquetBacktestLoader(store=store)

        result = loader.run(
            MultiSymbolSmaCross(fast=2, slow=3),
            instruments=[Equity.of("NSE", "RELIANCE"), Equity.of("NSE", "TCS")],
            timeframe=Timeframe.D1,
            start=_WINDOW[0],
            end=_WINDOW[1],
        )
        # each symbol crosses down then up → 2 signals each
        assert result.num_trades == 4
        traded = {str(t.instrument.instrument_id) for t in result.trades}
        assert traded == {"NSE:RELIANCE", "NSE:TCS"}

    def test_run_with_percentage_slippage_worsens_round_trip(self, tmp_path) -> None:
        """PercentageSlippageModel through the loader→engine path: BUY fills
        pay more and SELL fills receive less than the candle close."""
        from tradex_trading.execution.slippage import PercentageSlippageModel

        store = ParquetStorage(tmp_path)
        _upsert_series(store, "RELIANCE", [100.0, 200.0])
        loader = ParquetBacktestLoader(store=store)
        inst = Equity.of("NSE", "RELIANCE")

        base = loader.run(
            _BuyThenSell(inst),
            instruments=[inst],
            timeframe=Timeframe.D1,
            start=_WINDOW[0],
            end=_WINDOW[1],
        )
        slipped = loader.run(
            _BuyThenSell(inst),
            engine=BacktestEngine(
                slippage_model=PercentageSlippageModel(Decimal("0.01"))
            ),
            instruments=[inst],
            timeframe=Timeframe.D1,
            start=_WINDOW[0],
            end=_WINDOW[1],
        )
        # next_open fills: BUY (bar-1 signal) fills at bar-2's open (200);
        # SELL (bar-2 signal) settles at the post-loop flush (last candle's
        # open, 200). The last equity point is the bar-2 close mark with the
        # position still open, so the base run stays at 100000.0. With 1%
        # slippage the BUY pays 202 instead of 200 — that loss is fully
        # visible in the close mark: 100000 - 202 + 200 = 99998. (The SELL
        # slippage, 198 vs 200, lands in the post-loop flush, after the last
        # equity point — BUY pays more, SELL receives less.)
        assert base.equity_curve[-1] == 100000.0
        assert slipped.equity_curve[-1] == 99998.0
        assert slipped.equity_curve[-1] < base.equity_curve[-1]

    def test_backtest_engine_consumes_loader_output(self, tmp_path) -> None:
        """End-to-end: loader output feeds BacktestEngine.run directly."""
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2))
        loader = ParquetBacktestLoader(store=store)

        candles = loader.load(
            instruments=[Equity.of("NSE", "RELIANCE")],
            timeframe=Timeframe.D1,
            start=_WINDOW[0],
            end=_WINDOW[1],
        )
        strategy = _SignalEveryBar(Equity.of("NSE", "RELIANCE"))
        result = BacktestEngine().run(strategy, candles)

        assert result.num_trades == len(candles) == 2
        # next_open fills: the bar-1 BUY fills at bar-2's open (100) and the
        # bar-2 BUY settles in the post-loop flush. The equity curve is the
        # initial capital plus one point-in-time mark per candle — the open
        # position is marked at the final close (101), so the last point is
        # 100000 - 100 + 101 = 100001.
        assert len(result.equity_curve) == len(candles) + 1
        assert result.equity_curve == [100000.0, 100000.0, 100001.0]


class TestLoaderGapDetection:
    """Data-gap safety (parity review #3/#10): a symbol missing interior
    trading days must not silently distort a backtest."""

    def test_no_warning_when_days_contiguous(self, tmp_path) -> None:
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2, 3, 6))  # Wed/Thu/Fri/Mon 2026-07
        loader = ParquetBacktestLoader(store=store)

        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning fails the test
            candles = loader.load(
                instruments=[Equity.of("NSE", "RELIANCE")],
                start=_WINDOW[0],
                end=_WINDOW[1],
            )
        assert len(candles) == 8  # 4 days × 2 bars

    def test_warns_on_interior_missing_trading_day(self, tmp_path) -> None:
        """2026-07-02 is a Thursday and missing → flagged as a gap."""
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 3))  # skip day 2 (Thursday)
        loader = ParquetBacktestLoader(store=store)

        with pytest.warns(UserWarning, match="2026-07-02"):
            loader.load(
                instruments=[Equity.of("NSE", "RELIANCE")],
                start=_WINDOW[0],
                end=_WINDOW[1],
            )

    def test_weekend_gap_is_not_flagged(self, tmp_path) -> None:
        """Days 3 (Fri) → 6 (Mon): the weekend in between is not a gap."""
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (3, 6))
        loader = ParquetBacktestLoader(store=store)

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            loader.load(
                instruments=[Equity.of("NSE", "RELIANCE")],
                start=_WINDOW[0],
                end=_WINDOW[1],
            )

    def test_holiday_is_not_flagged(self, tmp_path) -> None:
        """Day 2 is a holiday for this calendar → not a data gap."""
        from datetime import date

        from tradex_trading.runtime.calendar import NSETradingCalendar

        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 3))
        loader = ParquetBacktestLoader(
            store=store,
            calendar=NSETradingCalendar(holidays={date(2026, 7, 2)}),
        )

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            loader.load(
                instruments=[Equity.of("NSE", "RELIANCE")],
                start=_WINDOW[0],
                end=_WINDOW[1],
            )

    def test_gap_strict_raises(self, tmp_path) -> None:
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 3))
        loader = ParquetBacktestLoader(store=store, gap_strict=True)

        with pytest.raises(ValueError, match="data gaps detected"):
            loader.load(
                instruments=[Equity.of("NSE", "RELIANCE")],
                start=_WINDOW[0],
                end=_WINDOW[1],
            )

    def test_gap_check_disabled(self, tmp_path) -> None:
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 3))
        loader = ParquetBacktestLoader(store=store, check_gaps=False)

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            loader.load(
                instruments=[Equity.of("NSE", "RELIANCE")],
                start=_WINDOW[0],
                end=_WINDOW[1],
            )

    def test_symbol_listed_mid_window_is_not_flagged(self, tmp_path) -> None:
        """First bar is the symbol's own start — days before it aren't gaps."""
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (10, 11))
        loader = ParquetBacktestLoader(store=store)

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            loader.load(
                instruments=[Equity.of("NSE", "RELIANCE")],
                start=_WINDOW[0],
                end=_WINDOW[1],
            )

    def test_one_gappy_symbol_among_clean_ones_warns_only_for_it(
        self, tmp_path
    ) -> None:
        """Multi-symbol aggregation: only the gappy symbol is named; the clean
        symbol never produces a spurious warning."""
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 3))  # gap on day 2
        _upsert_bars(store, "TCS", (1, 2, 3))     # contiguous
        loader = ParquetBacktestLoader(store=store)

        with pytest.warns(UserWarning, match="RELIANCE: missing trading day") as rec:
            loader.load(
                instruments=[
                    Equity.of("NSE", "RELIANCE"),
                    Equity.of("NSE", "TCS"),
                ],
                start=_WINDOW[0],
                end=_WINDOW[1],
            )
        msg = str(rec[0].message)
        assert "RELIANCE: missing" in msg
        assert "TCS" not in msg

    def test_strict_gaps_with_holiday_calendar_does_not_fail(self, tmp_path) -> None:
        """--strict-gaps with a holiday-aware calendar: a holiday is not a
        gap, so a strict run over a holiday day succeeds."""
        from datetime import date

        from tradex_trading.runtime.calendar import NSETradingCalendar

        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 3))
        loader = ParquetBacktestLoader(
            store=store,
            gap_strict=True,
            calendar=NSETradingCalendar(holidays={date(2026, 7, 2)}),
        )

        candles = loader.load(
            instruments=[Equity.of("NSE", "RELIANCE")],
            start=_WINDOW[0],
            end=_WINDOW[1],
        )
        assert len(candles) == 4  # 2 days × 2 bars — no exception raised


class TestLoaderSurvivorshipBias:
    """Survivorship-bias protection (review area #3): delisted symbols are
    filtered out when loading via universe or store-symbol paths, preventing
    backtests over past windows from including stocks that are no longer
    listed."""

    def test_delisted_symbol_excluded_from_universe(self, tmp_path, monkeypatch) -> None:
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2))
        monkeypatch.setattr(
            "tradex_trading.datalake.backtest_loader.load_universe",
            lambda name: [
                Equity.of("NSE", "RELIANCE"),
                Equity.of("NSE", "TCS"),
            ],
        )
        loader = ParquetBacktestLoader(store=store, delisted={"TCS"})

        candles = loader.load(universe="nifty50", start=_WINDOW[0], end=_WINDOW[1])
        symbols = {str(c.instrument.instrument_id) for c in candles}
        assert symbols == {"NSE:RELIANCE"}

    def test_delisted_symbol_excluded_from_store_default(self, tmp_path) -> None:
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2))
        _upsert_bars(store, "TCS", (1, 2))
        loader = ParquetBacktestLoader(store=store, delisted={"TCS"})

        candles = loader.load(start=_WINDOW[0], end=_WINDOW[1])
        symbols = {str(c.instrument.instrument_id) for c in candles}
        assert symbols == {"NSE:RELIANCE"}

    def test_explicit_instruments_not_filtered(self, tmp_path) -> None:
        """A delisted symbol can still be loaded explicitly by name."""
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2))
        _upsert_bars(store, "TCS", (1, 2))
        loader = ParquetBacktestLoader(store=store, delisted={"TCS"})

        candles = loader.load(
            instruments=[
                Equity.of("NSE", "RELIANCE"),
                Equity.of("NSE", "TCS"),
            ],
            start=_WINDOW[0],
            end=_WINDOW[1],
        )
        assert len(candles) == 8  # both symbols loaded

    def test_no_delisted_config_unchanged(self, tmp_path) -> None:
        """Backward compatible: no delisted set → all symbols load."""
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2))
        _upsert_bars(store, "TCS", (1, 2))
        loader = ParquetBacktestLoader(store=store)

        candles = loader.load(start=_WINDOW[0], end=_WINDOW[1])
        assert len(candles) == 8  # 2 symbols × 2 days × 2 bars
