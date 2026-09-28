"""Lane 5e — the adoption surface in ``tradex_interfaces.safety``.

``ExecutionEngine.reconcile`` reports drift and stops there; lane 5e makes the
reconciliation *adopt* the venue's positions instead. That is a write to the
book on the strength of a venue read, so every test here is a negative control
for one of the five guards that make it safe:

* a failed read never adopts (the venue raised),
* a partial read never adopts (the venue answered with gaps),
* a stale read never adopts (a retry landed out of order),
* an unsettled fill is never adopted over (the book is mid-flight),
* an instrument the venue omitted is skipped, never read as flat.

plus the two structural properties: adoption is idempotent, and a dry run
changes nothing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain.execution import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    TimeInForce,
)
from tradex_domain.instruments import Equity, Instrument
from tradex_domain.value_objects import CorrelationId, Money, OrderId, Price, Quantity
from tradex_interfaces.safety import (
    METRIC_ADOPTED,
    METRIC_CLEARED,
    METRIC_READ_FAILED,
    METRIC_REFUSED,
    METRIC_SKIPPED,
    SKIP_UNREADABLE_INSTRUMENT,
    SKIP_UNREADABLE_QUANTITY,
    SKIP_UNSETTLED_FILL,
    SKIP_VENUE_OMITTED,
    AdoptionOutcome,
    ReconciliationMonitorSeams,
    VenueAdoption,
    VenueRead,
    adopt_venue_positions,
    build_reconciliation_monitor_seams,
    derive_venue_adoption,
    read_venue_positions,
    unsettled_local_instruments,
)

_RELIANCE = Equity.of("NSE", "RELIANCE")
_TCS = Equity.of("NSE", "TCS")
_INFY = Equity.of("NSE", "INFY")
_STAMP = datetime(2026, 7, 31, 9, 30, tzinfo=UTC)


def _position(
    instrument: Instrument,
    quantity: Decimal,
    *,
    avg_price: Decimal = Decimal("100"),
    strategy_id: str | None = "strat-a",
) -> Position:
    return Position(
        instrument=instrument,
        quantity=Quantity(value=quantity),
        avg_price=Price(value=avg_price),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
        mark_price=None,
        marked_at=None,
        mark_source=None,
        strategy_id=strategy_id,
    )


class _Broker:
    """Venue stub. ``get_positions`` returns rows; raising models a venue error."""

    def __init__(self, positions: list[Position] | None = None) -> None:
        self.positions = list(positions or [])
        self.calls = 0

    def get_positions(self) -> list[Position]:
        self.calls += 1
        return list(self.positions)


class _RaisingBroker(_Broker):
    def __init__(self, error: BaseException | None = None) -> None:
        super().__init__()
        self.error = error or RuntimeError("venue down")

    def get_positions(self) -> list[Position]:
        self.calls += 1
        raise self.error


class _Cache:
    """Position cache stub, recording every write keyed by instrument id.

    Mirrors :meth:`tradex_execution.trading_cache.TradingCache.set_position`,
    which takes the position and keys it itself — the adoption does not choose
    the key, the book does.
    """

    def __init__(self, positions: list[Position] | None = None) -> None:
        self._positions: dict[str, Position] = {
            str(p.instrument.instrument_id): p for p in (positions or [])
        }
        self.writes: list[Position] = []

    def all_positions(self) -> list[Position]:
        return list(self._positions.values())

    def set_position(self, position: Position) -> None:
        self.writes.append(position)
        self._positions[str(position.instrument.instrument_id)] = position

    def written_ids(self) -> list[str]:
        return [str(p.instrument.instrument_id) for p in self.writes]


def _order(instrument: Instrument, status: OrderStatus) -> Order:
    """A real :class:`Order`, so the unsettled guard is exercised against the
    domain type the engine actually holds — not a hand-rolled stand-in."""
    return Order(
        order_id=OrderId(value="order-1"),
        instrument=instrument,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("5")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=status,
        correlation_id=CorrelationId(value="corr-1"),
    )


def _metrics() -> _RecordingRegistry:
    return _RecordingRegistry()


class _RecordingRegistry:
    def __init__(self) -> None:
        self.counters: dict[str, list[tuple[float, dict[str, object]]]] = {}
        self.gauges: dict[str, list[float]] = {}

    def counter(self, name: str, help_text: str = "") -> _Counter:
        return _Counter(self.counters.setdefault(name, []))

    def gauge(self, name: str, help_text: str = "") -> _Gauge:
        return _Gauge(self.gauges.setdefault(name, []))

    def amount(self, name: str) -> float:
        return sum(a for a, _ in self.counters.get(name, []))


class _Counter:
    def __init__(self, sink: list[tuple[float, dict[str, object]]]) -> None:
        self._sink = sink

    def inc(self, amount: float = 1, **labels: object) -> None:
        self._sink.append((float(amount), dict(labels)))


class _Gauge:
    def __init__(self, sink: list[float]) -> None:
        self._sink = sink

    def set(self, value: float) -> None:
        self._sink.append(float(value))


# -- the plan is pure ----------------------------------------------------------


def test_derive_adoption_adopts_the_venues_quantity() -> None:
    local = [_position(_RELIANCE, Decimal("3"))]
    venue = [_position(_RELIANCE, Decimal("7"))]
    plan = derive_venue_adoption(local_positions=local, venue_positions=venue)
    assert set(plan.positions) == {"NSE:RELIANCE"}
    assert plan.positions["NSE:RELIANCE"].quantity.value == Decimal("7")
    assert plan.previous["NSE:RELIANCE"].quantity.value == Decimal("3")


def test_derive_adoption_retains_a_position_that_already_agrees() -> None:
    """An adoption is a correction, not a rewrite of an agreeing book."""
    local = [_position(_RELIANCE, Decimal("7"), avg_price=Decimal("100"))]
    venue = [_position(_RELIANCE, Decimal("7"), avg_price=Decimal("100"))]
    plan = derive_venue_adoption(local_positions=local, venue_positions=venue)
    assert plan.positions == {}
    assert plan.cleared == {}
    assert plan.skipped == {}
    assert set(plan.retained) == {"NSE:RELIANCE"}
    assert plan.is_empty


def test_derive_adoption_writes_a_position_the_book_does_not_hold() -> None:
    """A fill the book missed is exactly the drift adoption exists to fix."""
    venue = [_position(_TCS, Decimal("4"))]
    plan = derive_venue_adoption(local_positions=[], venue_positions=venue)
    assert set(plan.positions) == {"NSE:TCS"}
    assert plan.positions["NSE:TCS"].quantity.value == Decimal("4")


def test_derive_adoption_omitting_a_price_difference_still_adopts() -> None:
    """A price disagreement alone is a correction, not a skip."""
    local = [_position(_RELIANCE, Decimal("7"), avg_price=Decimal("100"))]
    venue = [_position(_RELIANCE, Decimal("7"), avg_price=Decimal("101"))]
    plan = derive_venue_adoption(local_positions=local, venue_positions=venue)
    assert set(plan.positions) == {"NSE:RELIANCE"}
    assert plan.positions["NSE:RELIANCE"].avg_price.value == Decimal("101")


def test_derive_adoption_tolerates_the_venues_own_price_convention() -> None:
    """A sub-tick price difference is agreement, not a rewrite."""
    local = [_position(_RELIANCE, Decimal("7"), avg_price=Decimal("100.00"))]
    venue = [_position(_RELIANCE, Decimal("7"), avg_price=Decimal("100.005"))]
    plan = derive_venue_adoption(local_positions=local, venue_positions=venue)
    assert plan.positions == {}
    assert set(plan.retained) == {"NSE:RELIANCE"}


def test_derive_adoption_adopts_a_price_difference_beyond_tolerance() -> None:
    """A price gap past the tolerance is a correction, not a rounding artefact."""
    local = [_position(_RELIANCE, Decimal("7"), avg_price=Decimal("100.00"))]
    venue = [_position(_RELIANCE, Decimal("7"), avg_price=Decimal("100.05"))]
    plan = derive_venue_adoption(local_positions=local, venue_positions=venue)
    assert set(plan.positions) == {"NSE:RELIANCE"}
    assert plan.positions["NSE:RELIANCE"].avg_price.value == Decimal("100.05")


def test_derive_adoption_clears_a_position_the_venue_reports_flat() -> None:
    local = [_position(_RELIANCE, Decimal("3"))]
    venue = [_position(_RELIANCE, Decimal("0"))]
    plan = derive_venue_adoption(local_positions=local, venue_positions=venue)
    assert set(plan.cleared) == {"NSE:RELIANCE"}
    assert plan.cleared["NSE:RELIANCE"].quantity.value == Decimal("0")
    assert plan.positions == {}
    # A clear is destructive, so the previous position is kept for the log.
    assert plan.previous["NSE:RELIANCE"].quantity.value == Decimal("3")


def test_derive_adoption_skips_a_flat_position_the_book_does_not_hold() -> None:
    """Flat on both sides is nothing to write — no skip, no churn."""
    venue = [_position(_TCS, Decimal("0"))]
    plan = derive_venue_adoption(local_positions=[], venue_positions=venue)
    assert plan.is_empty


def test_derive_adoption_omitting_an_instrument_skips_rather_than_clears() -> None:
    """The venue saying nothing is *not* the venue saying flat.

    This is the guard the whole design turns on: a gap read as a zero is the
    corruption lane 5e exists to prevent, so an omitted instrument is a skip and
    the caller that proved the read complete is the only one that gets a clear.
    """
    local = [_position(_RELIANCE, Decimal("3")), _position(_TCS, Decimal("2"))]
    venue = [_position(_RELIANCE, Decimal("3"))]
    plan = derive_venue_adoption(local_positions=local, venue_positions=venue)
    assert plan.positions == {}
    assert plan.cleared == {}
    assert plan.skipped == {"NSE:TCS": SKIP_VENUE_OMITTED}
    assert set(plan.retained) == {"NSE:RELIANCE"}


def test_derive_adoption_skips_a_quantity_it_cannot_read() -> None:
    local = [_position(_RELIANCE, Decimal("3"))]

    class _Unreadable:
        instrument = _RELIANCE
        quantity = None

    plan = derive_venue_adoption(local_positions=local, venue_positions=[_Unreadable()])
    assert plan.skipped == {"NSE:RELIANCE": SKIP_UNREADABLE_QUANTITY}


def test_derive_adoption_skips_an_unsettle_instrument_when_it_disagrees() -> None:
    """The book is mid-flight, so the venue's figure is the stale one."""
    local = [_position(_RELIANCE, Decimal("3"))]
    venue = [_position(_RELIANCE, Decimal("7"))]
    plan = derive_venue_adoption(
        local_positions=local,
        venue_positions=venue,
        unsettled_instruments={"NSE:RELIANCE"},
    )
    assert plan.positions == {}
    assert plan.cleared == {}
    assert plan.skipped == {"NSE:RELIANCE": SKIP_UNSETTLED_FILL}


def test_derive_adoption_adopts_a_settled_instrument_that_disagrees() -> None:
    """The guard is narrow: it does not protect instruments that have resolved."""
    local = [_position(_RELIANCE, Decimal("3"))]
    venue = [_position(_RELIANCE, Decimal("7"))]
    plan = derive_venue_adoption(
        local_positions=local,
        venue_positions=venue,
        unsettled_instruments={"NSE:TCS"},
    )
    assert set(plan.positions) == {"NSE:RELIANCE"}


def test_derive_adoption_tolerates_unsettle_read_as_None() -> None:
    local = [_position(_RELIANCE, Decimal("3"))]
    venue = [_position(_RELIANCE, Decimal("7"))]
    plan = derive_venue_adoption(
        local_positions=local, venue_positions=venue, unsettled_instruments=None,
    )
    assert set(plan.positions) == {"NSE:RELIANCE"}


def test_derive_adoption_skips_a_venue_quantity_it_cannot_read() -> None:
    """A venue row whose quantity is absent is unreadable, not zero."""
    local = [_position(_RELIANCE, Decimal("3"))]

    class _VenueNoQuantity:
        instrument = _RELIANCE
        quantity = None

    plan = derive_venue_adoption(local_positions=local, venue_positions=[_VenueNoQuantity()])
    assert plan.skipped == {"NSE:RELIANCE": SKIP_UNREADABLE_QUANTITY}


def test_derive_adoption_skips_a_local_quantity_it_cannot_read() -> None:
    """The local side is checked too — an unreadable book is not adopted over."""

    class _LocalNoQuantity:
        instrument = _RELIANCE
        quantity = None

    venue = [_position(_RELIANCE, Decimal("7"))]
    plan = derive_venue_adoption(
        local_positions=[_LocalNoQuantity()], venue_positions=venue,
    )
    assert plan.skipped == {"NSE:RELIANCE": SKIP_UNREADABLE_QUANTITY}


def test_derive_adoption_skips_a_venue_only_row_with_no_quantity() -> None:
    """The venue-only branch of the plan skips unreadable quantities as well."""

    class _VenueOnlyNoQuantity:
        instrument = _TCS
        quantity = None

    plan = derive_venue_adoption(
        local_positions=[], venue_positions=[_VenueOnlyNoQuantity()],
    )
    assert plan.skipped == {"NSE:TCS": SKIP_UNREADABLE_QUANTITY}


def test_derive_adoption_reports_a_venue_row_it_cannot_key() -> None:
    """A row with no instrument id is unadoptable, and is not silently dropped."""

    class _NoInstrument:
        instrument = None
        quantity = Decimal("5")

    plan = derive_venue_adoption(
        local_positions=[], venue_positions=[_NoInstrument()],
    )
    assert SKIP_UNREADABLE_INSTRUMENT in plan.skipped.values()


def test_derive_adoption_is_pure_and_touches_nothing() -> None:
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    before = list(cache.all_positions())
    derive_venue_adoption(
        local_positions=cache.all_positions(),
        venue_positions=[_position(_RELIANCE, Decimal("9"))],
    )
    assert cache.writes == []
    assert cache.all_positions() == before


# -- the read is sealed --------------------------------------------------------


def test_read_venue_positions_marks_a_raising_venue_failed() -> None:
    read = read_venue_positions(_RaisingBroker())
    assert read.failed
    assert not read.ok
    assert read.positions == ()
    assert read.read_at is None


def test_read_venue_positions_carries_the_exception_without_raising() -> None:
    read = read_venue_positions(_RaisingBroker(ValueError("boom")))
    assert read.failed
    assert isinstance(read.error, ValueError)
    assert read.failure == "venue_get_positions_failed"


def test_read_venue_positions_is_complete_for_a_full_answer() -> None:
    read = read_venue_positions(
        _Broker([_position(_RELIANCE, Decimal("3"))]),
        known_instruments=["NSE:RELIANCE"],
        now=_STAMP,
    )
    assert read.ok
    assert not read.failed
    assert not read.partial
    assert len(read.positions) == 1
    assert read.read_at == _STAMP


def test_read_venue_positions_is_partial_when_a_book_instrument_is_missing() -> None:
    """``known_instruments`` is what makes a silently-dropped half unreadable."""
    read = read_venue_positions(
        _Broker([_position(_RELIANCE, Decimal("3"))]),
        known_instruments=["NSE:RELIANCE", "NSE:TCS"],
    )
    assert not read.ok
    assert read.partial
    assert read.unreadable_instruments == ("NSE:TCS",)


def test_read_venue_positions_is_complete_without_known_instruments() -> None:
    """Without the book's own set, a gap is not detectable, and is not invented."""
    read = read_venue_positions(_Broker([_position(_RELIANCE, Decimal("3"))]))
    assert read.ok
    assert read.unreadable_instruments == ()


def test_read_venue_positions_is_partial_for_an_unkeyable_row() -> None:
    class _NoInstrument:
        instrument = None
        quantity = Decimal("5")

    read = read_venue_positions(_Broker([_NoInstrument()]))
    assert not read.ok
    assert read.partial


# -- adopt: the five guards ----------------------------------------------------


def test_adopt_applies_the_venues_quantity_to_the_book() -> None:
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    outcome = adopt_venue_positions(
        broker=_Broker([_position(_RELIANCE, Decimal("7"))]),
        local_positions=cache.all_positions(),
        cache=cache,
        known_instruments=["NSE:RELIANCE"],
    )
    assert outcome.applied
    assert outcome.refusal == ""
    assert cache.written_ids() == ["NSE:RELIANCE"]
    assert cache.all_positions()[0].quantity.value == Decimal("7")


def test_adopt_writes_a_position_the_book_missed() -> None:
    cache = _Cache()
    outcome = adopt_venue_positions(
        broker=_Broker([_position(_TCS, Decimal("4"))]),
        local_positions=cache.all_positions(),
        cache=cache,
        known_instruments=[],
    )
    assert outcome.applied
    assert cache.written_ids() == ["NSE:TCS"]
    assert cache.all_positions()[0].quantity.value == Decimal("4")


def test_adopt_a_failed_read_never_adopts() -> None:
    """A venue that raised hands back nothing to adopt from."""
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    before = list(cache.all_positions())
    outcome = adopt_venue_positions(
        broker=_RaisingBroker(),
        local_positions=cache.all_positions(),
        cache=cache,
        known_instruments=["NSE:RELIANCE"],
    )
    assert not outcome.applied
    assert outcome.refusal != ""
    assert outcome.read is not None and outcome.read.failed
    assert cache.writes == []
    assert cache.all_positions() == before


def test_adopt_a_partial_read_never_adopts() -> None:
    """A venue that answered with gaps is refused, not interpreted."""
    cache = _Cache([_position(_RELIANCE, Decimal("3")), _position(_TCS, Decimal("2"))])
    before = [p.quantity.value for p in cache.all_positions()]
    outcome = adopt_venue_positions(
        broker=_Broker([_position(_RELIANCE, Decimal("9"))]),
        local_positions=cache.all_positions(),
        cache=cache,
        known_instruments=["NSE:RELIANCE", "NSE:TCS"],
    )
    assert not outcome.applied
    assert outcome.refusal != ""
    assert outcome.read is not None and outcome.read.partial
    assert cache.writes == []
    assert [p.quantity.value for p in cache.all_positions()] == before


def test_adopt_a_stale_read_never_adopts() -> None:
    """A retry that lands out of order must not move the book backwards."""
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    now = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)
    stale = now - timedelta(minutes=5)
    outcome = adopt_venue_positions(
        broker=_Broker([_position(_RELIANCE, Decimal("9"))]),
        local_positions=cache.all_positions(),
        cache=cache,
        known_instruments=["NSE:RELIANCE"],
        last_read_at=now,
        now=stale,
    )
    assert not outcome.applied
    assert outcome.refusal == "venue_read_stale"
    assert cache.writes == []


def test_adopt_a_fresh_read_is_accepted() -> None:
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    now = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)
    outcome = adopt_venue_positions(
        broker=_Broker([_position(_RELIANCE, Decimal("9"))]),
        local_positions=cache.all_positions(),
        cache=cache,
        known_instruments=["NSE:RELIANCE"],
        last_read_at=now - timedelta(minutes=1),
        now=now,
    )
    assert outcome.applied
    assert cache.written_ids() == ["NSE:RELIANCE"]


def test_adopt_never_adopts_over_an_unsettle_fill() -> None:
    """The book has a fill the venue has not reflected; the venue is the stale one."""
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    outcome = adopt_venue_positions(
        broker=_Broker([_position(_RELIANCE, Decimal("7"))]),
        local_positions=cache.all_positions(),
        cache=cache,
        unsettled_instruments={"NSE:RELIANCE"},
        known_instruments=["NSE:RELIANCE"],
    )
    assert outcome.applied
    assert cache.writes == []
    assert outcome.adoption.skipped == {"NSE:RELIANCE": SKIP_UNSETTLED_FILL}


def test_adopt_skips_an_instrument_the_venue_omits_but_the_book_holds() -> None:
    """An instrument the book holds that the venue omits is a skip, never a clear.

    This is the guard the whole design turns on: a gap read as a zero is the
    corruption lane 5e exists to prevent.
    """
    cache = _Cache([_position(_RELIANCE, Decimal("3")), _position(_TCS, Decimal("2"))])
    outcome = adopt_venue_positions(
        broker=_Broker([_position(_RELIANCE, Decimal("3"))]),
        local_positions=[_position(_RELIANCE, Decimal("3"))],
        cache=cache,
        known_instruments=["NSE:RELIANCE", "NSE:TCS"],
    )
    assert not outcome.applied
    assert outcome.read is not None and outcome.read.partial
    assert cache.writes == []
    assert cache.all_positions()[0].quantity.value == Decimal("3")


def test_adopt_skips_a_disagreeing_instrument_the_venue_omitted() -> None:
    """The skip, reached from the plan rather than the read."""
    local = [_position(_RELIANCE, Decimal("3")), _position(_TCS, Decimal("2"))]
    plan = derive_venue_adoption(
        local_positions=local,
        venue_positions=[_position(_RELIANCE, Decimal("3"))],
    )
    assert plan.skipped == {"NSE:TCS": SKIP_VENUE_OMITTED}
    assert plan.cleared == {}
    assert plan.positions == {}


def test_adopt_does_not_clear_when_the_read_is_partial() -> None:
    """The destructive direction is the one a partial read must never reach."""
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    outcome = adopt_venue_positions(
        broker=_Broker([]),
        local_positions=cache.all_positions(),
        cache=cache,
        known_instruments=["NSE:RELIANCE", "NSE:TCS"],
    )
    assert not outcome.applied
    assert cache.all_positions()[0].quantity.value == Decimal("3")


# -- adopt: structural properties ---------------------------------------------


def test_adopt_is_idempotent() -> None:
    """Running it twice writes the same positions twice and no more.

    The venue read is re-read each time and the book now holds the adopted
    position, so the second plan is empty: it rewrites nothing the first did not.
    """
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    broker = _Broker([_position(_RELIANCE, Decimal("7"))])

    def adopt() -> AdoptionOutcome:
        return adopt_venue_positions(
            broker=broker,
            local_positions=cache.all_positions(),
            cache=cache,
            known_instruments=["NSE:RELIANCE"],
        )

    first = adopt()
    assert first.applied
    assert cache.written_ids() == ["NSE:RELIANCE"]
    second = adopt()
    assert second.applied
    assert second.adoption.is_empty
    assert cache.written_ids() == ["NSE:RELIANCE"]


def test_adopt_repeated_against_a_live_book_converges() -> None:
    """Against a book that is not updated, the adoption does not compound."""
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    broker = _Broker([_position(_RELIANCE, Decimal("7"))])
    args = dict(
        broker=broker,
        local_positions=[_position(_RELIANCE, Decimal("7"))],
        cache=cache,
        known_instruments=["NSE:RELIANCE"],
    )
    adopt_venue_positions(**args)
    second = adopt_venue_positions(**args)
    assert second.adoption.is_empty


def test_adopt_a_dry_run_changes_nothing_but_reports_everything() -> None:
    """``adopt=False`` is the "what would change" an operator inspects."""
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    outcome = adopt_venue_positions(
        broker=_Broker([_position(_RELIANCE, Decimal("7"))]),
        local_positions=cache.all_positions(),
        cache=cache,
        known_instruments=["NSE:RELIANCE"],
        adopt=False,
    )
    assert not outcome.applied
    assert cache.writes == []
    assert set(outcome.adoption.positions) == {"NSE:RELIANCE"}
    assert outcome.adoption.positions["NSE:RELIANCE"].quantity.value == Decimal("7")


def test_adopt_a_dry_run_of_a_failed_read_reports_no_plan() -> None:
    outcome = adopt_venue_positions(
        broker=_RaisingBroker(),
        local_positions=[_position(_RELIANCE, Decimal("3"))],
        cache=_Cache(),
        adopt=False,
    )
    assert not outcome.applied
    assert outcome.adoption.is_empty


def test_adopt_preserves_the_local_strategy_attribution() -> None:
    """The venue cannot attribute a position; the local book's attribution survives."""
    cache = _Cache([_position(_RELIANCE, Decimal("3"), strategy_id="strat-a")])
    outcome = adopt_venue_positions(
        broker=_Broker([_position(_RELIANCE, Decimal("7"), strategy_id=None)]),
        local_positions=cache.all_positions(),
        cache=cache,
        known_instruments=["NSE:RELIANCE"],
    )
    assert outcome.applied
    assert cache.all_positions()[0].strategy_id == "strat-a"


def test_adopt_drops_the_venues_mark_so_a_quote_re_marks_it() -> None:
    """A mark is a quote fact; the venue's position answer carries a stale one."""
    venue = replace(
        _position(_RELIANCE, Decimal("7")),
        mark_price=Price(value=Decimal("999")),
        marked_at=_STAMP,
        mark_source="venue",
    )
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    outcome = adopt_venue_positions(
        broker=_Broker([venue]),
        local_positions=cache.all_positions(),
        cache=cache,
        known_instruments=["NSE:RELIANCE"],
    )
    assert outcome.applied
    written = cache.all_positions()[0]
    assert written.mark_price is None
    assert written.marked_at is None
    assert written.mark_source is None


def test_adopt_is_explicit_and_does_not_run_on_a_None_broker() -> None:
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    outcome = adopt_venue_positions(
        broker=None,
        local_positions=cache.all_positions(),
        cache=cache,
    )
    assert not outcome.applied
    assert cache.writes == []


def test_adopt_reads_the_venue_exactly_once_per_call() -> None:
    broker = _Broker([_position(_RELIANCE, Decimal("7"))])
    adopt_venue_positions(
        broker=broker,
        local_positions=[_position(_RELIANCE, Decimal("3"))],
        cache=_Cache(),
        known_instruments=["NSE:RELIANCE"],
    )
    assert broker.calls == 1


# -- the unsettled set ---------------------------------------------------------


def test_unsettle_instruments_derives_from_in_flight_orders() -> None:
    orders = [_order(_RELIANCE, OrderStatus.PARTIALLY_FILLED)]
    assert unsettled_local_instruments(orders) == {"NSE:RELIANCE"}


def test_settled_orders_do_not_protect_their_instrument() -> None:
    orders = [_order(_RELIANCE, OrderStatus.FILLED)]
    assert unsettled_local_instruments(orders) == frozenset()


def test_a_cancelled_order_does_not_protect_its_instrument() -> None:
    """A cancelled order cannot fill, so its instrument is free to be corrected."""
    orders = [_order(_RELIANCE, OrderStatus.CANCELLED)]
    assert unsettled_local_instruments(orders) == frozenset()


def test_an_unknown_order_protects_its_instrument() -> None:
    """An order the venue may have accepted and not reported is provably absent
    from the venue answer."""
    orders = [_order(_RELIANCE, OrderStatus.UNKNOWN)]
    assert unsettled_local_instruments(orders) == {"NSE:RELIANCE"}


def test_unsettle_instruments_is_empty_for_no_orders() -> None:
    assert unsettled_local_instruments([]) == frozenset()
    assert unsettled_local_instruments(None) == frozenset()


def test_unsettle_instruments_tolerates_a_plain_string_status() -> None:
    class _StubOrder:
        instrument = _TCS
        status = "SUBMITTED"

    assert unsettled_local_instruments([_StubOrder()]) == {"NSE:TCS"}


# -- the seams bind a session --------------------------------------------------


def test_seams_return_two_callables() -> None:
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    seams = build_reconciliation_monitor_seams(
        broker=_Broker([_position(_RELIANCE, Decimal("7"))]),
        cache=cache,
    )
    assert isinstance(seams, ReconciliationMonitorSeams)
    assert callable(seams.reconcile)
    assert callable(seams.adopt)


def test_seams_adopt_writes_the_book() -> None:
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    seams = build_reconciliation_monitor_seams(
        broker=_Broker([_position(_RELIANCE, Decimal("7"))]),
        cache=cache,
    )
    outcome = seams.adopt()
    assert isinstance(outcome, AdoptionOutcome)
    assert outcome.applied
    assert cache.written_ids() == ["NSE:RELIANCE"]


def test_seams_reconcile_is_a_noop_without_an_engine() -> None:
    seams = build_reconciliation_monitor_seams(broker=_Broker(), cache=_Cache())
    assert seams.reconcile() == ()


def test_seams_reconcile_runs_the_engines_comparison() -> None:
    class _Engine:
        def __init__(self) -> None:
            self.calls = 0

        def reconcile(self) -> list[str]:
            self.calls += 1
            return ["drift"]

        def all_orders(self) -> list[object]:
            return []

    engine = _Engine()
    seams = build_reconciliation_monitor_seams(
        broker=_Broker(), cache=_Cache(), engine=engine,
    )
    assert seams.reconcile() == ("drift",)
    assert engine.calls == 1


def test_seams_advance_the_freshness_baseline_only_on_a_usable_read() -> None:
    """A failed read must not become the bar a later read is judged stale against."""
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    clock = _StubClock()
    seams = build_reconciliation_monitor_seams(
        broker=_Broker([_position(_RELIANCE, Decimal("7"))]),
        cache=cache,
        clock=clock.now,
    )
    first = seams.adopt()
    assert first.applied
    baseline = _read_at(first)
    assert baseline is not None
    later = clock.advance(timedelta(minutes=1))
    second = seams.adopt()
    assert second.applied
    assert _read_at(second) == later
    assert later > baseline


def test_seams_a_failed_read_does_not_set_the_baseline() -> None:
    clock = _StubClock()
    seams = build_reconciliation_monitor_seams(
        broker=_RaisingBroker(), cache=_Cache(), clock=clock.now,
    )
    outcome = seams.adopt()
    assert not outcome.applied
    assert _read_at(outcome) is None
    # A second read at the same stamp would be refused as stale if the failed
    # read had set the baseline; it is not, because the baseline stayed clear.
    clock.advance(timedelta(minutes=1))
    again = seams.adopt()
    assert not again.applied
    assert again.refusal != "venue_read_stale"


def test_seams_derive_the_unsettle_guard_from_the_engine_orders() -> None:
    class _Engine:
        def all_orders(self) -> list[Order]:
            return [_order(_RELIANCE, OrderStatus.ACK)]

    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    seams = build_reconciliation_monitor_seams(
        broker=_Broker([_position(_RELIANCE, Decimal("7"))]),
        cache=cache,
        engine=_Engine(),
    )
    outcome = seams.adopt()
    assert outcome.applied
    assert outcome.adoption.skipped == {"NSE:RELIANCE": SKIP_UNSETTLED_FILL}
    assert cache.writes == []


def test_seams_survive_an_engine_that_cannot_list_orders() -> None:
    class _Engine:
        def reconcile(self) -> list[object]:
            return []

    seams = build_reconciliation_monitor_seams(
        broker=_Broker([_position(_RELIANCE, Decimal("7"))]),
        cache=_Cache(),
        engine=_Engine(),
    )
    outcome = seams.adopt()
    assert outcome.applied


def test_seams_survive_a_cache_that_cannot_list_positions() -> None:
    """A cache with no ``all_positions`` yields no local book, so the read is
    measured against nothing and the adoption is a no-op that reports itself."""

    class _Empty:
        pass

    seams = build_reconciliation_monitor_seams(
        broker=_Broker(), cache=_Empty(),
    )
    assert seams.reconcile() == ()
    outcome = seams.adopt()
    assert isinstance(outcome, AdoptionOutcome)
    assert outcome.adoption.is_empty


def test_seams_a_dry_run_does_not_write() -> None:
    cache = _Cache([_position(_RELIANCE, Decimal("3"))])
    seams = build_reconciliation_monitor_seams(
        broker=_Broker([_position(_RELIANCE, Decimal("7"))]),
        cache=cache,
        adopt=False,
    )
    outcome = seams.adopt()
    assert not outcome.applied
    assert cache.writes == []


def test_seams_survive_a_raising_engine() -> None:
    class _Boom:
        def reconcile(self) -> list[object]:
            raise RuntimeError("engine gone")

        def all_orders(self) -> list[object]:
            raise RuntimeError("orders gone")

    seams = build_reconciliation_monitor_seams(
        broker=_Broker(), cache=_Cache(), engine=_Boom(),
    )
    assert seams.reconcile() == ()
    outcome = seams.adopt()
    assert isinstance(outcome, AdoptionOutcome)


class _StubClock:
    def __init__(self) -> None:
        self._at = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._at

    def advance(self, delta: timedelta) -> datetime:
        self._at = self._at + delta
        return self._at


def _read_at(outcome: AdoptionOutcome) -> datetime | None:
    read = outcome.read
    return None if read is None else read.read_at


# -- the metric names are a wire contract --------------------------------------


def test_metric_names_are_the_prometheus_contract() -> None:
    """Both modules declare these; ``deploy/prometheus-rules.yml`` reads them."""
    assert METRIC_ADOPTED == "venue_adoption_adopted_total"
    assert METRIC_CLEARED == "venue_adoption_cleared_total"
    assert METRIC_SKIPPED == "venue_adoption_skipped_total"
    assert METRIC_REFUSED == "venue_adoption_refused_total"
    assert METRIC_READ_FAILED == "venue_read_failed_total"


def test_metric_names_are_re_exported_by_the_runtime_monitor() -> None:
    """The runtime module cannot import it; it re-declares it, and this pins agreement."""
    from tradex_runtime.reconciliation_monitor import (
        _METRIC_ADOPTED,
        _METRIC_CLEARED,
        _METRIC_READ_FAILED,
        _METRIC_REFUSED,
        _METRIC_SKIPPED,
    )

    assert _METRIC_ADOPTED == METRIC_ADOPTED
    assert _METRIC_CLEARED == METRIC_CLEARED
    assert _METRIC_SKIPPED == METRIC_SKIPPED
    assert _METRIC_REFUSED == METRIC_REFUSED
    assert _METRIC_READ_FAILED == METRIC_READ_FAILED


# -- the outcome and plan report themselves ------------------------------------


def test_venue_adoption_counts_its_own_buckets() -> None:
    plan = VenueAdoption(
        positions={"NSE:RELIANCE": object()},
        retained={"NSE:TCS": object()},
        cleared={"NSE:INFY": object()},
        skipped={"NSE:HDFC": SKIP_VENUE_OMITTED},
        previous={"NSE:INFY": object()},
    )
    assert plan.adopted_count == 1
    assert plan.is_empty is False
    actions = {row["action"] for row in plan.as_log_rows()}
    # Only the instruments that changed: ``retained`` is the agreeing majority and
    # logging it would drown the signal.
    assert actions == {"adopted", "cleared", "skipped"}


def test_venue_adoption_reports_one_row_per_instrument_it_acted_on() -> None:
    plan = VenueAdoption(
        positions={"NSE:RELIANCE": object()},
        cleared={"NSE:INFY": object()},
        skipped={"NSE:HDFC": SKIP_VENUE_OMITTED},
    )
    iids = [row["instrument_id"] for row in plan.as_log_rows()]
    assert iids == ["NSE:RELIANCE", "NSE:INFY", "NSE:HDFC"]


def test_venue_adoption_is_empty_when_it_did_nothing() -> None:
    plan = VenueAdoption()
    assert plan.adopted_count == 0
    assert plan.is_empty
    assert plan.as_log_rows() == []


def test_venue_read_reports_itself() -> None:
    read = VenueRead(failed=True, failure="boom")
    assert read.as_log_dict()["failed"] is True
    complete = VenueRead(read_at=_STAMP)
    assert complete.ok
    assert complete.as_log_dict()["failed"] is False


def test_adoption_outcome_reports_its_own_adopted_count() -> None:
    outcome = AdoptionOutcome(
        adoption=VenueAdoption(positions={"NSE:RELIANCE": object()}),
        applied=True,
    )
    assert outcome.adopted_count == 1
