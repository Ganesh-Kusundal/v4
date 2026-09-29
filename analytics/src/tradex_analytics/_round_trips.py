"""Round-trip trade derivation — the single home of fills-to-trades pairing.

Two pairings live here because they answer two different questions.

``round_trip_trades`` is the canonical derivation: FIFO lot matching over
domain ``Fill`` objects, one trade per matched portion, with per-order cost
and exit-reason attribution and optional MAE/MFE measurement. Backtest,
replay, paper and live all book their fills through the same
``domain.position_math.apply_fill`` model (parity review CRITICAL-1), so this
one derivation reproduces the same round trips in every execution mode, and
the results UI reads one shape no matter where the run ran.

``round_trips_from_fills`` pairs engine fill *records* (dicts, not domain
fills) by position lifecycle: one trip per instrument per entry-to-zero
sequence, scale-ins averaged into the entry price, partial exits folded into
the one trip. The promotion bar counts these, because a ``>=`` floor on
round trips must not be flattered by counting FIFO portions — a scale-in
closed by one order is one trip there and two portions here.

Pure computation, no I/O — the caller owns persistence.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.market import Candle
from tradex_domain.utils import q2

#: Zero, quantized like every other money value in the codebase.
_ZERO = q2(Decimal(0))


class ValidationReportError(RuntimeError):
    """A validation report cannot be built from what it was handed.

    A ``RuntimeError`` rather than a ``ValueError``: this is not a bad argument
    to a function, it is a run that failed to produce the evidence the verdict
    is computed from, and the operator has to see it.
    """


@dataclass(frozen=True, slots=True)
class Trade:
    """One closed round trip — the row of the results-UI trade list."""

    symbol: str
    side: str  # "LONG" | "SHORT"
    quantity: Decimal
    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime
    exit_price: Decimal
    gross_pnl: Decimal
    costs: Decimal
    net_pnl: Decimal
    holding_period: timedelta
    exit_reason: str = ""
    #: Maximum adverse / favourable excursion as a *price* distance from the
    #: entry, one-sided (both are >= 0). ``None`` when no bars were supplied
    #: to measure the path between entry and exit.
    mae: Decimal | None = None
    mfe: Decimal | None = None

    @property
    def is_win(self) -> bool:
        """True when the round trip made money after costs."""
        return self.net_pnl > 0

    def to_dict(self) -> dict[str, object]:
        """JSON-ready row for the API/chart (times as IST epoch seconds)."""
        return {
            "symbol": self.symbol,
            "side": self.side,
            "qty": float(self.quantity),
            "entry_time": int(self.entry_time.timestamp()),
            "entry_price": float(self.entry_price),
            "exit_time": int(self.exit_time.timestamp()),
            "exit_price": float(self.exit_price),
            "gross_pnl": float(self.gross_pnl),
            "costs": float(self.costs),
            "net_pnl": float(self.net_pnl),
            "holding_period": self.holding_period.total_seconds(),
            "exit_reason": self.exit_reason,
            "mae": None if self.mae is None else float(self.mae),
            "mfe": None if self.mfe is None else float(self.mfe),
        }


@dataclass(slots=True)
class _Lot:
    """One open entry lot awaiting its closing fill(s)."""

    signed_qty: Decimal  # > 0 long, < 0 short
    price: Decimal
    time: datetime
    cost_per_unit: Decimal


def _instrument_symbol(instrument: object) -> str:
    """Display symbol for *instrument* — matches the datalake's keying."""
    symbol = getattr(instrument, "symbol", None)
    return str(symbol) if symbol else str(getattr(instrument, "instrument_id", instrument))


def _excursions(
    bars: Mapping[str, Sequence[Candle]],
    symbol: str,
    entry_time: datetime,
    exit_time: datetime,
    entry_price: Decimal,
    is_long: bool,
) -> tuple[Decimal | None, Decimal | None]:
    """Return ``(mae, mfe)`` price distances, or ``(None, None)`` without bars.

    Walks only the bars inside ``[entry_time, exit_time]`` via binary search on
    the (assumed time-sorted) series, so this stays O(log n + bars_in_trade).
    """
    series = bars.get(symbol)
    if not series:
        return None, None
    times = [b.timestamp for b in series]
    lo = bisect_left(times, entry_time)
    hi = bisect_right(times, exit_time)
    window = series[lo:hi]
    if not window:
        return None, None
    highest = max(b.ohlc.high.value for b in window)
    lowest = min(b.ohlc.low.value for b in window)
    if is_long:
        # Long: adverse = price fell below entry, favourable = price rose above.
        return q2(max(entry_price - lowest, _ZERO)), q2(max(highest - entry_price, _ZERO))
    return q2(max(highest - entry_price, _ZERO)), q2(max(entry_price - lowest, _ZERO))


def round_trip_trades(
    fills: Iterable[Fill],
    *,
    cost_by_order: Mapping[str, Decimal] | None = None,
    reason_by_order: Mapping[str, str] | None = None,
    bars_by_symbol: Mapping[str, Sequence[Candle]] | None = None,
) -> list[Trade]:
    """Pair *fills* into closed round trips using FIFO lot matching.

    A lot opened by a fill is closed by the next opposing fills until its
    quantity is exhausted, so partial exits and scale-ins produce one ``Trade``
    per matched portion (the standard FIFO attribution). Reversals work: an
    oversized opposing fill closes every open lot and opens a fresh one with
    the remainder.

    Parameters
    ----------
    fills:
        Fills in any order; they are sorted by timestamp before matching.
    cost_by_order:
        Total cost (fees + taxes + slippage) booked per ``order_id``,
        attributed to each matched portion in proportion to its quantity.
    reason_by_order:
        Exit reason per ``order_id`` (e.g. ``"stop_loss"``, ``"target"``),
        stamped onto the trade the fill closes.
    bars_by_symbol:
        Time-sorted bars keyed by the same symbol :func:`_instrument_symbol`
        returns, used to measure MAE/MFE. Omit to leave both ``None``.

    Returns
    -------
    list[Trade]
        Closed round trips in exit order. Fills that never close stay open and
        are not reported — matching ``Position``, which is still non-zero.
    """
    costs = cost_by_order or {}
    reasons = reason_by_order or {}
    bars = bars_by_symbol or {}
    open_lots: dict[str, deque[_Lot]] = {}
    trades: list[Trade] = []

    for fill in sorted(fills, key=lambda f: f.timestamp):
        symbol = _instrument_symbol(fill.instrument)
        total_qty = abs(fill.quantity.value)
        if total_qty == 0:
            continue
        # Per-unit cost for this fill, so each matched portion carries its share.
        cost_per_unit = q2(costs.get(str(fill.order_id), _ZERO) / total_qty)
        signed = total_qty if fill.side == OrderSide.BUY else -total_qty
        price = fill.price.value

        lots = open_lots.setdefault(symbol, deque())

        while signed != 0 and lots and (lots[0].signed_qty > 0) != (signed > 0):
            lot = lots[0]
            matched = min(abs(signed), abs(lot.signed_qty))
            is_long = lot.signed_qty > 0
            # Long: bought at lot.price, sold at price. Short: sold at
            # lot.price, bought back at price.
            gross = q2((price - lot.price) * matched if is_long else (lot.price - price) * matched)
            trade_cost = q2((lot.cost_per_unit + cost_per_unit) * matched)
            mae, mfe = _excursions(bars, symbol, lot.time, fill.timestamp, lot.price, is_long)
            trades.append(
                Trade(
                    symbol=symbol,
                    side="LONG" if is_long else "SHORT",
                    quantity=matched,
                    entry_time=lot.time,
                    entry_price=lot.price,
                    exit_time=fill.timestamp,
                    exit_price=price,
                    gross_pnl=gross,
                    costs=trade_cost,
                    net_pnl=q2(gross - trade_cost),
                    holding_period=fill.timestamp - lot.time,
                    exit_reason=reasons.get(str(fill.order_id), ""),
                    mae=mae,
                    mfe=mfe,
                )
            )
            lot.signed_qty -= matched if lot.signed_qty > 0 else -matched
            signed -= matched if signed > 0 else -matched
            if lot.signed_qty == 0:
                lots.popleft()

        if signed != 0:
            lots.append(
                _Lot(
                    signed_qty=signed,
                    price=price,
                    time=fill.timestamp,
                    cost_per_unit=cost_per_unit,
                )
            )

    return trades


# ---------------------------------------------------------------- round trips


def _field(record: Any, name: str) -> Any:
    """Read *name* from a mapping record or an object attribute.

    The engine's ``BacktestResult.fills`` are dicts; a research caller may hold
    real ``Fill`` objects. Both are read the same way so neither caller has to
    convert into the other's shape.
    """
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def _time_of(record: Any) -> datetime | None:
    """A record's timestamp, under either name it is written.

    The engine's ``BacktestResult.fills`` records use ``time``; a domain ``Fill``
    uses ``timestamp``. Both are read here so neither caller converts into the
    other's shape.
    """
    return _field(record, "time") or _field(record, "timestamp")


def _quantity_of(record: Any) -> Any:
    """A record's size, under either name it is written.

    The second half of :func:`_time_of`'s bargain, and it was missing: the
    engine's records carry ``qty`` and a domain ``Fill`` carries ``quantity``,
    so reading only the first raised "a fill record is missing its quantity" on
    exactly the ``Fill`` objects this function's docstring promises to accept.
    One of the two callers was being made to convert after all, and the failure
    was a ``ValidationReportError`` about a field that was present.

    ``Price`` and ``Quantity`` need no help: both render as their decimal value,
    so ``_decimal`` reads a wrapped number and a bare one the same way.
    """
    qty = _field(record, "qty")
    return _field(record, "quantity") if qty is None else qty


def _is_buy(side: Any) -> bool:
    text = str(getattr(side, "value", side)).strip().upper()
    if text in {"BUY", "B", "LONG"}:
        return True
    if text in {"SELL", "S", "SHORT"}:
        return False
    raise ValidationReportError(
        f"fill side {side!r} is neither a buy nor a sell, so it cannot be paired "
        "into a round trip."
    )


def _decimal(value: Any, what: str) -> Decimal:
    if value is None:
        raise ValidationReportError(f"a fill record is missing its {what}.")
    return q2(Decimal(str(value)))


def _executed(records: Sequence[Any]) -> list[tuple[datetime, bool, Decimal, Decimal]]:
    """The fill records that reached the book, as ``(time, is_buy, qty, price)``.

    A rejected order never reached the book and a zero price is not a price, so
    neither is a fill and neither may become half a round trip. Sorted by time,
    stably, so two fills on one bar keep the order they were recorded in.
    """
    out: list[tuple[datetime, bool, Decimal, Decimal]] = []
    for record in records:
        if _field(record, "rejected"):
            continue
        qty = _decimal(_quantity_of(record), "quantity")
        price = _decimal(_field(record, "price"), "price")
        if qty == 0 or price <= 0:
            continue
        when = _time_of(record)
        if when is None:
            raise ValidationReportError(
                "a fill record carries no timestamp, so it cannot be placed inside "
                "or outside the out of sample window."
            )
        out.append((when, _is_buy(_field(record, "side")), qty, price))
    out.sort(key=lambda item: item[0])
    return out


def _bar_instruments(bars: Sequence[Any]) -> dict[datetime, frozenset[str]]:
    """``timestamp -> instruments with a bar at that timestamp``.

    The engine stamps a ``next_open`` fill with the bar it filled against
    (``FillModel.fill_timestamp`` takes the request's reference timestamp, and
    the strategy engine sets it to the candle being filled), so the bar is the
    record's own statement of which instrument traded.
    """
    index: dict[datetime, set[str]] = {}
    for bar in bars:
        when = getattr(bar, "timestamp", None)
        instrument = getattr(bar, "instrument", None)
        if when is None or instrument is None:
            raise ValidationReportError(
                "a bar carries no timestamp or no instrument, so fills cannot be "
                "attributed to a position."
            )
        index.setdefault(when, set()).add(
            str(getattr(instrument, "instrument_id", instrument))
        )
    return {when: frozenset(names) for when, names in index.items()}


def _instrument_of(
    record: Any, when: datetime, bars: dict[datetime, frozenset[str]]
) -> str:
    """The instrument a fill belongs to, or a loud failure.

    Three ways to name it, in order of authority: the record's own
    ``instrument``, the bar it filled against, and nothing — in which case this
    raises. A scalar ledger over symbols it cannot separate invents round trips
    (a buy in one symbol and a sell in another reads as a position taken to
    zero), and two bars at one timestamp is ambiguity rather than an answer.
    """
    named = _field(record, "instrument")
    if named is not None:
        return str(getattr(named, "instrument_id", named))
    candidates = bars.get(when)
    if candidates and len(candidates) == 1:
        return next(iter(candidates))
    detail = (
        f"ambiguous: {len(candidates)} instruments have a bar at that timestamp"
        if candidates
        else "no bar carries that timestamp"
    )
    raise ValidationReportError(
        f"a fill at {when.isoformat()} cannot name its instrument ({detail}). "
        "Positions are per instrument, so a fill stream that cannot be separated "
        "would report round trips that never happened. Either the record carries "
        "an 'instrument', or exactly one instrument has a bar at that timestamp."
    )


@dataclass(frozen=True, slots=True)
class _OpenLot:
    """One open position, by instrument, and the trip it is accumulating.

    A trip is emitted when the position reaches zero or flips — not when any
    single fill reduces it. Two partial exits of one position are one trip with
    two closing fills, which is what "a fill sequence that takes a position to
    zero" means, and what the results surface's trip list shows.
    """

    signed_qty: Decimal  # > 0 long, < 0 short
    avg_price: Decimal
    since: datetime
    #: Closed quantity, closing notional and realized P&L so far.
    closed_qty: Decimal = Decimal(0)
    closed_notional: Decimal = Decimal(0)
    realized: Decimal = Decimal(0)


def round_trips_from_fills(
    records: Sequence[Any],
    *,
    bars: Sequence[Any] = (),
) -> list[Trade]:
    """Pair *records* into closed round trips, one position per instrument.

    A **round trip** is the fill sequence that takes a position to zero or
    through it: the entry, any scale-ins averaged into the entry price, and the
    fills that reduce the position. A partial exit is part of that sequence and
    not a trip of its own. A closing fill that overshoots completes the trip and
    opens the other side from the remainder, so a reversal is one trip and a new
    position.

    The reported row carries the entry, the quantity that was closed, the
    quantity-weighted exit price, and the P&L summed over the closing fills.

    This is the pairing the results surface shows — one row per round trip, in
    ``frontend/src/backtest.ts``'s ``pairRoundTrips`` — and it differs from
    :func:`round_trip_trades` in one respect, stated because it matters at a
    ``>=`` floor: that function returns one row per matched FIFO **portion**, so
    a scale-in closed by one order is two rows there and one trip here.

    The returned rows are ``Trade`` objects with ``costs`` at zero, because the
    engine's fill records carry no per-order cost breakdown. So
    ``compute_statistics`` over these rows reports per-trip P&L gross of fees,
    while the P&L the bar gates on — ``net_pnl_at_2x_costs`` — is net, read off
    the equity curves where the fees were actually debited.
    """
    index = _bar_instruments(bars)
    lots: dict[str, _OpenLot] = {}
    trips: list[Trade] = []

    for record in sorted(records, key=_time_of):
        if _field(record, "rejected"):
            continue
        qty = _decimal(_quantity_of(record), "quantity")
        price = _decimal(_field(record, "price"), "price")
        if qty == 0 or price <= 0:
            continue
        when = _time_of(record)
        instrument = _instrument_of(record, when, index)
        is_buy = _is_buy(_field(record, "side"))
        signed = qty if is_buy else -qty
        lot = lots.get(instrument)

        if lot is None or (lot.signed_qty > 0) == (signed > 0):
            # Opening, or adding to an open position: one averaged entry.
            if lot is None:
                lots[instrument] = _OpenLot(signed, price, when)
            else:
                total = abs(lot.signed_qty) + qty
                avg = q2(
                    (lot.avg_price * abs(lot.signed_qty) + price * qty) / total
                )
                lots[instrument] = replace(
                    lot, signed_qty=lot.signed_qty + signed, avg_price=avg
                )
            continue

        # An opposing fill: it closes what is open, and any remainder opens the
        # other side from this fill's own price.
        matched = min(abs(lot.signed_qty), qty)
        long_side = lot.signed_qty > 0
        realized = q2(
            (price - lot.avg_price) * matched
            if long_side
            else (lot.avg_price - price) * matched
        )
        closed = replace(
            lot,
            signed_qty=lot.signed_qty - (matched if long_side else -matched),
            closed_qty=lot.closed_qty + matched,
            closed_notional=lot.closed_notional + price * matched,
            realized=lot.realized + realized,
        )
        remainder = qty - matched
        if remainder > 0:
            # Through zero: this trip is complete and the remainder is a new
            # position opened by this same fill.
            trips.append(_closed_trip(instrument, long_side, closed, when))
            lots[instrument] = _OpenLot(
                remainder if is_buy else -remainder, price, when
            )
        elif closed.signed_qty == 0:
            trips.append(_closed_trip(instrument, long_side, closed, when))
            lots.pop(instrument, None)
        else:
            lots[instrument] = closed

    return trips


def _closed_trip(
    instrument: str, long_side: bool, lot: _OpenLot, exit_time: datetime
) -> Trade:
    """The ``Trade`` row for a position lifecycle that has just completed."""
    return Trade(
        symbol=instrument,
        side="LONG" if long_side else "SHORT",
        quantity=lot.closed_qty,
        entry_time=lot.since,
        entry_price=lot.avg_price,
        exit_time=exit_time,
        exit_price=q2(lot.closed_notional / lot.closed_qty),
        gross_pnl=q2(lot.realized),
        costs=q2(Decimal(0)),
        net_pnl=q2(lot.realized),
        holding_period=exit_time - lot.since,
    )
