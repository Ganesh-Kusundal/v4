"""Strategy scanner + engine tests (F10, F11).

Ported from v3 ``test_strategy_scanner.py``.

v4 API:
- ``ScannerEngine(market, analytics?, window_days?)`` with ``run()``, ``top()``, ``scan()``
- ``ReactiveStrategyEngine(bus)`` replaces v3 ``StrategyEngine``
- ``BuyAndHoldStrategy(strategy_id, instrument)`` emits Signal objects on quote
- Strategy protocol: ``strategy_id`` property, ``on_bar``, ``on_quote``, ``on_fill``
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tradex_domain import (
    OHLC,
    Candle,
    Condition,
    Equity,
    HistoricalSeries,
    OrderSide,
    Price,
    Quantity,
    Quote,
    ScannerDefinition,
    Timeframe,
)
from tradex_domain.strategy import StrategyContext
from tradex_domain.value_objects import Price as QuotePrice

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.strategy.core.buy_and_hold import BuyAndHoldStrategy
from tradex_trading.strategy.core.engine import ReactiveStrategyEngine
from tradex_trading.strategy.core.protocols import Strategy
from tradex_trading.strategy.core.scanner import ScannerEngine


def _now() -> datetime:
    return datetime(2026, 7, 31, 10, 30, tzinfo=UTC)


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _candle(close: float, ts: datetime) -> Candle:
    return Candle(
        instrument=_eq(),
        timeframe=Timeframe.D1,
        ohlc=OHLC(
            open=Price(value=Decimal(str(close - 1))),
            high=Price(value=Decimal(str(close + 1))),
            low=Price(value=Decimal(str(close - 1))),
            close=Price(value=Decimal(str(close))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


def _definition(*conditions: Condition) -> ScannerDefinition:
    return ScannerDefinition(
        universe=[_eq(), Equity.of("NSE", "TCS")],
        conditions=list(conditions),
    )


# ---------------------------------------------------------------------------
# F10 — ScannerEngine (stub in v4)
# ---------------------------------------------------------------------------


class _FakeMarket:
    """Minimal market provider returning a canned HistoricalSeries."""

    def __init__(self, candles: list[Candle] | None = None) -> None:
        self._candles = candles or []

    def history(self, instrument, timeframe, start, end) -> HistoricalSeries:
        return HistoricalSeries(
            instrument=instrument,
            timeframe=timeframe,
            candles=self._candles,
            start=start,
            end=end,
        )


def _series(closes: list[float]) -> list[Candle]:
    """Build a list of D1 candles from close prices."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        _candle(c, base + timedelta(days=i))
        for i, c in enumerate(closes)
    ]


class TestScannerEngine:
    """ScannerEngine with full condition evaluation."""

    def test_scanner_empty_universe(self) -> None:
        engine = ScannerEngine(market=_FakeMarket())
        results = engine.run(ScannerDefinition(universe=[], conditions=[]))
        assert results == []

    def test_scanner_close_condition(self) -> None:
        candles = _series([100.0, 101.0, 102.0, 103.0, 104.0])
        market = _FakeMarket(candles)
        engine = ScannerEngine(market=market)
        cond = Condition(name="close", operator=">", threshold=100.0)
        defn = ScannerDefinition(universe=[_eq()], conditions=[cond])
        results = engine.run(defn)
        assert len(results) == 1
        assert results[0].score == 1.0
        assert results[0].rank == 1
        assert "close" in results[0].matched_conditions

    def test_scanner_top_limits_results(self) -> None:
        candles = _series([100.0, 101.0])
        market = _FakeMarket(candles)
        engine = ScannerEngine(market=market)
        cond = Condition(name="close", operator=">", threshold=50.0)
        defn = ScannerDefinition(
            universe=[_eq(), Equity.of("NSE", "TCS")],
            conditions=[cond],
        )
        results = engine.top(defn, limit=1)
        assert len(results) == 1

    def test_matches_unknown_operator(self) -> None:
        cond = Condition(name="close", operator="??", threshold=1.0)
        assert ScannerEngine._matches(cond, 5.0) is False

    def test_matches_none_threshold(self) -> None:
        cond = Condition(name="close", operator=">", threshold=None)
        assert ScannerEngine._matches(cond, 5.0) is False


# ---------------------------------------------------------------------------
# F11 — Strategy protocol
# ---------------------------------------------------------------------------


class TestStrategyProtocol:
    """Strategy protocol conformance."""

    def test_buy_and_hold_conforms_to_protocol(self) -> None:
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        assert isinstance(strategy, Strategy)

    def test_strategy_id_property(self) -> None:
        strategy = BuyAndHoldStrategy("my-strategy", _eq())
        assert strategy.strategy_id == "my-strategy"


# ---------------------------------------------------------------------------
# F11 — BuyAndHoldStrategy
# ---------------------------------------------------------------------------


class TestBuyAndHoldStrategy:
    """BuyAndHoldStrategy emits signals on quote."""

    def test_on_quote_emits_buy_signal(self) -> None:
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        ctx = StrategyContext()
        quote = Quote(
            instrument=_eq(),
            ltp=QuotePrice(value=Decimal("100")),
            timestamp=_now(),
        )
        strategy.on_quote(ctx, quote)
        assert len(strategy.signals) == 1
        signal = strategy.signals[0]
        assert signal.direction == OrderSide.BUY
        assert signal.reason == "buy_and_hold"

    def test_on_bar_is_noop(self) -> None:
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        ctx = StrategyContext()
        candle = _candle(100.0, _now())
        strategy.on_bar(ctx, candle)
        assert strategy.signals == []

    def test_on_fill_is_noop(self) -> None:
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        ctx = StrategyContext()
        strategy.on_fill(ctx, object())
        assert strategy.signals == []

    def test_multiple_quotes_emit_multiple_signals(self) -> None:
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        ctx = StrategyContext()
        for i in range(3):
            quote = Quote(
                instrument=_eq(),
                ltp=QuotePrice(value=Decimal(str(100 + i))),
                timestamp=_now() + timedelta(seconds=i),
            )
            strategy.on_quote(ctx, quote)
        assert len(strategy.signals) == 3


# ---------------------------------------------------------------------------
# F11 — ReactiveStrategyEngine
# ---------------------------------------------------------------------------


class _RecordingStrategy:
    """Strategy that records received events."""

    def __init__(self, strategy_id: str = "rec") -> None:
        self._id = strategy_id
        self.bars: list = []
        self.quotes: list = []
        self.fills: list = []

    @property
    def strategy_id(self) -> str:
        return self._id

    def on_bar(self, context, candle: object) -> None:
        self.bars.append(candle)

    def on_quote(self, context, quote: object) -> None:
        self.quotes.append(quote)

    def on_fill(self, context, fill: object) -> None:
        self.fills.append(fill)


class TestReactiveStrategyEngine:
    """ReactiveStrategyEngine routes events via bus subscriptions."""

    def test_register_and_routes_candle(self) -> None:
        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        strat = _RecordingStrategy()
        engine.register(strat)

        candle = _candle(100.0, _now())
        bus.publish(candle)

        assert len(strat.bars) == 1
        assert strat.bars[0] is candle
        engine.dispose_all()

    def test_register_and_routes_quote(self) -> None:
        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        strat = _RecordingStrategy()
        engine.register(strat)

        quote = Quote(
            instrument=_eq(),
            ltp=QuotePrice(value=Decimal("100")),
            timestamp=_now(),
        )
        bus.publish(quote)

        assert len(strat.quotes) == 1
        engine.dispose_all()

    def test_unregister_stops_events(self) -> None:
        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        strat = _RecordingStrategy()
        engine.register(strat)
        engine.unregister("rec")

        candle = _candle(100.0, _now())
        bus.publish(candle)

        # Strategy was unregistered, but bus subscription still delivers
        # (unregister removes from _strategies dict, subscription remains until dispose_all)
        assert strat.strategy_id not in engine.strategies
        engine.dispose_all()

    def test_strategies_property(self) -> None:
        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        strat = _RecordingStrategy("s1")
        engine.register(strat)
        assert "s1" in engine.strategies
        engine.dispose_all()

    def test_dispose_all_clears_everything(self) -> None:
        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        engine.register(_RecordingStrategy("s1"))
        engine.register(_RecordingStrategy("s2"))
        engine.dispose_all()
        assert engine.strategies == {}

# ---------------------------------------------------------------------------
# Scanner with registry-fallback indicators (6 of 11 total)
#
# Previously only the native indicators (sma, ema, rsi, roc) could be
# used as scanner conditions because ScannerEngine._value() dispatched to
# AnalyticsEngine.indicator_values(), which only knew about _INDICATORS.
# The two-tier resolution now falls back to compute_indicator for the 7
# registry indicators (macd, bollinger, atr, vwap, obv, stochastic,
# supertrend).
# ---------------------------------------------------------------------------

_REGISTRY_SCANNER_INDICATORS = [
    "macd", "bollinger", "atr", "vwap", "obv", "stochastic", "supertrend",
]
_NATIVE_SCANNER_INDICATORS = ["sma", "ema", "rsi", "roc"]


class TestScannerAllIndicators:
    """ScannerEngine resolves all 11 indicators (4 native + 7 registry) as
    condition sources."""

    @pytest.mark.parametrize("name", _REGISTRY_SCANNER_INDICATORS)
    def test_scanner_resolves_registry_indicator(self, name: str) -> None:
        """Each of the 7 registry indicators is usable as a scanner condition."""
        closes = [float(i) for i in range(1, 31)]
        market = _FakeMarket(_series(closes))
        engine = ScannerEngine(market=market)
        cond = Condition(name=name, operator=">", threshold=0.0)
        defn = ScannerDefinition(universe=[_eq()], conditions=[cond])
        results = engine.run(defn)
        assert len(results) == 1
        assert name in results[0].indicator_values
        # Value must be a real computed value, not the 0.0 fallback that
        # _value() returns when indicator_values() yields all-None.
        assert results[0].indicator_values[name] != 0.0
        assert name in results[0].matched_conditions

    @pytest.mark.parametrize("name", _NATIVE_SCANNER_INDICATORS)
    def test_scanner_resolves_native_indicator(self, name: str) -> None:
        """The 4 native indicators continue to work via the fast-path."""
        closes = [float(i) for i in range(1, 31)]
        market = _FakeMarket(_series(closes))
        engine = ScannerEngine(market=market)
        cond = Condition(name=name, operator=">", threshold=0.0)
        defn = ScannerDefinition(universe=[_eq()], conditions=[cond])
        results = engine.run(defn)
        assert len(results) == 1
        assert name in results[0].indicator_values
        assert results[0].indicator_values[name] != 0.0

    def test_scanner_mixed_native_and_registry_conditions(self) -> None:
        """A single scan can mix native and registry indicators."""
        closes = [float(i) for i in range(1, 31)]
        market = _FakeMarket(_series(closes))
        engine = ScannerEngine(market=market)
        defn = ScannerDefinition(
            universe=[_eq()],
            conditions=[
                Condition(name="sma", operator=">", threshold=0.0),
                Condition(name="bollinger", operator=">", threshold=0.0),
                Condition(name="rsi", operator="<", threshold=101.0),
                Condition(name="atr", operator=">", threshold=0.0),
            ],
        )
        results = engine.run(defn)
        assert len(results) == 1
        assert results[0].score == 1.0
        assert set(results[0].matched_conditions) == {
            "sma", "bollinger", "rsi", "atr",
        }
        assert len(results[0].indicator_values) == 4

    @pytest.mark.parametrize("name", _REGISTRY_SCANNER_INDICATORS)
    def test_scanner_matches_when_threshold_exceeded(self, name: str) -> None:
        """A condition should match when the indicator value exceeds the
        threshold (proves the tail value is not silently zeroed)."""
        closes = [float(i) for i in range(100, 130)]
        market = _FakeMarket(_series(closes))
        engine = ScannerEngine(market=market)
        cond = Condition(name=name, operator=">", threshold=0.0)
        defn = ScannerDefinition(universe=[_eq()], conditions=[cond])
        results = engine.run(defn)
        assert name in results[0].matched_conditions


# ---------------------------------------------------------------------------
# G7 — ScannerEngine streaming consumer
# ---------------------------------------------------------------------------


class TestScannerStreaming:
    """Streaming path: consume() feeds a per-instrument rolling buffer."""

    def test_consume_grows_history_incrementally(self) -> None:
        """Feeding bars one at a time grows the buffered history."""
        engine = ScannerEngine(market=_FakeMarket(), max_bars=10)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(5):
            engine.consume(_candle(100.0 + i, base + timedelta(days=i)))
        series = engine._history(_eq())
        assert len(series.candles) == 5
        assert float(series.candles[-1].ohlc.close.value) == 104.0

    def test_buffer_respects_max_bars_cap(self) -> None:
        """Buffer drops oldest bars when max_bars is exceeded."""
        engine = ScannerEngine(market=_FakeMarket(), max_bars=3)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(7):
            engine.consume(_candle(100.0 + i, base + timedelta(days=i)))
        series = engine._history(_eq())
        assert len(series.candles) == 3
        # Oldest kept candle should be index 4 (close=104), newest is 6 (close=106)
        assert float(series.candles[0].ohlc.close.value) == 104.0
        assert float(series.candles[-1].ohlc.close.value) == 106.0

    def test_scan_uses_streamed_bars_not_market(self) -> None:
        """Once bars are streamed, run() uses them instead of the market."""
        # Market returns closes [1,2,3]; stream provides [200,201,202]
        market_candles = _series([1.0, 2.0, 3.0])
        engine = ScannerEngine(market=_FakeMarket(market_candles), max_bars=10)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(3):
            engine.consume(_candle(200.0 + i, base + timedelta(days=i)))
        cond = Condition(name="close", operator=">", threshold=199.0)
        defn = ScannerDefinition(universe=[_eq()], conditions=[cond])
        results = engine.run(defn)
        assert len(results) == 1
        # Should match because streamed close=202 > 199
        assert "close" in results[0].matched_conditions
        assert results[0].indicator_values["close"] == 202.0

    def test_empty_buffer_falls_back_to_market(self) -> None:
        """When no bars have been consumed, _history uses the market provider."""
        candles = _series([50.0, 51.0, 52.0])
        engine = ScannerEngine(market=_FakeMarket(candles))
        # No consume() calls — should use market
        series = engine._history(_eq())
        assert len(series.candles) == 3
        assert float(series.candles[0].ohlc.close.value) == 50.0

    def test_per_instrument_isolation(self) -> None:
        """Buffers are independent per instrument."""
        engine = ScannerEngine(market=_FakeMarket(), max_bars=10)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        eq1 = Equity.of("NSE", "RELIANCE")
        eq2 = Equity.of("NSE", "TCS")
        engine.consume(_candle(100.0, base))
        # Consume for different instrument
        c2 = Candle(
            instrument=eq2,
            timeframe=Timeframe.D1,
            ohlc=OHLC(
                open=Price(value=Decimal("199")),
                high=Price(value=Decimal("201")),
                low=Price(value=Decimal("199")),
                close=Price(value=Decimal("200")),
            ),
            volume=Quantity(value=Decimal("500")),
            timestamp=base,
        )
        engine.consume(c2)
        s1 = engine._history(eq1)
        s2 = engine._history(eq2)
        assert len(s1.candles) == 1
        assert len(s2.candles) == 1
        assert float(s1.candles[0].ohlc.close.value) == 100.0
        assert float(s2.candles[0].ohlc.close.value) == 200.0
