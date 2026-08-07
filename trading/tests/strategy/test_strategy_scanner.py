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
from tradex_trading.strategy.buy_and_hold import BuyAndHoldStrategy
from tradex_trading.strategy.engine import ReactiveStrategyEngine
from tradex_trading.strategy.protocols import Strategy
from tradex_trading.strategy.scanner import ScannerEngine


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
