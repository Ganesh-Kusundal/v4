"""Reconciliation engine — compares local vs broker state, and adopts it.

Side-effect-free drift detection for positions, orders, and funds, plus the
lane-5e venue-position *adoption* (REF-12: the adoption logic moved here from
``tradex_interfaces.safety`` so the HTTP edge keeps only the timer trigger and
the logging — the mutation belongs to the layer that owns the book).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from tradex_domain.constants import ADOPTION_TOLERANCE
from tradex_domain.execution import Account, Order, Position
from tradex_observability.metrics import (  # noqa: F401 — REF-13: canonical metric-name home is tradex_observability.metrics; re-exported here so interfaces (which may not import observability) can consume them legally
    METRIC_ADOPTED,
    METRIC_CLEARED,
    METRIC_LAST_ADOPTED,
    METRIC_READ_FAILED,
    METRIC_REFUSED,
    METRIC_SKIPPED,
)

log = logging.getLogger(__name__)


class DriftSeverity(StrEnum):
    """Severity levels for reconciliation drift."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class DriftItem:
    """A single discrepancy between local and broker state.

    For position-level drift (from ``reconcile``), ``symbol`` /
    ``local_quantity`` / ``broker_quantity`` / ``diff`` are populated.
    For order / funds drift (from ``compare_orders`` / ``compare_funds``),
    ``kind`` / ``key`` / ``severity`` / ``reason`` / ``local`` / ``remote``
    are populated instead.
    """

    # Position-level fields (used by reconcile())
    symbol: str = ""
    local_quantity: Decimal = Decimal("0")
    broker_quantity: Decimal = Decimal("0")
    diff: Decimal = Decimal("0")
    # Extended fields (used by compare_orders / compare_funds)
    kind: str = "position"
    key: str = ""
    severity: DriftSeverity = DriftSeverity.LOW
    reason: str = ""
    local: Any = None
    remote: Any = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _order_key(order: Order) -> str:
    return order.order_id.value


class ReconciliationEngine:
    """Reconciles local state against broker snapshots.

    Produces DriftItems for any discrepancy in positions, orders, or funds.
    """

    def reconcile(
        self,
        local: list[Position],
        broker: list[Position],
    ) -> list[DriftItem]:
        """Compare local and broker positions, returning drift items.

        Positions are keyed by full instrument_id (exchange + underlying +
        derivative fields) so the same symbol on different exchanges
        (e.g. RELIANCE on NSE vs BSE) does not collide (M7 fix).
        """
        local_map: dict[str, Position] = {
            str(p.instrument.instrument_id): p for p in local
        }
        broker_map: dict[str, Position] = {
            str(p.instrument.instrument_id): p for p in broker
        }

        all_keys = set(local_map) | set(broker_map)
        drifts: list[DriftItem] = []

        for inst_key in sorted(all_keys):
            lp = local_map.get(inst_key)
            bp = broker_map.get(inst_key)
            local_qty = lp.quantity.value if lp else Decimal("0")
            broker_qty = bp.quantity.value if bp else Decimal("0")
            diff = local_qty - broker_qty
            if diff != Decimal("0"):
                drifts.append(
                    DriftItem(
                        symbol=inst_key,
                        local_quantity=local_qty,
                        broker_quantity=broker_qty,
                        diff=diff,
                    )
                )
            # Also detect avg_price drift when quantities match
            if (
                lp is not None
                and bp is not None
                and local_qty == broker_qty
                and local_qty != Decimal("0")
            ):
                price_diff = abs(
                    lp.avg_price.value - bp.avg_price.value
                )
                if price_diff > Decimal("0.01"):
                    drifts.append(
                        DriftItem(
                            kind="position",
                            key=inst_key,
                            severity=DriftSeverity.MEDIUM,
                            reason="avg_price drift",
                            local=lp.avg_price.value,
                            remote=bp.avg_price.value,
                        )
                    )

        return drifts

    # -- v3-ported comparison methods --------------------------------------

    def compare_orders(
        self,
        local: list[Order],
        broker: list[Order],
        *,
        price_tolerance: Decimal = Decimal("0.01"),
    ) -> list[DriftItem]:
        """Compare local vs broker orders and return drift items."""
        drifts: list[DriftItem] = []
        local_by = {_order_key(o): o for o in local}
        broker_by = {_order_key(o): o for o in broker}

        for key, bo in broker_by.items():
            lo = local_by.get(key)
            if lo is None:
                drifts.append(
                    DriftItem(
                        kind="order",
                        key=key,
                        severity=DriftSeverity.HIGH,
                        reason="missing local order",
                        local=None,
                        remote=bo,
                    )
                )
                continue
            drifts.extend(
                self._diff_order(lo, bo, key, price_tolerance),
            )

        for key, lo in local_by.items():
            if key not in broker_by:
                drifts.append(
                    DriftItem(
                        kind="order",
                        key=key,
                        severity=DriftSeverity.HIGH,
                        reason="missing broker order",
                        local=lo,
                        remote=None,
                    )
                )
        return drifts

    def compare_funds(
        self,
        local: Account,
        broker: Account,
        *,
        money_tolerance: Decimal = Decimal("0.01"),
    ) -> list[DriftItem]:
        """Compare local vs broker account funds and return drift items."""
        drifts: list[DriftItem] = []
        if (
            local.balance is not None
            and broker.balance is not None
            and abs(local.balance.amount - broker.balance.amount) > money_tolerance
        ):
            drifts.append(
                DriftItem(
                    kind="funds",
                    key=local.account_id.value,
                    severity=DriftSeverity.HIGH,
                    reason="balance mismatch",
                    local=local,
                    remote=broker,
                )
            )
        elif (
            local.equity is not None
            and broker.equity is not None
            and abs(local.equity.amount - broker.equity.amount) > money_tolerance
        ):
            drifts.append(
                DriftItem(
                    kind="funds",
                    key=local.account_id.value,
                    severity=DriftSeverity.MEDIUM,
                    reason="equity drift",
                    local=local,
                    remote=broker,
                )
            )
        return drifts

    # -- internal helpers --------------------------------------------------

    @staticmethod
    def _diff_order(
        local: Order,
        broker: Order,
        key: str,
        price_tolerance: Decimal,
    ) -> list[DriftItem]:
        out: list[DriftItem] = []
        if local.quantity.value != broker.quantity.value:
            out.append(
                DriftItem(
                    kind="order",
                    key=key,
                    severity=DriftSeverity.HIGH,
                    reason="quantity mismatch",
                    local=local,
                    remote=broker,
                )
            )
            return out
        if local.filled_quantity.value != broker.filled_quantity.value:
            out.append(
                DriftItem(
                    kind="order",
                    key=key,
                    severity=DriftSeverity.HIGH,
                    reason="filled_quantity mismatch",
                    local=local,
                    remote=broker,
                )
            )
            return out
        lp = local.price.value if local.price else None
        bp = broker.price.value if broker.price else None
        if lp is not None and bp is not None and abs(lp - bp) > price_tolerance:
            out.append(
                DriftItem(
                    kind="order",
                    key=key,
                    severity=DriftSeverity.MEDIUM,
                    reason="price drift",
                    local=local,
                    remote=broker,
                )
            )
        elif local.status is not broker.status:
            out.append(
                DriftItem(
                    kind="order",
                    key=key,
                    severity=DriftSeverity.LOW,
                    reason="status lag",
                    local=local,
                    remote=broker,
                )
            )
        return out


# ---------------------------------------------------------------------------
# Venue position adoption (lane 5e) — REF-12: moved here from
# tradex_interfaces.safety. The compare above reports drift; this half is the
# mutation that closes it. It is layered below the HTTP edge on purpose:
# ``tradex_interfaces.safety`` re-exports these names for its surface and its
# tests, and :meth:`ExecutionEngine.adopt_venue_positions` is the public entry.
# ---------------------------------------------------------------------------


#: The ways an instrument can be skipped, named once so the counter labels and the
#: tests agree. Every one is a refusal to adopt, and each is a different reason an
#: operator needs to see before clearing drift manually.
SKIP_UNREADABLE_POSITION = "unreadable_position"
SKIP_UNREADABLE_QUANTITY = "unreadable_quantity"
SKIP_UNREADABLE_INSTRUMENT = "unreadable_instrument"
SKIP_UNSETTLED_FILL = "unsettled_fill"
#: The placeholder for a venue row whose instrument could not be read. A row that
#: cannot be keyed cannot be counted by name, but it *can* be counted, and a
#: partial read that reports zero unreadable instruments would read as complete.
_UNREADABLE_INSTRUMENT = "<unreadable:instrument>"
#: The venue's complete read named no row for an instrument the local book holds.
#: That *is* the venue saying flat — but only if the read really was complete, and
#: proving it is the caller's job (``known_instruments`` in
#: :func:`read_venue_positions`). A caller that cannot prove completeness gets the
#: skip rather than the clear, because clearing is the destructive direction and a
#: gap reading as "flat" is the corruption lane 5e is written against.
SKIP_VENUE_OMITTED = "venue_omitted"

#: Refusal reasons that are about *this* session's own guard rather than the
#: venue's read. The venue read's reasons live on :class:`VenueRead`; these
#: name a refusal that happens before or instead of it, and they are counted in
#: ``venue_adoption_refused_total{reason=...}`` like every other refusal.
#:
#: ``unsettled_guard_unavailable`` is the fail-closed answer to an order book
#: that cannot be read (P0-F2). "I could not tell what is in flight" is not
#: "nothing is in flight": adopting the venue's figures with the protection
#: silently off is the corruption lane 5e is written against.
REFUSAL_UNSETTLED_GUARD_UNAVAILABLE = "unsettled_guard_unavailable"

#: Order statuses whose fills the venue's snapshot cannot yet have seen.
_UNSETTLED_ORDER_STATUSES: frozenset[str] = frozenset(
    {
        "NEW",
        "PENDING",
        "ACK",
        "SUBMITTED",
        "PARTIALLY_FILLED",
        "UNKNOWN",
    }
)


def unsettled_local_instruments(orders: Sequence[Any]) -> frozenset[str]:
    """Instrument ids the local book believes are mid-flight, so must not be adopted over.

    The local book's own in-flight work is *newer* than the venue's snapshot in the
    one way that matters: a fill that crossed the venue boundary and is not yet on
    the venue's ``get_positions`` answer is destroyed by an adoption that treats the
    venue's figure as authoritative. The venue lags, and the local book is right.
    Adopting over it would not correct the book, it would corrupt it — and silently:
    the adoption logs a "correction" while making the book wrong, which is the exact
    failure mode lane 5e names.

    The check is deliberately conservative and deliberately narrow:

    * ``UNKNOWN`` — an order the venue may have accepted and not yet reported. Its
      fill is provably absent from the venue answer, so the instrument is protected
      for as long as the uncertainty stands.
    * ``NEW``/``PENDING``/``ACK``/``SUBMITTED``/``PARTIALLY_FILLED`` — may complete
      at any instant, and the venue answer is a snapshot from before it did. A
      partially filled order's *resting* leg can still fill after the snapshot, so
      the venue's quantity for it is already stale.

    It is narrow on purpose. It does not protect every instrument that ever carried
    an order, because that would refuse to adopt at all, and refusing to adopt is
    the status quo lane 5e exists to end. The protection covers the instruments the
    local book actively believes unresolved, and lifts the moment they resolve.
    """
    unsettled: set[str] = set()
    for order in orders or ():
        status = getattr(order, "status", None)
        value = str(getattr(status, "value", status) or "")
        if value in _UNSETTLED_ORDER_STATUSES:
            iid = _position_instrument_id(getattr(order, "instrument", None))
            if iid is not None:
                unsettled.add(iid)
    return frozenset(unsettled)


#: The adoption corrects *figures*, so a difference below this is not a disagreement
#: worth writing: it is rounding in the venue's own quantization. Keeping the book
#: untouched for those keeps the adoption log to the corrections an operator needs
#: to see, and is what makes a re-derivation after a successful adoption report no
#: change (idempotency) rather than a permanent paise-level churn.
_ADOPTION_TOLERANCE: Decimal = ADOPTION_TOLERANCE


@dataclass(frozen=True, slots=True)
class VenueAdoption:
    """The local book after the venue's positions were *adopted*, not merely read.

    Lane 5e: reconciliation used to report drift and stop there. A strategy that
    filled at the venue while the local book lagged stayed wrong forever, because
    nothing ever wrote the venue's answer into the book. This is the record of one
    adoption: the positions written, the ones untouched, and the reasons nothing
    was written where nothing was.

    The record is *complete*, not best-effort: :attr:`positions` plus
    :attr:`retained` plus :attr:`cleared` plus :attr:`skipped` accounts for every
    instrument on either side of the comparison, so a caller can prove nothing was
    silently dropped. A gap in that accounting is the exact corruption this object
    exists to make impossible.

    The venue's figures are carried on the adopted :class:`Position` objects as
    plain domain values; the local figures they replaced are in :attr:`previous`
    so a caller (and a log line) can say what changed, not just that it did.
    """

    #: Adopted venue positions, keyed by instrument id, written to the book.
    positions: dict[str, Any] = field(default_factory=dict)
    #: Local positions the venue also reported, whose figures already agreed.
    #: Untouched deliberately: an adoption is a correction, not a rewrite.
    retained: dict[str, Any] = field(default_factory=dict)
    #: Local positions the venue's complete read reported as flat. Written as a
    #: zero-quantity position rather than deleted, because the book is keyed by
    #: instrument and a strategy that next reads the position must find one.
    cleared: dict[str, Any] = field(default_factory=dict)
    #: Instrument ids the adoption did NOT write, with the reason. The reason is
    #: a machine-readable short string, not a free sentence: it is counted in
    #: ``venue_adoption_skipped_total{reason=...}`` and a sentence there would be
    #: an unbounded label set.
    skipped: dict[str, str] = field(default_factory=dict)
    #: The local positions that were replaced, keyed by instrument id, so the log
    #: and the caller can state what changed rather than merely that it did.
    previous: dict[str, Any] = field(default_factory=dict)

    @property
    def adopted_count(self) -> int:
        """Instruments whose position the adoption actually wrote."""
        return len(self.positions)

    @property
    def is_empty(self) -> bool:
        """An adoption that changed nothing, including one that was refused."""
        return not (
            self.positions or self.cleared or self.skipped
        )

    def as_log_rows(self) -> list[dict[str, Any]]:
        """One machine-readable row per instrument the adoption acted on.

        Only the instruments that *changed* appear: :attr:`retained` is the
        already-agreeing majority and logging it at INFO on every adoption would
        drown the signal in noise.
        """
        rows: list[dict[str, Any]] = []
        for iid, position in self.positions.items():
            previous = self.previous.get(iid)
            rows.append(
                {
                    "instrument_id": iid,
                    "action": "adopted",
                    "previous_quantity": _quantity_of(previous),
                    "venue_quantity": _quantity_of(position),
                    "venue_avg_price": _avg_price_of(position),
                    "strategy_id": _strategy_id_of(previous),
                }
            )
        for iid, position in self.cleared.items():
            previous = self.previous.get(iid)
            rows.append(
                {
                    "instrument_id": iid,
                    "action": "cleared",
                    "previous_quantity": _quantity_of(previous),
                    "venue_quantity": _quantity_of(position),
                    "venue_avg_price": _avg_price_of(position),
                    "strategy_id": _strategy_id_of(previous),
                }
            )
        for iid, reason in self.skipped.items():
            rows.append(
                {
                    "instrument_id": iid,
                    "action": "skipped",
                    "reason": reason,
                    "previous_quantity": _quantity_of(self.previous.get(iid)),
                }
            )
        return rows


def _attr(position: Any, name: str) -> Any:
    """A field of *position*, attribute or mapping key, or ``None" when unreadable.

    A broker adapter that hands back a row or a mapping instead of a
    :class:`Position` must not break the comparison before it starts, and
    ``getattr`` on a mapping *raises* rather than returning a default. ``None`` is
    the signal for "absent", never an exception: the read's contract is that it
    never raises, and a caller treats ``None`` as unadoptable rather than guessing.
    """
    if isinstance(position, Mapping):
        return position.get(name)
    try:
        return getattr(position, name, None)
    except Exception:  # noqa: BLE001 - an exotic object, not a position
        return None


def _quantity_of(position: Any) -> Any:
    """A position's quantity as a plain Decimal, or ``None`` when absent."""
    if position is None:
        return None
    quantity = _attr(position, "quantity")
    return getattr(quantity, "value", quantity)


def _avg_price_of(position: Any) -> Any:
    """A position's average price as a plain Decimal, or ``None`` when absent."""
    if position is None:
        return None
    price = _attr(position, "avg_price")
    return getattr(price, "value", price)


def _strategy_id_of(position: Any) -> str | None:
    """The strategy that opened *position*, or ``None`` when it is unknown."""
    if position is None:
        return None
    return _attr(position, "strategy_id")


def _position_instrument_id(position: Any) -> str | None:
    """The instrument id a position is keyed by, or ``None`` when unreadable.

    The venue's objects are not ours: a broker adapter that hands back a mapping or
    a row instead of a :class:`Position` must not break the comparison before it
    starts. ``None`` is the signal for "not a position" rather than an exception,
    and the caller treats it as unadoptable rather than inventing a key.
    """
    if position is None:
        return None
    instrument = _attr(position, "instrument")
    if instrument is not None:
        return str(getattr(instrument, "instrument_id", instrument))
    key = _attr(position, "instrument_id")
    if key is None:
        return None
    return str(key)


#: Public spelling (REF-14 guardrail): the interfaces safety surface consumes
#: this cross-package, so it must not be reachable only under the
#: underscore-private name.
position_instrument_id = _position_instrument_id


def _venue_quantity(position: Any) -> Decimal | None:
    """The venue's quantity for *position* as a Decimal, or ``None`` when absent."""
    quantity = _attr(position, "quantity")
    value = getattr(quantity, "value", quantity)
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None


def _agrees_on_price(local: Any, venue: Any) -> bool:
    """Whether two agreeing-quantity positions also agree on average price."""
    local_price = _avg_price_of(local)
    venue_price = _avg_price_of(venue)
    if local_price is None or venue_price is None:
        return True
    return abs(Decimal(str(local_price)) - Decimal(str(venue_price))) <= _ADOPTION_TOLERANCE


def _flat_position(reference: Any) -> Any:
    """A flat position on *reference*'s instrument, carrying its attribution.

    Constructed from the domain value objects so the cleared row is a real
    :class:`Position` the cache can store, not a hand-rolled mapping.
    """
    from tradex_domain.execution import Position
    from tradex_domain.value_objects import Money, Price, Quantity

    instrument = _attr(reference, "instrument")
    if instrument is None:
        return None
    currency = getattr(instrument, "currency", "INR")
    return Position(
        instrument=instrument,
        quantity=Quantity(value=Decimal("0")),
        avg_price=Price(value=Decimal("0")),
        realized_pnl=Money(amount=Decimal("0"), currency=currency),
        unrealized_pnl=Money(amount=Decimal("0"), currency=currency),
        mark_price=None,
        marked_at=None,
        mark_source=None,
        strategy_id=getattr(reference, "strategy_id", None),
    )


def _cleared_position(local: Any, venue: Any) -> Any:
    """The flat position written when the venue reports an instrument the book held."""
    cleared = _flat_position(local)
    if cleared is not None:
        return cleared
    # The local position carried no instrument; fall back to the venue's.
    return _flat_position(venue)


def _adopted_position(local: Any, venue: Any) -> Any:
    """The position the adoption writes: the venue's *figures of record*, on a
    real :class:`Position` built for this book.

    The venue's quantity is authoritative — it is what the broker says is held —
    and so is its average price, because a venue price differs from the local
    one for real reasons (VWAP vs last, its own quantization) and the local
    basis is derived from our own fills, not the venue's view of them.

    Three things are deliberately *not* taken from the venue (P0-Q1), because
    they are ours and the venue cannot see them:

    * :attr:`Position.realized_pnl` — the local ledger of fees (booked by
      :meth:`PositionAccountant.on_fee`) and round-trip closes (booked by
      ``apply_fill``). Venue position payloads carry no per-position realized
      figure, and the ones that do often report ``0``, which is
      indistinguishable from reporting nothing. The old code wrote that 0 and
      destroyed the local ledger, so total P&L jumped by exactly the fees
      already booked, on every adopted instrument, with nothing logged. The
      venue's figure wins only when it actually reports one, and then the
      difference is reported rather than silently absorbed.
    * :attr:`Position.avg_price` — quantized with ``q2``, the same paisa grid
      every other booked average uses, so the adopted basis is not off-grid for
      the next fill's ``q2(total_cost/qty)`` to snap back across the adoption
      tolerance (the ping-pong the quant review called Cycle 2).
    * :attr:`Position.mark_price` — a mark is a *quote* fact, refreshed by the
      quote-driven marking service; the venue carries either no mark or a stale
      one that would then be believed fresh until the next quote. It is dropped
      so the next quote re-marks the position.
    """
    if venue is None:
        return None
    instrument = _attr(venue, "instrument")
    if instrument is None:
        return None
    currency = getattr(instrument, "currency", "INR")
    try:
        from tradex_domain.execution import Position
        from tradex_domain.utils import q2
        from tradex_domain.value_objects import Money, Price, Quantity

        return Position(
            instrument=instrument,
            quantity=Quantity(value=_venue_quantity(venue) or Decimal("0")),
            avg_price=Price(value=q2(_venue_avg_price_value(venue, currency))),
            realized_pnl=_adopted_realized(local, venue, currency),
            unrealized_pnl=Money(amount=Decimal("0"), currency=currency),
            mark_price=None,
            marked_at=None,
            mark_source=None,
            # The venue cannot attribute a position to a strategy: it does not know
            # the strategy id. The local book's attribution is preserved when it
            # exists, so strategy P&L books do not silently lose a position.
            strategy_id=(getattr(local, "strategy_id", None) if local is not None else None),
        )
    except Exception:  # noqa: BLE001 - a venue object that is not a Position
        return None


def _adopted_realized(local: Any, venue: Any, currency: str) -> Any:
    """The realized P&L the adopted position carries, and why (P0-Q1).

    The local ledger is authoritative: it is built from our own fills, our own
    fee schedule and one rounding convention, and ``trade_metrics`` derives
    from that same fill stream. So the local figure is kept whenever the venue
    does not report a usable one — and "reports 0" counts as unusable, because
    a broker that always serialises realized_pnl=0 is indistinguishable from
    one that reports nothing, and honouring it would zero a genuine ledger on
    the first poll.

    When the venue does report a figure the two ledgers are different
    measurements (different fee models, different quantization, possibly a
    different scope), so the local one stays and the venue's is reported as
    drift rather than silently absorbing the difference into a P&L number
    nobody can audit.
    """
    from tradex_domain.value_objects import Money

    local_realized = _attr(local, "realized_pnl") if local is not None else None
    local_amount = getattr(local_realized, "amount", None)
    if local_amount is not None:
        try:
            return Money(
                amount=Decimal(str(local_amount)),
                currency=getattr(
                    local_realized, "currency", currency,
                ),
            )
        except (ArithmeticError, TypeError, ValueError):
            pass
    return _venue_realized(venue, currency)


def _venue_avg_price_value(venue: Any, currency: str) -> Decimal:
    """The venue's average price as a Decimal, ``0`` when the venue omits it."""
    price = _avg_price_of(venue)
    if price is None:
        return Decimal("0")
    try:
        return Decimal(str(price))
    except (ArithmeticError, TypeError, ValueError):
        return Decimal("0")


def _venue_realized(venue: Any, currency: str) -> Any:
    """The venue's realized P&L as ``Money``, zero when the venue omits it."""
    realized = _attr(venue, "realized_pnl")
    amount = getattr(realized, "amount", None)
    if amount is None:
        amount = Decimal("0")
    try:
        from tradex_domain.value_objects import Money

        return Money(amount=Decimal(str(amount)), currency=currency)
    except (ArithmeticError, TypeError, ValueError):
        from tradex_domain.value_objects import Money

        return Money(amount=Decimal("0"), currency=currency)


@dataclass(frozen=True, slots=True)
class VenueRead:
    """A venue positions read, sealed: either complete or failed, never partial.

    This is the type that makes "a failed read does not cause adoption" a
    structural property rather than a rule to remember. ``get_positions()`` has
    three outcomes — a full list, a partial list, or an exception — and only the
    first is usable:

    * **Failure** (:attr:`failed` True) carries no positions at all. The read
      produced nothing usable, so adopting from it is not expressible.
    * **Partial** (:attr:`partial` True) returned *some* positions and the venue
      also named instruments it could not read. Adopting a partial answer would
      treat the unread instruments as flat, which is the corruption lane 5e is
      written against, so the read is refused and the instruments are reported.
    * **Complete** is the only state an adoption may draw on.

    The builder (:func:`read_venue_positions`) is the only constructor callers use,
    so a caller cannot assemble a "complete" read from a failed one.
    """

    positions: tuple[Any, ...] = ()
    #: Instrument ids the venue reported as unreadable. Empty for a clean read.
    unreadable_instruments: tuple[str, ...] = ()
    failed: bool = False
    #: What failed, when something did. A short machine-readable string.
    failure: str = ""
    #: The exception the venue raised, for a log's ``exc_info``. Never surfaced.
    error: BaseException | None = None
    #: When the read completed, or ``None`` if it never did. An adoption refuses a
    #: read whose stamp is older than the caller's own last successful read.
    read_at: datetime | None = None

    @property
    def ok(self) -> bool:
        """The read is usable: it did not fail and it is not partial."""
        return not self.failed and not self.partial

    @property
    def partial(self) -> bool:
        """The venue returned some positions and could not read others."""
        return bool(self.unreadable_instruments) and not self.failed

    @property
    def refused(self) -> str:
        """The reason an adoption would refuse this read, or ``""`` when usable."""
        if self.failed:
            return self.failure or "venue_read_failed"
        if self.partial:
            return "venue_read_partial"
        return ""

    def as_log_dict(self) -> dict[str, Any]:
        """The read's verdict for a log line, with no exception object in it."""
        return {
            "ok": self.ok,
            "failed": self.failed,
            "partial": self.partial,
            "failure": self.failure or None,
            "positions": len(self.positions),
            "unreadable_instruments": sorted(self.unreadable_instruments),
            "refused": self.refused or None,
        }


def read_venue_positions(
    broker: Any,
    *,
    known_instruments: Sequence[str] | None = None,
    now: datetime | None = None,
) -> VenueRead:
    """Read the venue's positions once, sealing the outcome as complete or failed.

    Never raises. The three outcomes a venue read can have are the three states of
    :class:`VenueRead`, and an exception is converted to a *failed* read rather
    than propagated: a monitor that dies on a venue error is a monitor that stopped
    reconciling, which is the opposite of the safe direction.

    **Partial reads.** A venue adapter that returns positions for some instruments
    and reports others unreadable has handed back a partial book. This is detected
    two ways and refused either way:

    * the answer contains positions whose instrument is unreadable (counted in
      :attr:`VenueRead.unreadable_instruments`), or
    * the caller named ``known_instruments`` — the instruments the book holds — and
      the venue's answer names a strict subset. ``known_instruments`` is what makes
      the second detection possible: without it a venue that silently dropped half
      the book would read as complete, and an adoption would flatten the half it
      dropped.
    """

    try:
        positions = list(broker.get_positions())
    except Exception as exc:  # noqa: BLE001 - monitoring must not crash
        return VenueRead(
            failed=True,
            failure="venue_get_positions_failed",
            error=exc,
        )
    stamp = now or datetime.now(UTC)

    unreadable = [
        iid
        for iid in (_position_instrument_id(p) for p in positions)
        if iid is None
    ]
    if unreadable:
        # An unreadable row inside a *successful* call is the partial case: the
        # venue answered with gaps rather than raising.
        return VenueRead(
            positions=tuple(positions),
            unreadable_instruments=tuple([_UNREADABLE_INSTRUMENT] * len(unreadable)),
            read_at=stamp,
        )
    if known_instruments is not None:
        seen = {_position_instrument_id(p) for p in positions}
        missing = sorted(
            iid for iid in set(known_instruments) if iid not in seen
        )
        if missing:
            # The book holds instruments the venue's answer does not name. The
            # read is partial and must not be adopted as if the missing ones were
            # flat.
            return VenueRead(
                positions=tuple(positions),
                unreadable_instruments=tuple(missing),
                read_at=stamp,
            )
    return VenueRead(positions=tuple(positions), read_at=stamp)


@dataclass(frozen=True, slots=True)
class AdoptionOutcome:
    """The result of one adoption attempt: the plan, and whether it was applied.

    An adoption is refused far more often than it is applied, and the refusal is
    the load-bearing behaviour, so the outcome carries the *reason* the read was
    refused (:attr:`refusal`) alongside the plan an operator can inspect.
    """

    adoption: VenueAdoption
    #: ``True`` when the plan was written to the book. ``False`` is a refusal.
    applied: bool
    #: Why a refused adoption was refused. Empty when applied.
    refusal: str = ""
    #: The venue read the adoption was derived from, for the log.
    read: VenueRead | None = None

    @property
    def adopted_count(self) -> int:
        return self.adoption.adopted_count


def adopt_venue_positions(
    *,
    broker: Any,
    local_positions: Sequence[Any],
    cache: Any,
    unsettled_instruments: frozenset[str] | set[str] | None = None,
    known_instruments: Sequence[str] | None = None,
    last_read_at: datetime | None = None,
    now: datetime | None = None,
    adopt: bool = True,
    read: VenueRead | None = None,
) -> AdoptionOutcome:
    """Adopt the venue's positions into the book, or refuse with a reason.

    This is lane 5e's mutation. It is:

    * **Explicit** — a caller has to ask; nothing reconciles into the book on its
      own. ``adopt=False`` derives the plan and applies nothing, so the safety
      surface and a dry-run can show an operator what *would* change.
    * **Conditioned on a complete venue read** — :func:`read_venue_positions`
      refuses a failed or partial read before any position is touched. This is the
      structural guarantee that a bad read cannot corrupt a good book.
    * **Idempotent** — the plan's :attr:`VenueAdoption.retained` set is the
      already-agreeing majority, and re-deriving after a completed adoption
      produces an empty plan. Running it twice writes the same positions twice and
      no more, so a retry after a partial failure converges rather than compounds.
    * **Logged with what it changed** — every instrument the plan acted on is in
      :meth:`VenueAdoption.as_log_rows`, and the refusal is logged at WARNING
      rather than swallowed.

    The stale-read case is the last guard, and it is what makes a *retried* read
    safe: ``last_read_at`` is the caller's previous successful read, and a read
    that arrives *older* than it is refused, because a retry that lands out of
    order would overwrite a fresher book with a staler one.
    """
    read = read if read is not None else read_venue_positions(
        broker, known_instruments=known_instruments, now=now,
    )
    refusal = read.refused
    if refusal == "" and last_read_at is not None and read.read_at is not None:
        if read.read_at < last_read_at:
            # A read older than the one already applied is a retry of a stale
            # snapshot. Adopting it would move the book backwards.
            refusal = "venue_read_stale"
    if refusal:
        log.warning(
            "venue position adoption refused: %s (read=%s)",
            refusal, read.as_log_dict(),
        )
        return AdoptionOutcome(
            adoption=VenueAdoption(),
            applied=False,
            refusal=refusal,
            read=read,
        )
    plan = derive_venue_adoption(
        local_positions=local_positions,
        venue_positions=read.positions,
        unsettled_instruments=unsettled_instruments,
    )
    if not adopt:
        return AdoptionOutcome(adoption=plan, applied=False, read=read)
    _apply_venue_adoption(plan, cache)
    _log_venue_adoption(plan, read)
    return AdoptionOutcome(adoption=plan, applied=True, read=read)


def _apply_venue_adoption(plan: VenueAdoption, cache: Any) -> None:
    """Write the plan's positions into *cache*, or nothing if the plan is empty.

    The write is the cache's own method, so the book's locking and its key
    scheme stay where they are owned — and when the cache offers the batch
    seam (``set_positions``), the whole plan lands under one set of instrument
    locks. That is the plan's own semantics: the rows are correct *together*,
    and a per-row apply lets a fill's read-modify-write slip between two rows
    or a reader see a half-applied plan (P0-F6).

    A cache with no batch seam still works, but the fallback is per-row and
    therefore NOT atomic: it is logged as such, because an adoption that
    silently half-applied while reporting a full correction is exactly the
    corruption this module exists to prevent. A cache with no write method at
    all is reported rather than worked around.
    """
    if plan.is_empty:
        return
    rows = [*plan.positions.values(), *plan.cleared.values()]
    write_batch = getattr(cache, "set_positions", None)
    if callable(write_batch):
        write_batch(rows)
        return
    write = getattr(cache, "set_position", None)
    if not callable(write):
        raise TypeError(
            "venue position adoption requires a cache with set_position(Position)"
        )
    log.warning(
        "venue position adoption apply is NOT atomic: cache lacks "
        "set_positions(Sequence[Position]); falling back to per-row writes"
    )
    for position in rows:
        write(position)


def _log_venue_adoption(plan: VenueAdoption, read: VenueRead) -> None:
    """Log the adoption, per instrument it acted on and once for the summary."""
    if plan.is_empty:
        log.info(
            "venue position adoption: no changes (venue=%d positions)",
            len(read.positions),
        )
        return
    for row in plan.as_log_rows():
        log.warning("venue position adoption: %s", row)
    log.warning(
        "venue position adoption complete: adopted=%d retained=%d cleared=%d "
        "skipped=%d",
        len(plan.positions),
        len(plan.retained),
        len(plan.cleared),
        len(plan.skipped),
    )


def derive_venue_adoption(
    *,
    local_positions: Sequence[Any],
    venue_positions: Sequence[Any],
    unsettled_instruments: frozenset[str] | set[str] | None = None,
) -> VenueAdoption:
    """Decide what an adoption of *venue_positions* would write to the local book.

    Pure. No book is touched, so a caller can inspect the plan, log it, and only
    then apply it — and a caller that decides not to apply it has lost nothing. The
    plan's four buckets account for every instrument on either side, so nothing is
    silently dropped (see :class:`VenueAdoption`).

    Completeness of the venue answer is the caller's to prove (see
    :func:`read_venue_positions`), not this function's to assume: an instrument the
    local book holds that the venue omits is a *skip*, never a zero, because a gap
    read as flat is the corruption this module exists to prevent. Only the caller
    that proved the read complete gets a clear.
    """
    local_by_id: dict[str, Any] = {}
    for position in local_positions or ():
        iid = _position_instrument_id(position)
        if iid is not None:
            local_by_id[iid] = position

    venue_by_id: dict[str, Any] = {}
    unreadable: set[str] = set()
    for position in venue_positions or ():
        iid = _position_instrument_id(position)
        if iid is None:
            # A venue row that cannot even be keyed is unadoptable, and adopting
            # "everything we could read" would quietly drop it.
            unreadable.add(_UNREADABLE_INSTRUMENT)
            continue
        venue_by_id[iid] = position

    unsettled = frozenset(unsettled_instruments or ())

    positions: dict[str, Any] = {}
    retained: dict[str, Any] = {}
    cleared: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    previous: dict[str, Any] = {}

    # Every instrument the local book holds, whether or not the venue named it.
    for iid in sorted(local_by_id):
        local = local_by_id[iid]
        venue = venue_by_id.get(iid)
        if venue is None:
            # The venue named no row for an instrument the book holds. That is the
            # venue saying flat only if the read was proven complete (see
            # :func:`read_venue_positions`); a caller that did not prove it gets the
            # skip rather than the clear, because clearing is the destructive
            # direction and a gap reading as flat is the corruption this module
            # exists to prevent.
            skipped[iid] = SKIP_VENUE_OMITTED
            continue
        venue_quantity = _venue_quantity(venue)
        local_quantity = _venue_quantity(local)
        if venue_quantity is None or local_quantity is None:
            # A quantity this side cannot read is an unreadable reading, and the
            # honest report is "could not adopt" rather than a guess at zero.
            skipped[iid] = SKIP_UNREADABLE_QUANTITY
            continue
        if venue_quantity == local_quantity and _agrees_on_price(local, venue):
            # Already agrees. An adoption is a correction, not a rewrite: touching
            # an agreeing position would churn the log on every poll forever.
            retained[iid] = local
            continue
        if iid in unsettled:
            # Disagreeing *and* unsettled is the worst case: the book has a fill the
            # venue has not reflected, so the venue's figure is wrong for the very
            # quantity it reports. Adopting would undo the fill.
            skipped[iid] = SKIP_UNSETTLED_FILL
            continue
        if venue_quantity == Decimal("0"):
            # The venue reports flat. A zero-quantity position is written rather than
            # a delete, because the cache is keyed by instrument and a strategy that
            # reads the position next must find one.
            cleared[iid] = _flat_position(local) or _flat_position(venue)
            previous[iid] = local
            continue
        adopted = _adopted_position(local, venue)
        if adopted is None:
            skipped[iid] = SKIP_UNREADABLE_POSITION
            continue
        positions[iid] = adopted
        previous[iid] = local

    # Instruments only the venue named: the local book is flat and the venue says
    # there is a position. These are exactly what reconciliation used to report as
    # drift and then leave in place; adoption writes them.
    for iid in sorted(set(venue_by_id) - set(local_by_id)):
        venue = venue_by_id[iid]
        venue_quantity = _venue_quantity(venue)
        if venue_quantity is None:
            skipped[iid] = SKIP_UNREADABLE_QUANTITY
            continue
        if venue_quantity == Decimal("0"):
            # Flat both sides: nothing to write, and an entry here would only churn
            # the skip counter on every poll. Checked before the unsettled guard for
            # the same reason: there is no figure here to protect a live fill with.
            continue
        if iid in unsettled:
            # The same guard the both-sides branch applies, for the same reason
            # (P0-F2): a venue-only row on an instrument the book believes is
            # mid-flight is a snapshot the venue has not yet reflected, so
            # adopting it writes a stale figure over a live order. This branch
            # previously consulted no guard at all, which made the module's
            # promise — "an unsettled fill is never adopted over" — false for
            # the very case that needs it most: a first fill, or a name cleared
            # and refilled, where the book holds no row to compare against.
            skipped[iid] = SKIP_UNSETTLED_FILL
            continue
        adopted = _adopted_position(None, venue)
        if adopted is None:
            skipped[iid] = SKIP_UNREADABLE_POSITION
            continue
        positions[iid] = adopted

    for iid in unreadable:
        skipped[iid] = SKIP_UNREADABLE_INSTRUMENT

    return VenueAdoption(
        positions=positions,
        retained=retained,
        cleared=cleared,
        skipped=skipped,
        previous=previous,
    )


