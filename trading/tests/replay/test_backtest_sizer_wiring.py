"""The backtest reaches the sizer, and every recorded fill names its instrument.

Two wiring sites that other lanes left behind, and the reason each matters.

**The sizer.** ``ReactiveStrategyEngine`` sizes through a ``PositionSizer`` and
falls back to :func:`unconfigured_sizer` — a fixed Rs 100,000 notional — when it
is given none, and the backtest's recording-only bridge used to carry a *second*
implementation of "signal -> quantity" that read ``abs(signal.strength)``. So a
backtest sized nothing by its own logic at all, and the two ways a strategy can
emit a signal were sized by two different rules. AC-5's parity half is that both
sides ask the same object.

**The instrument.** ``BacktestResult.fills`` recorded no instrument, so the
validation report's round trip counter had to recover it from the bar the fill
was stamped against. That is exact for one instrument and *refused* when two
symbols share a timestamp — which is most multi-symbol research runs, so a
multi-symbol report could not be produced at all. The records carry the key.

The sizes below are hand-computed; the arithmetic is in each test's comment and
is the reason the assertions are numbers rather than "whatever came out".
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from tradex_analytics.validation_report import ValidationReportError, round_trips_from_fills
from tradex_domain import OHLC, Candle, OrderSide, Signal, Timeframe
from tradex_domain.enums import AssetClass, ExchangeId
from tradex_domain.instruments import Equity, InstrumentId
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.risk import RiskManager
from tradex_replay.backtest import BacktestEngine
from tradex_strategy.core.sizing import PositionSizer

# --------------------------------------------------------------------------- fixtures

#: Carries the lot and tick the master would record, so the sizer's risk
#: denominator is knowable. ``Equity.of`` leaves both ``None``, and a sizer that
#: cannot know a lot size has no risk denominator — which is the honest state of
#: the tree, and is why these tests supply one rather than working around it.
INSTRUMENT = Equity(
    instrument_id=InstrumentId.equity("NSE", "RELIANCE"),
    symbol="RELIANCE",
    exchange=ExchangeId.NSE,
    asset_class=AssetClass.EQUITY,
    lot_size=Decimal("1"),
    tick_size=Decimal("0.01"),
)

OTHER = Equity.of("NSE", "TCS")

#: A signal declaring this pair is sized by its stop, not by a notional.
LEVELS = {"stop_loss_price": Decimal("98"), "target_price": Decimal("105")}


def _candles(prices: list[float], instrument: Any = INSTRUMENT) -> list[Candle]:
    out: list[Candle] = []
    for i, p in enumerate(prices):
        price = Price(value=Decimal(str(p)))
        out.append(
            Candle(
                instrument=instrument,
                timeframe=Timeframe.D1,
                ohlc=OHLC(open=price, high=price, low=price, close=price),
                volume=Quantity(value=Decimal("1000")),
                timestamp=datetime(2026, 9, 1 + i, tzinfo=UTC),
            )
        )
    return out


class _RecordingOnly:
    """Records signals without returning them — the backtest's own bridge.

    ``returns=False`` makes it the recording-only path, which is the one the
    strength-as-size read lived in. ``returns=True`` is the engine path, which
    goes through ``ReactiveStrategyEngine`` instead.
    """

    def __init__(
        self,
        *,
        strength: float = 1.0,
        quantity: Quantity | None = None,
        levels: dict[str, Any] | None = None,
        buys: int = 1,
        returns: bool = False,
    ) -> None:
        self._signals: list[Signal] = []
        self._bar = 0
        self._strength = strength
        self._quantity = quantity
        self._levels = levels or {}
        self._buys = buys
        self._returns = returns

    @property
    def strategy_id(self) -> str:
        return "sizer-probe"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def signals(self) -> list:
        return list(self._signals)

    def _signal(self, candle: Candle) -> Signal:
        return Signal(
            instrument=candle.instrument,
            direction=OrderSide.BUY,
            strength=self._strength,
            quantity=self._quantity,
            reason="probe",
            metadata=dict(self._levels),
            timestamp=candle.timestamp,
        )

    def on_bar(self, context, candle):
        self._bar += 1
        if self._bar > self._buys:
            return None
        signal = self._signal(candle)
        self._signals.append(signal)
        return signal if self._returns else None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill) -> None:
        """No-op fill hook."""


# --------------------------------------------------------------------------- the sizer


def test_the_engine_path_in_a_backtest_is_sized_by_the_run_sizer() -> None:
    """A signal with no quantity and a declared stop is sized by the stop.

    Hand-computed on this fixture — entry 100.00, stop 98.00, tick 0.01, lot 1::

        distance      = 100.00 - 98.00          = 2.00
        ticks         = ceil(2.00 / 0.01)        = 200
        risk per lot  = 200 x 0.01 x 1           = 2.00
        budget        = 0.01 x 100,000           = 1,000
        lots          = 1,000 / 2.00             = 500
        ceiling       = 500,000 / 100.00         = 5,000   (does not bind)

    So the fill is **500**, and the three answers this test has to tell apart are
    all different numbers: 500 is the risk size, 5,000 is what the same sizer
    would produce with no risk inputs (its fallback notional), and 1,000 is what
    the engine's *unwired* fallback (``unconfigured_sizer``'s fixed Rs 100,000
    notional) produces. If ``run`` stops passing a sizer, this fails at 1,000.
    """
    result = BacktestEngine(
        initial_capital=Decimal("100000"),
        max_order_value=Decimal("500000"),
        risk_fraction=Decimal("0.01"),
    ).run(_RecordingOnly(levels=LEVELS, returns=True), _candles([100.0, 100.0, 100.0]))

    assert [f["qty"] for f in result.fills] == [500.0]
    assert result.num_rejected == 0


def test_the_bridge_path_in_a_backtest_is_sized_by_the_run_sizer_not_by_strength() -> None:
    """``Signal.strength`` cannot move a backtest fill by a single unit.

    The recording-only bridge used to compute ``abs(signal.strength) if signal
    strength else 1.0`` — so the two ways a strategy could emit a signal were
    sized by two different rules and every shipped strategy (all of which emit
    ``strength=1.0``) bought one share through this one.

    The sizer is given a ceiling of Rs 5,000 and no risk inputs, so a signal with
    no quantity and no stop takes the configured fallback: ``floor(5000 / 100.00)``
    = **50** units. Every strength below must produce that same 50, and none of
    them is 50.
    """
    sizer = PositionSizer(
        max_order_value=Decimal("5000"),
        fallback_notional=Decimal("5000"),
        label="backtest-probe",
    )
    quantities = set()
    for strength in (1.0, 0.0, -3.0, 3.5, 1000.0):
        result = BacktestEngine(sizer=sizer).run(
            _RecordingOnly(strength=strength), _candles([100.0, 100.0, 100.0])
        )
        assert [f["qty"] for f in result.fills] == [50.0], strength
        quantities.add(result.fills[0]["qty"])

    assert len(quantities) == 1
    assert quantities.isdisjoint({1.0, 3.0, 1000.0})


def test_both_emission_paths_in_a_backtest_get_the_same_size() -> None:
    """The parity claim itself: returned and recorded signals agree.

    AC-5 is parity between the sizing that runs live and the sizing that runs in
    a backtest. With two implementations of "signal -> quantity" in the backtest
    alone there was nothing to be in parity *with*, so this asserts the two paths
    through one run produce the same fill.
    """
    engine = BacktestEngine(
        initial_capital=Decimal("100000"),
        max_order_value=Decimal("500000"),
        risk_fraction=Decimal("0.01"),
    )
    candles = _candles([100.0, 100.0, 100.0])
    returned = engine.run(_RecordingOnly(levels=LEVELS, returns=True), candles)
    recorded = engine.run(_RecordingOnly(levels=LEVELS, returns=False), candles)

    assert [f["qty"] for f in returned.fills] == [f["qty"] for f in recorded.fills]


def test_a_signal_the_sizer_cannot_size_publishes_nothing() -> None:
    """A refusal is a dropped signal, not a defaulted one.

    No quantity, no declared stop, no risk inputs and no ``fallback_notional``:
    the sizer cannot decide, and an order that cannot be sized is not an order.
    The signal is still reported in ``trades`` — the strategy emitted it, it
    simply did not trade.
    """
    result = BacktestEngine(
        sizer=PositionSizer(max_order_value=Decimal("5000")),
    ).run(_RecordingOnly(), _candles([100.0, 100.0, 100.0]))

    assert result.fills == []
    assert result.num_trades == 0
    assert len(result.trades) == 1


def test_a_stated_quantity_survives_the_backtest_sizer() -> None:
    """The strategy's own size is honoured, and still clamped by the ceiling.

    ``Signal.quantity`` is the interface AC-5 names. 10,000 units at 100.00 is
    Rs 1,000,000, so the Rs 500,000 ceiling clamps it to ``floor(500000/100)`` =
    **5,000**.
    """
    result = BacktestEngine(max_order_value=Decimal("500000")).run(
        _RecordingOnly(quantity=Quantity(Decimal("10000")), returns=True),
        _candles([100.0, 100.0, 100.0]),
    )
    assert [f["qty"] for f in result.fills] == [5000.0]


# --------------------------------------------------------------- the instrument key


class _RoundTripper:
    """A recorded BUY on bar 1 and a recorded SELL on bar 2, per instrument."""

    def __init__(self) -> None:
        self._signals: list[Signal] = []
        self._seen: dict[str, int] = {}

    @property
    def strategy_id(self) -> str:
        return "round-tripper"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def signals(self) -> list:
        return list(self._signals)

    def on_bar(self, context, candle):
        symbol = candle.instrument.symbol
        count = self._seen.get(symbol, 0) + 1
        self._seen[symbol] = count
        # BUY on the first bar of each symbol, SELL on the second, so each fills
        # against the next bar and three bars per symbol is a whole round trip.
        if count in (1, 2):
            signal = Signal(
                instrument=candle.instrument,
                direction=OrderSide.BUY if count == 1 else OrderSide.SELL,
                strength=1.0,
                quantity=Quantity(Decimal("10")),
                reason=f"{symbol}{count}",
                timestamp=candle.timestamp,
            )
            self._signals.append(signal)
        return None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill) -> None:
        """No-op fill hook."""


def _two_symbols_sharing_timestamps() -> list[Candle]:
    """Interleaved bars: both symbols print on the same three timestamps.

    Interleaved rather than concatenated so neither symbol's bars are a
    contiguous block of the stream — the shape a portfolio run actually has, and
    the one where a timestamp alone cannot name an instrument.
    """
    return [
        candle
        for pair in zip(_candles([100.0, 100.0, 100.0], INSTRUMENT),
                        _candles([200.0, 200.0, 200.0], OTHER))
        for candle in pair
    ]


def test_every_recorded_fill_names_its_own_instrument() -> None:
    """Both record sites carry it: the filled one and the rejected one.

    ``recorded_fills.append`` happens in two places — ``_on_fill`` and
    ``_on_rejected`` — and a key added to only one of them is a shape that looks
    right on the happy path and is empty exactly when something went wrong.
    """
    candles = _candles([100.0, 100.0, 100.0])
    filled = BacktestEngine().run(
        _RecordingOnly(quantity=Quantity(Decimal("10")), returns=True), candles
    )
    assert [f["instrument"] for f in filled.fills] == ["NSE:RELIANCE"]

    rejected = BacktestEngine(
        risk_manager=RiskManager(max_order_value=Decimal("1"))
    ).run(
        _RecordingOnly(quantity=Quantity(Decimal("10")), returns=True), candles
    )
    assert [f["rejected"] for f in rejected.fills] == [True]
    assert [f["instrument"] for f in rejected.fills] == ["NSE:RELIANCE"]


def test_a_multi_symbol_run_produces_a_report_when_two_symbols_share_a_timestamp() -> None:
    """The case that used to be refused, and it is the deliverable.

    A validation report counts round trips *per instrument*. ``BacktestResult
    .fills`` carried no instrument, so ``round_trips_from_fills`` recovered it
    from the bar the fill was stamped against — and two symbols on one timestamp
    is an ambiguity, not an answer, so it raised. That is right, and it also
    meant a multi-symbol report could not be produced at all. With the key on the
    record, each symbol's BUY and SELL pair into its own round trip.
    """
    candles = _two_symbols_sharing_timestamps()
    assert len({c.timestamp for c in candles}) == 3, "the fixture must collide"

    result = BacktestEngine().run(_RoundTripper(), candles)

    # Two symbols, each bought and sold: two round trips, not one and not zero.
    trips = round_trips_from_fills(result.fills, bars=candles)
    assert len(trips) == 2
    assert sorted(t.symbol for t in trips) == ["NSE:RELIANCE", "NSE:TCS"]


def test_the_multi_symbol_case_is_the_one_that_used_to_be_refused() -> None:
    """The control: with the key removed from the records, this raises.

    Proven rather than asserted — the same fixture, the same bars, the same
    fills, with the instrument deleted from each record. If the pair of records
    ever stopped colliding on a timestamp this would stop raising and the
    negative control would be measuring nothing.
    """
    candles = _two_symbols_sharing_timestamps()
    result = BacktestEngine().run(_RoundTripper(), candles)

    stripped = [
        {k: v for k, v in fill.items() if k != "instrument"} for fill in result.fills
    ]
    with pytest.raises(ValidationReportError) as excinfo:
        round_trips_from_fills(stripped, bars=candles)
    assert "ambiguous" in str(excinfo.value)
