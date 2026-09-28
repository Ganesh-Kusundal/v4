"""Position sizing — the one thing that turns a signal into an order size.

Before this module the size was ``abs(signal.strength)``, and every shipped
strategy emits ``strength=1.0``, so every strategy in the repository bought
exactly one share. ``Signal.strength`` is signal *quality*; sizing is a
separate decision and now lives here, with a target fixed by the production
readiness spec:

    lots = min(max_order_value / (lot x price),
               risk_fraction x equity / (stop_distance x ticksize x lot_size))
    quantity = max(lots, 1) x lot_size

Every test below is a case where an implementation can *look* right while
changing nothing, so each pins the specific constraint that fired:

- a size above ``max_order_value`` is clamped by the cap, and a control sizer
  with a huge cap proves the cap (not the formula) is what bound it;
- a computed size below one lot floors to one lot rather than to nothing;
- a signal with no quantity and no knowable denominator is **refused**, and one
  with a configured notional is sized to that notional — never to one share;
- ``Signal.strength`` does not move the size by a single unit.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tradex_domain import OHLC, Candle, OrderSide, Quote, Signal, Timeframe
from tradex_domain.enums import AssetClass, ExchangeId
from tradex_domain.instruments import (
    Commodity,
    Currency,
    Equity,
    Future,
    Index,
    Instrument,
    InstrumentId,
    InstrumentMeta,
    Option,
)
from tradex_domain.value_objects import Price, Quantity
from tradex_reactive.bus import ReactiveBus
from tradex_strategy.core.brackets import declared_levels, protective_request
from tradex_strategy.core.engine import ReactiveStrategyEngine
from tradex_strategy.core.sizing import (
    CASH_EQUITY_ASSET_CLASSES,
    CASH_EQUITY_LOT_SIZE,
    DEFAULT_TICK_SIZE,
    ContractSpec,
    PositionSizer,
)

# -- fixtures in round rupee/tick numbers, so every expected size is hand-checkable --
from strategy._ac22_recorder import RecordingSubmit

REPO = Path(__file__).resolve().parents[3]  # <repo>/trading/tests/strategy/this_file.py
_T0 = datetime(2026, 7, 31, 10, 30, tzinfo=UTC)
EQUITY_RS = Decimal("1000000")  # account equity
RISK_FRACTION = Decimal("0.01")  # 1% of equity at risk
LOT = Decimal("50")
TICK = Decimal("0.05")
ENTRY = Decimal("2500")
STOP = Decimal("2475")  # 25.00 away = 500 ticks = 1250.00 at risk per lot
TARGET = Decimal("2550")
RISK_PER_LOT = Decimal("1250")  # 500 ticks x 0.05 x 50
LOTS_BY_RISK = Decimal("8")  # 1% of 1,000,000 = 10,000 / 1,250
UNITS_BY_RISK = Decimal("400")  # 8 lots x 50
BIG_CAP = Decimal("5000000")  # never the binding constraint below


def _instrument(
    lot: str | None = "50", tick: str | None = "0.05", *, meta: InstrumentMeta | None = None,
) -> Equity:
    return Equity(
        instrument_id=InstrumentId.equity("NSE", "RELIANCE"),
        symbol="RELIANCE",
        exchange=ExchangeId.NSE,
        asset_class=AssetClass.EQUITY,
        lot_size=Decimal(lot) if lot is not None else None,
        tick_size=Decimal(tick) if tick is not None else None,
        meta=meta or InstrumentMeta(),
    )


def _signal(
    instrument: Equity | None = None,
    *,
    quantity: Quantity | None = None,
    strength: float = 1.0,
    stop: Decimal | None = STOP,
    target: Decimal | None = TARGET,
    side: OrderSide = OrderSide.BUY,
) -> Signal:
    metadata: dict[str, object] = {}
    if stop is not None:
        metadata["stop_loss_price"] = stop
    if target is not None:
        metadata["target_price"] = target
    return Signal(
        instrument=instrument or _instrument(),
        direction=side,
        strength=strength,
        reason="test",
        quantity=quantity,
        metadata=metadata,
    )


def _sizer(**overrides: object) -> PositionSizer:
    kwargs: dict[str, object] = {
        "max_order_value": BIG_CAP,
        "risk_fraction": RISK_FRACTION,
        "equity": EQUITY_RS,
    }
    kwargs.update(overrides)
    return PositionSizer(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the formula
# ---------------------------------------------------------------------------


def test_the_risk_formula_sizes_to_the_stop_the_signal_declared() -> None:
    decision = _sizer().size(_signal(), reference_price=ENTRY)

    assert decision.ok
    assert decision.quantity == Quantity(Decimal("400"))
    assert decision.source == "stop_distance"
    assert decision.lots == LOTS_BY_RISK
    assert decision.risk_per_lot == RISK_PER_LOT
    assert decision.stop_distance_ticks == Decimal("500")
    assert decision.notional == UNITS_BY_RISK * ENTRY
    assert decision.clamped_by is None


def test_a_size_above_max_order_value_is_clamped_by_the_cap_not_the_formula() -> None:
    cap = Decimal("250000")  # exactly 2 lots at 2500
    capped = _sizer(max_order_value=cap).size(_signal(), reference_price=ENTRY)
    uncapped = _sizer(max_order_value=BIG_CAP).size(_signal(), reference_price=ENTRY)

    # The cap is what bound it...
    assert capped.clamped_by == "max_order_value"
    assert capped.quantity == Quantity(Decimal("100"))  # 2 lots, not 8
    assert capped.notional == cap  # and it binds exactly, not approximately
    # ...proved by a control in which the same formula is free to run.
    assert uncapped.quantity == Quantity(UNITS_BY_RISK)
    assert uncapped.quantity.value > capped.quantity.value


def test_a_computed_size_below_one_lot_floors_to_one_lot() -> None:
    # 1% of 100 is 1 rupee against 1,250 at risk per lot: 0.0008 lots.
    decision = _sizer(equity=Decimal("100")).size(_signal(), reference_price=ENTRY)

    assert decision.ok
    assert decision.quantity == Quantity(LOT)  # one lot, not zero and not one share
    assert decision.clamped_by == "lot_floor"
    assert decision.lots == Decimal("1")


def test_a_cap_smaller_than_one_lot_is_refused_rather_than_exceeded() -> None:
    # Flooring to one lot here would breach the cap the sizer exists to enforce,
    # so the two irreconcilable constraints produce no order at all.
    decision = _sizer(max_order_value=Decimal("1000")).size(_signal(), reference_price=ENTRY)

    assert not decision.ok
    assert decision.quantity is None
    assert decision.source == "refused"
    assert "one lot" in decision.reason


# ---------------------------------------------------------------------------
# the denominator is not knowable
# ---------------------------------------------------------------------------


def test_a_signal_with_no_stop_uses_the_configured_notional_and_never_one_share() -> None:
    decision = _sizer(fallback_notional=Decimal("50000")).size(
        _signal(_instrument(lot="1", tick=None), stop=None, target=None),
        reference_price=ENTRY,
    )

    assert decision.ok
    assert decision.source == "fallback_notional"
    assert decision.quantity == Quantity(Decimal("20"))  # 50,000 / 2,500
    assert decision.quantity.value != 1
    assert "stop" in decision.reason


def test_a_signal_with_no_stop_and_no_configured_notional_is_refused() -> None:
    decision = _sizer().size(
        _signal(_instrument(lot="1", tick=None), stop=None, target=None),
        reference_price=ENTRY,
    )

    assert not decision.ok
    assert decision.quantity is None
    assert decision.source == "refused"
    assert decision.reason


def test_a_cash_equity_with_no_recorded_lot_size_sizes_to_one_unit_lots() -> None:
    # A missing lot size is unknown for a contract, and *not* unknown for a cash
    # equity: NSE/BSE cash equities trade in units of one share, so 1 is the
    # venue's rule rather than a guess. Every source here is empty — the typed
    # field, the typed meta, the master's ``meta.extra`` — so this exercises the
    # default and not a fallback.
    #   tick 0.05, entry 2500, stop 2475 -> 25.00 away = 500 ticks
    #   risk per lot = 500 x 0.05 x 1 lot = 25.00
    #   1% of 1,000,000 = 10,000 -> 10,000 / 25.00 = 400 lots of 1 = 400 units
    decision = _sizer().size(_signal(_instrument(lot=None)), reference_price=ENTRY)

    assert decision.ok
    assert decision.source == "stop_distance"
    assert decision.lots == Decimal("400")
    assert decision.risk_per_lot == Decimal("25")
    assert decision.stop_distance_ticks == Decimal("500")
    assert decision.quantity == Quantity(Decimal("400"))
    assert decision.clamped_by is None


def test_the_cash_equity_lot_default_is_the_venue_rule_and_is_stated_as_one() -> None:
    # The number is pinned, so a later edit that turns it into a tunable (or
    # something else) fails here rather than silently resizing every live order.
    assert CASH_EQUITY_LOT_SIZE == Decimal("1")
    assert AssetClass.EQUITY in CASH_EQUITY_ASSET_CLASSES
    # And it is reachable from the outside: a lot the master DID record still
    # wins, because a recorded value is information and a default is a rule.
    assert _sizer().size(_signal(_instrument(lot="50")), reference_price=ENTRY).lots == LOTS_BY_RISK


@pytest.mark.parametrize(
    "instrument",
    [
        Future.of("NSE", "RELIANCE", date(2026, 10, 29)),
        Option.of("NSE", "RELIANCE", date(2026, 10, 29), Decimal("2500"), "CE"),
        Currency.of("NSE", "USDINR"),
        Commodity.of("MCX", "GOLD"),
        Index.of("NSE", "NIFTY50"),
    ],
    ids=["future", "option", "currency", "commodity", "index"],
)
def test_a_derivative_with_no_contract_specification_is_still_refused(
    instrument: Instrument,
) -> None:
    """The control that matters most: the cash-equity default must not generalise.

    Sizing a contract on an invented lot size is how money is lost, so every
    asset class whose lot size is a property of *the contract* still refuses
    when no master has recorded one — no fallback notional is configured here,
    so the honest answer is no order at all.
    """
    decision = _sizer().size(_signal(instrument), reference_price=ENTRY)

    assert not decision.ok
    assert decision.quantity is None
    assert decision.source == "refused"
    assert "lot" in decision.reason
    assert instrument.asset_class not in CASH_EQUITY_ASSET_CLASSES


def test_a_derivative_with_a_recorded_lot_size_sizes_normally() -> None:
    # The control for the control: the refusal above is about a *missing*
    # specification, not about derivatives being unsizable. A future whose
    # master row carries its lot sizes sizes on the same formula.
    future = dataclasses.replace(
        Future.of("NSE", "RELIANCE", date(2026, 10, 29)),
        lot_size=LOT,
        tick_size=TICK,
    )
    decision = _sizer().size(_signal(future), reference_price=ENTRY)

    assert decision.ok
    assert decision.quantity == Quantity(UNITS_BY_RISK)  # 8 lots of 50


def test_a_stated_quantity_wins_over_every_derivation() -> None:
    # An explicit size is an instruction. The sizer has no risk fraction, no
    # equity, no stop and no lot size to work with — and must still obey, rather
    # than substitute a notional or refuse.
    sizer = _sizer(fallback_notional=Decimal("50000"))
    decision = sizer.size(
        _signal(_instrument(lot=None, tick=None), quantity=Quantity(Decimal("7")),
                stop=None, target=None),
        reference_price=ENTRY,
    )

    assert decision.source == "signal"
    assert decision.quantity == Quantity(Decimal("7"))
    # ...and it wins on a derivative too, because "3" is not a guess about a
    # contract specification. The venue's own lot check remains the last word.
    future = Future.of("NSE", "RELIANCE", date(2026, 10, 29))
    on_contract = sizer.size(
        _signal(future, quantity=Quantity(Decimal("3")), stop=None, target=None),
        reference_price=ENTRY,
    )
    assert on_contract.source == "signal"
    assert on_contract.quantity == Quantity(Decimal("3"))


def test_a_non_positive_equity_is_not_a_risk_budget() -> None:
    # Equity 0 with a stop yields a 0-rupee budget, and the "floor at one lot"
    # rule would turn that into a full lot. A zero account is not a sizing
    # policy, so the sizer falls back to the configured notional instead.
    decision = _sizer(equity=Decimal("0"), fallback_notional=Decimal("50000")).size(
        _signal(_instrument(lot="1", tick="0.05")), reference_price=ENTRY,
    )

    assert decision.source == "fallback_notional"
    assert decision.quantity == Quantity(Decimal("20"))
    assert "equity" in decision.reason


def test_a_stop_at_the_entry_is_no_stop_at_all() -> None:
    # Distance 0 is a division by zero dressed as a stop. It must not raise and
    # must not become a size by the formula.
    refused = _sizer().size(_signal(stop=ENTRY), reference_price=ENTRY)
    assert not refused.ok
    assert refused.quantity is None

    fell_back = _sizer(fallback_notional=Decimal("50000")).size(
        _signal(_instrument(lot="1", tick=None), stop=ENTRY, target=TARGET),
        reference_price=ENTRY,
    )
    assert fell_back.source == "fallback_notional"
    assert fell_back.quantity == Quantity(Decimal("20"))


def test_a_non_positive_price_is_refused() -> None:
    decision = _sizer().size(_signal(), reference_price=Decimal("0"))

    assert not decision.ok
    assert decision.quantity is None


# ---------------------------------------------------------------------------
# lot size and tick size: where the master's data actually lives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "instrument",
    [
        _instrument(lot="50"),  # the typed Instrument field
        _instrument(lot=None, meta=InstrumentMeta(lot_size=50)),  # typed meta field
        _instrument(lot=None, meta=InstrumentMeta(extra={"lot_size": "50"})),  # master row
    ],
    ids=["instrument_field", "meta_field", "master_meta"],
)
def test_the_lot_size_is_read_from_wherever_the_master_put_it(instrument: Equity) -> None:
    # The Dhan/Upstox master loaders park lot and tick in the registry meta as
    # strings, and the typed Instrument fields were never populated — so a sizer
    # that reads only ``instrument.lot_size`` sees None for every real symbol.
    decision = _sizer().size(_signal(instrument), reference_price=ENTRY)

    assert decision.ok
    assert decision.quantity == Quantity(UNITS_BY_RISK)
    assert decision.quantity.value % LOT == 0


def test_a_contract_lookup_supplies_the_lot_size_the_instrument_does_not_carry() -> None:
    # ``Instrument`` objects built from a master row have no lot size, so the
    # wiring passes the registry's own answer in.
    sizer = _sizer(
        contracts=lambda _iid: ContractSpec(lot_size=LOT, tick_size=TICK),
    )
    decision = sizer.size(_signal(_instrument(lot=None, tick=None)), reference_price=ENTRY)

    assert decision.ok
    assert decision.quantity == Quantity(UNITS_BY_RISK)
    assert decision.stop_distance_ticks == Decimal("500")


def test_the_typed_instrument_fields_win_over_the_registry_meta() -> None:
    # A typed 50 must not be overridden by a stale 7 in the registry meta: the
    # master loader's string is the fallback for a field the master never filled.
    instrument = _instrument(lot="50", meta=InstrumentMeta(lot_size=7))
    decision = _sizer().size(_signal(instrument), reference_price=ENTRY)

    assert decision.quantity == Quantity(UNITS_BY_RISK)  # 8 lots of 50, not of 7


def test_the_tick_size_rounds_the_stop_distance_up_to_a_whole_tick() -> None:
    # Entry 100, stop 99.30, tick 0.50 → 1.4 ticks. A venue cannot stop you out
    # between ticks, so the distance that sizes the trade is 2 ticks, which is a
    # smaller position than the 1.4 the raw price distance would have bought.
    ticked = _sizer(
        equity=Decimal("100"), risk_fraction=Decimal("0.1"),
    ).size(_signal(_instrument(lot="1", tick="0.5"), stop=Decimal("99.3"),
                   target=Decimal("101")), reference_price=Decimal("100"))
    untiled = _sizer(
        equity=Decimal("100"), risk_fraction=Decimal("0.1"),
    ).size(_signal(_instrument(lot="1", tick=None), stop=Decimal("99.3"),
                   target=Decimal("101")), reference_price=Decimal("100"))

    assert ticked.stop_distance_ticks == Decimal("2")
    assert ticked.risk_per_lot == Decimal("1")  # 2 ticks x 0.50 x 1
    assert ticked.quantity == Quantity(Decimal("10"))
    # The control: no tick recorded anywhere, so the sizer falls back to the
    # configured DEFAULT_TICK_SIZE of 0.01, which rounds 0.70 up to 70 ticks —
    # the same 0.70 of risk the raw distance gave, for a stated reason rather
    # than an accident of the tick being None.
    assert DEFAULT_TICK_SIZE == Decimal("0.01")
    assert untiled.stop_distance_ticks == Decimal("70")
    assert untiled.risk_per_lot == Decimal("0.70")
    assert untiled.quantity == Quantity(Decimal("14"))
    assert untiled.tick_size_defaulted is True
    assert ticked.quantity.value < untiled.quantity.value


def test_the_tick_default_is_configuration_and_the_decision_says_it_was_one() -> None:
    # Tick size is not derivable — it is a per-security venue value — so it comes
    # from configuration. Two properties are load bearing: a configured tick is
    # actually used, and a substituted one is *visible* on the decision, because
    # ``stop_distance_ticks`` otherwise reads as a real venue tick count.
    configured = _sizer(
        tick_size=Decimal("0.5"),
        equity=Decimal("100"),
        risk_fraction=Decimal("0.1"),
    ).size(
        _signal(_instrument(lot="1", tick=None), stop=Decimal("99.3"),
                target=Decimal("101")),
        reference_price=Decimal("100"),
    )
    recorded = _sizer(
        equity=Decimal("100"), risk_fraction=Decimal("0.1"),
    ).size(
        _signal(_instrument(lot="1", tick="0.5"), stop=Decimal("99.3"),
                target=Decimal("101")),
        reference_price=Decimal("100"),
    )

    assert configured.stop_distance_ticks == recorded.stop_distance_ticks == Decimal("2")
    assert configured.tick_size == recorded.tick_size == Decimal("0.5")
    assert recorded.tick_size_defaulted is False
    assert configured.tick_size_defaulted is True
    assert configured.quantity == recorded.quantity == Quantity(Decimal("10"))


def test_a_non_positive_configured_tick_size_is_refused_rather_than_defaulted() -> None:
    # Silently falling back to the default would make a typo look like a policy.
    for bad in (Decimal("0"), Decimal("-0.05"), "not a tick"):
        with pytest.raises(ValueError, match="tick_size"):
            _sizer(tick_size=bad)


# ---------------------------------------------------------------------------
# an explicit Signal.quantity is intent, not exemption
# ---------------------------------------------------------------------------


def test_an_explicit_quantity_is_still_rounded_down_to_a_whole_lot() -> None:
    decision = _sizer().size(
        _signal(quantity=Quantity(Decimal("137"))), reference_price=ENTRY,
    )

    assert decision.quantity == Quantity(Decimal("100"))  # 2 lots, never 137
    assert decision.clamped_by == "lot_round"
    assert decision.source == "signal"


def test_an_explicit_quantity_still_obeys_max_order_value() -> None:
    decision = _sizer(max_order_value=Decimal("250000")).size(
        _signal(quantity=Quantity(Decimal("4000"))), reference_price=ENTRY,
    )

    assert decision.quantity == Quantity(Decimal("100"))
    assert decision.clamped_by == "max_order_value"


def test_an_explicit_quantity_on_a_cash_equity_is_still_rounded_to_a_whole_lot() -> None:
    # The same guarantee, arrived at from a different direction. Before the cash
    # equity rule a missing lot size left the stated count unrounded and logged a
    # warning; now the lot is 1 by the venue's rule, so the count is a whole
    # number of lots *by construction* rather than by omission — and a recorded
    # lot of 50 still truncates 137 to 100 (above), which is what proves the
    # rounding is real and not just an artefact of everything being 1.
    decision = _sizer(max_order_value=Decimal("250000")).size(
        _signal(_instrument(lot=None), quantity=Quantity(Decimal("40"))),
        reference_price=ENTRY,
    )

    assert decision.quantity == Quantity(Decimal("40"))
    assert decision.clamped_by is None

    capped = _sizer(max_order_value=Decimal("25000")).size(
        _signal(_instrument(lot=None), quantity=Quantity(Decimal("40"))),
        reference_price=ENTRY,
    )
    assert capped.quantity == Quantity(Decimal("10"))
    assert capped.clamped_by == "max_order_value"


# ---------------------------------------------------------------------------
# Signal.strength is inert
# ---------------------------------------------------------------------------


def test_signal_strength_does_not_move_the_size_by_a_single_unit() -> None:
    sizer = _sizer()
    sizes = {
        _sizer().size(_signal(strength=value), reference_price=ENTRY).quantity
        for value in (1.0, 0.0, -3.0, 3.5, 1000.0)
    }

    assert sizes == {sizer.size(_signal(), reference_price=ENTRY).quantity}
    assert sizes == {Quantity(UNITS_BY_RISK)}

    # And on the fallback path, where the old bug produced exactly one share.
    fallback = {
        _sizer(fallback_notional=Decimal("50000")).size(
            _signal(_instrument(lot="1", tick=None), stop=None, target=None,
                    strength=value),
            reference_price=ENTRY,
        ).quantity
        for value in (1.0, 250.0)
    }
    assert fallback == {Quantity(Decimal("20"))}


def test_the_stop_the_sizer_sizes_against_is_the_stop_the_order_carries() -> None:
    # One declaration, two readers: if the sizer could read a stop the order does
    # not carry, the size would be sized against a risk that is not protected.
    signal = _signal()
    decision = _sizer().size(signal, reference_price=ENTRY)
    request = protective_request(
        signal=signal, entry=Price(value=ENTRY), quantity=decision.quantity,
    )

    assert declared_levels(signal.metadata)[0] == Price(value=STOP)
    assert request.stop_loss_price == Price(value=STOP)


# ---------------------------------------------------------------------------
# Signal.quantity — nullable, and the wire stays readable
# ---------------------------------------------------------------------------


def test_a_signal_with_no_quantity_is_asking_the_sizer() -> None:
    # ``None`` is an instruction ("size this to the risk budget"), not a missing
    # value, so it is the default and every shipped strategy relies on it.
    assert _signal().quantity is None
    assert _signal(quantity=Quantity(Decimal("7"))).quantity == Quantity(Decimal("7"))


def test_a_signal_written_before_the_field_existed_still_loads() -> None:
    # Recorded runs, tapes and event stores hold serialised signals from before
    # ``quantity`` existed. A field added to a frozen dataclass the model forbids
    # removing must not make those payloads unreadable.
    legacy = {
        "instrument": _instrument().to_dict(),
        "direction": OrderSide.BUY.value,
        "strength": 1.0,
        "reason": "recorded before sizing existed",
        "metadata": {},
        "timestamp": _T0.isoformat(),
    }
    restored = Signal.from_dict(legacy)

    assert restored.quantity is None
    assert restored.reason == legacy["reason"]


def test_a_signal_round_trips_its_quantity() -> None:
    signal = _signal(quantity=Quantity(Decimal("7")))
    assert Signal.from_dict(signal.to_dict()).quantity == Quantity(Decimal("7"))


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------


def _candle(instrument: Equity, open_: str, close: str, *, minutes: int = 0) -> Candle:
    o, c = Decimal(open_), Decimal(close)
    return Candle(
        instrument=instrument,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=Price(value=o), high=Price(value=max(o, c)),
                  low=Price(value=min(o, c)), close=Price(value=c)),
        volume=Quantity(value=Decimal("1000")),
        timestamp=_T0 + timedelta(minutes=minutes),
    )


def _quote(instrument: Equity, ltp: str = "2500") -> Quote:
    return Quote(instrument=instrument, ltp=Price(value=Decimal(ltp)), timestamp=_T0)


class _Signaller:
    """Emits whatever signal it was handed, once, on the first quote."""

    def __init__(self, signal: Signal, strategy_id: str = "sizer_test") -> None:
        self._signal = signal
        self.strategy_id = strategy_id
        self.version = "1.0.0"

    def on_quote(self, context, quote):  # noqa: ANN001, ANN201
        return self._signal


def _engine_with(signal: Signal, sizer: PositionSizer) -> tuple[ReactiveBus, list]:
    bus = ReactiveBus()
    orders = RecordingSubmit()
    engine = ReactiveStrategyEngine(bus, orders, sizer=sizer, fill_reference="signal_close")
    engine.register(_Signaller(signal))
    bus.publish(_quote(signal.instrument, "2500"))
    return bus, orders


def test_the_engine_does_not_size_from_strength() -> None:
    sizer = _sizer()
    quantities = []
    for strength in (1.0, 7.0, 1000.0):
        _bus, orders = _engine_with(_signal(strength=strength), sizer)
        assert len(orders) == 1
        quantities.append(orders[0].quantity)

    assert quantities == [Quantity(UNITS_BY_RISK)] * 3
    assert quantities[0] != Quantity(Decimal("1"))


def test_a_refused_size_publishes_nothing_at_all() -> None:
    # No stop, no configured notional: the sizer cannot decide, so no order.
    _bus, orders = _engine_with(_signal(stop=None, target=None), _sizer())

    assert len(orders) == 0


def test_a_shipped_strategy_no_longer_buys_one_share() -> None:
    # Every shipped strategy emits ``strength=1.0`` and no quantity. That used to
    # be an order for one share. Driven through the engine, one of them (the
    # quote-driven buy-and-hold reference) must now produce a real size.
    from tradex_strategy.core.buy_and_hold import BuyAndHoldStrategy

    strategy = BuyAndHoldStrategy("sizer_gate", _instrument(lot="1", tick="0.05"))
    signal = strategy.on_quote(None, _quote(_instrument(lot="1", tick="0.05")))

    assert signal is not None
    assert signal.quantity is None, "the strategy leaves sizing to the sizer"
    assert signal.strength == 1.0  # signal quality, and the whole of it

    bus = ReactiveBus()
    orders = RecordingSubmit()
    engine = ReactiveStrategyEngine(bus, orders, sizer=_sizer(fallback_notional=Decimal("50000")),
        fill_reference="signal_close",
    )
    engine.register(strategy)
    bus.publish(_quote(_instrument(lot="1", tick="0.05")))

    assert len(orders) == 1
    assert orders[0].quantity == Quantity(Decimal("20"))
    assert orders[0].quantity.value != 1


def test_a_shipped_strategy_that_declares_a_stop_is_sized_by_that_stop() -> None:
    # The reference protective-level producer. Its entry signal declares the stop
    # the sizer reads, so the size and the protection cannot disagree — and the
    # size is the risk budget, not the notional fallback.
    from tradex_domain.strategy import StrategyContext
    from tradex_strategy.extensions.strategies.bracket_breakout import (
        BracketBreakoutStrategy,
    )

    instrument = _instrument(lot="10", tick="0.05")
    strategy = BracketBreakoutStrategy(
        "sizer_gate", instrument, lookback=2, atr_period=2, stop_atr=1.0, reward=2.0,
    )
    ctx = StrategyContext(timestamp=_T0)
    closes = ["100", "100", "100", "130"]  # the last close breaks the range high
    signal = None
    for i, close in enumerate(closes):
        signal = strategy.on_bar(ctx, _candle(instrument, close, close, minutes=i))
    assert signal is not None, "the breakout should have entered"

    stop = declared_levels(signal.metadata)[0]
    assert stop is not None, "the entry signal declares a stop"
    assert signal.quantity is None, "the strategy leaves sizing to the sizer"

    # 130 entry, stop 100 below the range (high 100) — the sizer's denominator.
    distance = Decimal("130") - stop.value
    ticks = (distance / Decimal("0.05")).to_integral_value(rounding="ROUND_CEILING")
    risk_per_lot = ticks * Decimal("0.05") * Decimal("10")
    decision = _sizer().size(signal, reference_price=Decimal("130"))

    assert decision.source == "stop_distance"
    assert decision.risk_per_lot == risk_per_lot
    assert decision.quantity == Quantity(
        (RISK_FRACTION * EQUITY_RS / risk_per_lot * Decimal("10")).to_integral_value(
            rounding="ROUND_FLOOR"
        )
    )
    # and the order the engine builds carries both halves of that decision
    bus = ReactiveBus()
    orders = RecordingSubmit()
    engine = ReactiveStrategyEngine(bus, orders, sizer=_sizer(), fill_reference="signal_close",
    )

    class _Bar(_Signaller):
        def on_bar(self, context, candle):  # noqa: ANN001, ANN201
            return signal

    engine.register(_Bar(signal))
    bus.publish(_candle(instrument, "130", "130", minutes=9))
    assert len(orders) == 1
    assert orders[0].quantity == decision.quantity
    assert orders[0].stop_loss_price == stop


def test_no_shipped_strategy_constructs_a_signal_with_its_own_quantity() -> None:
    # The invariant the previous test could only sample: no strategy in the
    # registry may hand the sizer a size. Stated over the package rather than
    # over a driven instance, because a strategy that needs a 200-bar warm-up
    # answers None to a single bar and a driven check then passes vacuously.
    offenders: list[str] = []
    for path in sorted((REPO / "strategy/src/tradex_strategy").rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "test_position_sizing.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "Signal" and any(kw.arg == "quantity" for kw in node.keywords):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert offenders == [], (
        f"a shipped strategy chose its own position size at {offenders}; sizing "
        "belongs to PositionSizer"
    )


def test_the_size_uses_the_price_the_order_fills_at_not_the_signal_bar_close() -> None:
    # next_open defers the signal and fills at the *next* bar's open. Sizing at
    # the signal bar's close would set the notional against a price the order
    # never got.
    instrument = _instrument(lot="1", tick=None)
    signal = _signal(instrument, stop=None, target=None)
    bus = ReactiveBus()
    orders = RecordingSubmit()
    engine = ReactiveStrategyEngine(
        bus, orders, sizer=_sizer(fallback_notional=Decimal("1000")),
        fill_reference="next_open",
    )

    class _BarSignaller(_Signaller):
        def on_bar(self, context, candle):  # noqa: ANN001, ANN201
            return self._signal

    engine.register(_BarSignaller(signal))
    bus.publish(_candle(instrument, "2500", "2500", minutes=0))
    bus.publish(_candle(instrument, "50", "50", minutes=1))

    assert len(orders) == 1
    # 1,000 of notional at a 2,500 signal close would be 0.4 units; at the 50 open
    # it is 20, and 20 is what the order carries.
    assert orders[0].quantity == Quantity(Decimal("20"))
    assert orders[0].price == Price(value=Decimal("50"))
