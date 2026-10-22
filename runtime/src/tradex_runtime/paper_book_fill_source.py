"""Book-matching fill path for the paper session (AC-14, spec lane 2d).

Flow B runs two paper fill paths side by side, selected per run:

* ``slippage_model`` — fill at the live quote through a slippage model
  (``PaperFillSource`` + a ``SlippageModel``), and
* ``book_matching`` — **the existing paper broker's simulated book**: a
  multi-level order book the fill walks, price and size.

This module is the second one, adapted to the ``FillSource`` seam the
``ExecutionEngine`` already consumes, so the selection is a single switch
(``ExecutionConfig.fill_path``) rather than two divergent pipelines. The
matching itself is NOT reimplemented: it is delegated to the real
``tradex_brokers.paper.adapter.PaperBroker`` running in ``order_book`` mode, so
the oracle is the same book any other paper run matches against.

Why a bridge and not a second matcher
-------------------------------------
The two paths are structurally different objects today — a ``BrokerAdapter``
(``PaperBroker``) and a ``FillSource`` (``PaperFillSource``) — so they could
not be compared on one engine, one ledger and one fee model. Adapting the
existing book onto the ``FillSource`` seam is the minimum change that makes
"selectable per run" true AND comparable, and it reuses the book verbatim rather
than trusting a reimplementation of it.

Book lifecycle
--------------
The book is a simulation fixture, not a venue: it is re-seeded around the
order's market reference before each submit so every submit matches against a
book whose mid is the same price the slippage path would have filled at. That
is what makes the two paths comparable — a divergence is then about *how* the
cost is modelled (walked levels vs a single slippage constant), not about two
different moments in time. The consumption the book walk performs is recorded
on a :class:`BookAudit` so a run can prove the oracle really walked a book
rather than silently falling back to a flat quote.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from tradex_brokers.paper.adapter import PaperBroker
from tradex_domain.enums import OrderSide, OrderStatus, OrderType
from tradex_domain.execution import Fill, Order, OrderRequest
from tradex_domain.instruments import Instrument
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.fill_sources import make_order


@dataclass
class BookAudit:
    """What the oracle's book actually did, so a run can prove it was used.

    A parity suite comparing a book-matching run against a slippage run must not
    be satisfiable by a book path that quietly fell back to the flat LTP — that
    would compare the slippage path against itself. These counters make the walk
    observable: ``level_fills`` counts orders that consumed at least one level,
    ``vwap_orders`` counts those that spanned more than one (the "one fill per
    partial" case), and ``orders`` lets a failure name the exact order that
    diverged.
    """

    submits: int = 0
    single_level_fills: int = 0
    vwap_fills: int = 0
    unfilled: int = 0
    orders: list[dict[str, Any]] = field(default_factory=list)

    def record(self, *, order: Order, fill: Fill, walked_levels: bool) -> None:
        self.submits += 1
        if walked_levels:
            self.vwap_fills += 1
        else:
            self.single_level_fills += 1
        self.orders.append(
            {
                "instrument": str(fill.instrument.instrument_id),
                "side": fill.side.value,
                "requested_qty": str(order.quantity.value),
                "filled_qty": str(fill.quantity.value),
                "price": str(fill.price.value),
                "walked_levels": walked_levels,
            }
        )

    def record_unfilled(self, *, instrument: str, side: str, qty: str) -> None:
        self.submits += 1
        self.unfilled += 1
        self.orders.append(
            {
                "instrument": instrument,
                "side": side,
                "requested_qty": qty,
                "filled_qty": "0",
                "price": "",
                "walked_levels": False,
            }
        )


class PaperBookFillSource:
    """``FillSource`` that fills by walking ``PaperBroker``'s simulated book.

    Parameters
    ----------
    spread_bps:
        Half-spread in BASIS POINTS of the mid. The book is seeded as
        ``bid = mid - mid*spread_bps/10_000`` and ``ask = mid + ...``, so a
        top-of-book fill costs exactly ``mid*spread_bps/10_000`` per share — the
        quantity the production ``PercentageSlippageModel(spread_bps/10_000)``
        charges. Sizing the spread in bps (not in price units) is what lets the
        oracle and the slippage path be compared at the SAME constant over a
        session whose price moves, which a fixed per-share spread cannot do.
    level_depth:
        Shares resting at each price level. An order larger than one level walks
        several and is booked at the consumed-level VWAP, which is exactly the
        "one fill per partial vs one fill per order" asymmetry the spec compares
        at the round-trip level rather than fill by fill.
    levels:
        How many price levels to seed on each side.
    tick:
        Spacing between adjacent price levels.
    cash:
        Starting cash for the INTERNAL paper broker. The session's
        ``ExecutionEngine`` owns the real ledger (this source declares
        ``position_projection_owned = False``); the broker's own cash exists only
        so its internal fill accounting has a balance to move, and is given a
        large one so it can never be the thing that refuses an order.
    """

    #: Never contacts a venue: every failure here is definitively local, so the
    #: engine may file it as a rejection rather than an unknown outcome.
    reaches_venue = False
    #: The ExecutionEngine applies position and cash for this source, exactly as
    #: it does for the slippage path — otherwise the two runs would differ in
    #: whose ledger moves, not merely in how the price was found.
    position_projection_owned = False
    #: Book matching is a local simulation, so no submit crosses a boundary.
    submission_boundary_crossed = False

    def __init__(
        self,
        *,
        spread_bps: Decimal = Decimal("5"),
        level_depth: Decimal = Decimal("100000"),
        levels: int = 5,
        tick: Decimal = Decimal("0.05"),
        cash: Decimal = Decimal("1000000000"),
        audit: BookAudit | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if spread_bps < 0:
            raise ValueError(f"spread_bps must be >= 0, got {spread_bps}")
        if level_depth <= 0:
            raise ValueError(f"level_depth must be > 0, got {level_depth}")
        if levels < 1:
            raise ValueError(f"levels must be >= 1, got {levels}")
        if tick <= 0:
            raise ValueError(f"tick must be > 0, got {tick}")
        self._spread_bps = spread_bps
        self._level_depth = level_depth
        self._levels = levels
        self._tick = tick
        #: The existing book. ``project_positions=False`` because the engine is
        #: the book of record; ``auto_fill=True`` so a submit matches immediately.
        #: ``clock`` is forwarded to the broker's session gate, which refuses a
        #: fill outside 09:15-15:30 IST: a consumer that pins a simulated session
        #: time (a parity run, a replay) must pin it here rather than inherit the
        #: host clock, or the book silently stops matching after the close.
        self._broker: PaperBroker = PaperBroker(
            starting_cash=cash,
            auto_fill=True,
            project_positions=False,
            order_book=True,
            clock=clock,
        )
        #: The reactive cache, bound by the composition root after the engine
        #: exists, exactly as the slippage path's ``PaperFillSource`` is. It is
        #: the LIVE quote both fill paths anchor on, so book and slippage
        #: reference the same instant.
        self._cache: Any = None
        #: Market reference per instrument, set explicitly when a caller knows
        #: better than the cache. When absent the cache LTP, then the order's own
        #: price, is the reference.
        self._mid: dict[str, Price] = {}
        self._audit = audit if audit is not None else BookAudit()
        #: Filled-quantity seen per broker order id, so re-reading an order never
        #: double-counts a walk.
        self._last_seen_filled: dict[str, Decimal] = {}

    @property
    def audit(self) -> BookAudit:
        """What the book did this run — read by the parity report."""
        return self._audit

    @property
    def broker(self) -> PaperBroker:
        """The simulated book itself, for depth inspection in a report."""
        return self._broker

    def bind_cache(self, cache: Any) -> None:
        """Attach the OMS cache so the book is seeded around the LIVE quote.

        Called by the composition root once the engine exists, mirroring
        ``PaperFillSource.bind_cache``. The book and the slippage model then read
        the SAME market reference, so a divergence between the two fill paths is
        about cost modelling and not about which quote each one saw.
        """
        self._cache = cache

    def set_mid_price(self, instrument: Instrument, ltp: Price) -> None:
        """Pin the book mid for an instrument, overriding the cache quote."""
        self._mid[str(instrument.instrument_id)] = ltp

    def _ltp_from_cache(self, request: OrderRequest) -> Price | None:
        """Latest quote for the order's instrument, if the cache has one."""
        cache = self._cache
        if cache is None or not hasattr(cache, "get_quote"):
            return None
        quote = cache.get_quote(request.instrument)
        if quote is None:
            return None
        return Price(value=quote.ltp.value)

    def _mid_for(self, request: OrderRequest) -> Price:
        """The live quote this order matches against.

        Prefers an explicitly pinned mid, then the cache LTP (the same reference
        the slippage path fills from), then the order's own price (the
        ``next_open`` reference a strategy-driven order carries). It never invents
        a price: a book seeded around a made-up number would agree with a
        slippage model for reasons that have nothing to do with either one.
        """
        mid = self._mid.get(str(request.instrument.instrument_id))
        if mid is not None:
            return mid
        cached = self._ltp_from_cache(request)
        if cached is not None and cached.value > 0:
            return cached
        if request.price is not None and request.price.value > 0:
            return request.price
        if request.trigger_price is not None and request.trigger_price.value > 0:
            return request.trigger_price
        raise ValueError(
            f"book matching has no market reference for "
            f"{request.instrument.instrument_id}; call set_mid_price() or "
            f"submit a priced order"
        )

    def _half_spread(self, mid: Price) -> Decimal:
        """Half-spread at *mid*, in price units (bps of the mid)."""
        return mid.value * self._spread_bps / Decimal(10000)

    def _seed_book(self, instrument: Instrument, mid: Price) -> None:
        """(Re)seed a symmetric book around *mid* on both sides."""
        half = self._half_spread(mid)
        bids: list[tuple[Price, Quantity]] = []
        asks: list[tuple[Price, Quantity]] = []
        for level in range(self._levels):
            offset = half + Decimal(level) * self._tick
            depth = Quantity(value=self._level_depth)
            bids.append((Price(value=mid.value - offset), depth))
            asks.append((Price(value=mid.value + offset), depth))
        self._broker.set_depth(instrument, bids=bids, asks=asks)
        self._broker.set_quote(
            instrument,
            ltp=mid,
            bid=Price(value=mid.value - half),
            ask=Price(value=mid.value + half),
        )

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
        instrument = request.instrument
        mid = self._mid_for(request)
        half = self._half_spread(mid)
        top_of_book = (
            Price(value=mid.value + half)
            if request.side is OrderSide.BUY
            else Price(value=mid.value - half)
        )
        self._seed_book(instrument, mid)

        order_id = self._broker.submit_order(self._to_book_order(request))
        broker_order = self._broker.get_order(order_id)
        if broker_order is None:  # pragma: no cover - the broker registers every submit
            raise RuntimeError(f"paper broker lost order {order_id}")
        # A FRESH OMS order at ACK: the engine transitions ACK → FILLED and
        # sets filled_quantity itself. Returning the broker's already-filled
        # order would make on_order_filled see remaining=0 and clamp the fill
        # to zero — the engine, not the broker, owns the OMS projection here.
        order = make_order(request, status=OrderStatus.ACK, order_id=order_id)

        key = str(order_id.value)
        already = self._last_seen_filled.get(key, Decimal(0))
        filled_now = broker_order.filled_quantity.value - already
        self._last_seen_filled[key] = broker_order.filled_quantity.value

        if broker_order.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) \
                or filled_now <= 0:
            # The book could not match this order. Report an open order with no
            # fill rather than inventing one: the engine then leaves it open,
            # exactly as a venue would, and the parity report lists it.
            self._audit.record_unfilled(
                instrument=str(instrument.instrument_id),
                side=request.side.value,
                qty=str(request.quantity.value),
            )
            return order, None

        price = broker_order.avg_price_traded or broker_order.price
        if price is None:  # pragma: no cover - a book-filled order carries a price
            raise ValueError(f"book-filled order {order_id} has no price")
        fill = Fill(
            order_id=order_id,
            instrument=instrument,
            side=order.side,
            quantity=Quantity(value=filled_now),
            price=price,
            timestamp=(
                request.reference_timestamp
                if request.reference_timestamp is not None
                else datetime.now(UTC)
            ),
            product_type=order.product_type,
        )
        # How many price levels the walk touched, inferred from the fill's own
        # price rather than by reading the consumed book: ``PaperBroker.depth()``
        # synthesises a one-level book from the quote, so the real multi-level
        # book it walks is not readable through the public API. A fill strictly
        # worse than the top level can only have come from a walk across levels
        # (a BUY above the ask, a SELL below the bid), which is exactly the "one
        # fill per partial" case the spec compares at round-trip level. Anything
        # at the top level is a single-level fill.
        walked = (
            fill.price.value > top_of_book.value
            if request.side is OrderSide.BUY
            else fill.price.value < top_of_book.value
        )
        self._audit.record(order=order, fill=fill, walked_levels=walked)
        return order, fill

    @staticmethod
    def _to_book_order(request: OrderRequest) -> OrderRequest:
        """The request as the BOOK must see it.

        ``protective_request`` stamps ``price`` on every order, including
        MARKET ones, where it is a *reference* (the ``next_open`` the strategy
        priced against) and not a limit. ``PaperBroker`` reads ``Order.price`` as
        the book's price bound, so handing it over verbatim caps a MARKET buy at
        the reference price and the book — whose best ask is above it by exactly
        the spread — matches nothing at all. A MARKET order has no bound, so the
        reference price is dropped for it and kept for a genuine LIMIT.
        """
        if request.order_type is not OrderType.MARKET or request.price is None:
            return request
        return replace(request, price=None)

    def cancel(self, order_id: OrderId) -> None:
        self._broker.cancel_order(order_id)

    def modify(self, order_id: OrderId, request: OrderRequest) -> None:
        self._broker.modify_order(order_id, request)
