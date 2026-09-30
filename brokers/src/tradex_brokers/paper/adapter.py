"""Paper (in-memory simulation) broker adapter.

Provides a fully self-contained ``BrokerAdapter`` implementation that keeps
all state in memory — no external broker connection required. Useful for
unit / integration tests and paper-trading sessions.

Supports auto-fill against a seeded tape, position projection, and cash
ledger management.

Realism gates (C5, 2026-09-29)
------------------------------
Paper mode is where strategies get *approved*.  A paper broker that is more
optimistic than live approves strategies that lose money, so this adapter
now refuses the three things a live venue refuses:

* **Order book by default.**  ``order_book=True`` is the constructor default
  (:attr:`PaperBroker.__init__`).  The old default was an unconditional 100%
  fill, which is the single most optimistic assumption a simulator can make.
* **A margin gate.**  :meth:`PaperBroker._apply_fill` refuses a buy it cannot
  pay for (:class:`~tradex_domain.errors.OrderRejectedError`) instead of
  letting the cash scalar walk negative.  The engine's ``CashLedger`` is a
  separate, engine-owned check; the broker's internal scalar is not covered
  by it, and an unguarded scalar approves unaffordable strategies.
* **A session gate.**  :meth:`PaperBroker._market_session_open` refuses fills
  outside the NSE/BSE cash session, using the shared
  :class:`tradex_domain.trading_calendar.NSETradingCalendar` rather than an
  invented local calendar.

The clock is injectable (``clock=``) precisely so the session gate is
testable and so a replay/backtest run can pin a simulated session time
instead of inheriting the host's wall clock.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from tradex_domain.capabilities import require_capability
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, Timeframe
from tradex_domain.errors import (
    BrokerUnavailableError,
    CapabilityNotSupportedError,
    OrderRejectedError,
)
from tradex_domain.execution import (
    Account,
    Fill,
    Order,
    OrderRequest,
    PortfolioSnapshot,
    Position,
)
from tradex_domain.instruments import Instrument
from tradex_domain.market import Depth, HistoricalSeries, Quote, require_depth_supported
from tradex_domain.market_calendar import NSE_HOLIDAYS_2026, to_ist_naive
from tradex_domain.options import OptionChain
from tradex_domain.position_math import apply_fill as _domain_apply_fill
from tradex_domain.protocols import TradingCacheProtocol
from tradex_domain.trading_calendar import NSETradingCalendar
from tradex_domain.value_objects import AccountId, InstrumentId, Money, OrderId, Price, Quantity

from tradex_brokers.common.base import BaseBroker
from tradex_brokers.common.capabilities import paper_capabilities

log = logging.getLogger(__name__)


class _PaperCache(TradingCacheProtocol):
    """Minimal dict-based cache for PaperBroker internal use."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._quotes: dict[str, Quote] = {}

    def update_order(self, order: Order) -> None:
        self._orders[order.order_id.value] = order

    def get_order(self, order_id: OrderId | str) -> Order | None:
        key = order_id.value if isinstance(order_id, OrderId) else order_id
        return self._orders.get(key)

    def all_orders(self) -> list[Order]:
        return list(self._orders.values())

    def update_position(self, position: Position) -> None:
        self._positions[str(position.instrument.instrument_id)] = position

    def get_position(self, instrument: Instrument | InstrumentId | str) -> Position | None:
        if isinstance(instrument, Instrument):
            key = str(instrument.instrument_id)
        elif isinstance(instrument, InstrumentId):
            key = str(instrument)
        else:
            key = instrument
        return self._positions.get(key)

    def all_positions(self) -> list[Position]:
        return list(self._positions.values())

    def update_quote(self, quote: Quote) -> None:
        self._quotes[str(quote.instrument.instrument_id)] = quote

    def get_quote(self, instrument: Instrument | InstrumentId | str) -> Quote | None:
        if isinstance(instrument, Instrument):
            key = str(instrument.instrument_id)
        elif isinstance(instrument, InstrumentId):
            key = str(instrument)
        else:
            key = instrument
        return self._quotes.get(key)

    def snapshot(self) -> dict[str, dict]:
        return {
            "orders": dict(self._orders),
            "positions": dict(self._positions),
            "quotes": dict(self._quotes),
        }

    def restore(self, data: dict[str, dict]) -> None:
        self._orders = dict(data.get("orders", {}))
        self._positions = dict(data.get("positions", {}))
        self._quotes = dict(data.get("quotes", {}))

    def clear(self) -> None:
        self._orders.clear()
        self._positions.clear()
        self._quotes.clear()


_PAPER_STARTING_CASH = Decimal("100000")
_CURRENCY = "INR"
_DEPTH_SIZE = Quantity(value=Decimal(100))
_DEFAULT_LTP = Price(value=Decimal("100"))
_DEFAULT_BID = Price(value=Decimal("99.95"))
_DEFAULT_ASK = Price(value=Decimal("100.05"))

#: Notional cushion the margin gate requires on top of the exact cost of a
#: buy, as a fraction of that cost.  A live venue reserves margin and charges
#: brokerage / GST / stamp duty on top of the traded value, so a gate that
#: checks ``cash >= notional`` exactly is still marginally more optimistic
#: than the broker it is simulating.  5% of notional is a rounded stand-in
#: for those charges.  It is a named constant so the assumption is greppable
#: and tunable rather than buried in the arithmetic.
_MARGIN_BUFFER = Decimal("0.05")

#: The clock the session gate reads when the caller injects none.  It is a
#: module-level name rather than an inline lambda so a test harness can pin
#: paper's notion of "now" to an in-session moment without monkeypatching the
#: constructor or reaching into private methods — see ``brokers/tests/conftest.py``.
def _default_clock() -> datetime:
    return datetime.now(UTC)


_DEFAULT_CLOCK: Callable[[], datetime] = _default_clock


class PaperHistoryNotImplemented(NotImplementedError):
    """Paper has no historical data source — ``history()`` is unimplemented.

    The old implementation returned a :class:`HistoricalSeries` with an empty
    ``candles`` tuple inside a valid-looking ``start``/``end`` window.  That
    made "this simulator has no tape" indistinguishable from "the market was
    shut and nothing traded in this window", so any caller that sliced or
    counted the result — a chart, a backtest loader, an indicator warm-up —
    silently read it as real data.

    Raising keeps the two apart.  A caller that genuinely wants a
    best-effort empty series has to say so by catching this, which is the
    decision the raise surfaces.  :attr:`PaperBroker.supports_history` lets
    that caller branch without a try/except.
    """


class PaperBroker(BaseBroker):
    """In-memory simulation broker, extending the same seam as the live ones.

    Candidate 4 of the 2026-09-17 architecture review: PaperBroker used to be
    a standalone class that manually re-implemented every protocol method, so
    a fix to ``BaseBroker``'s lifecycle, instrument loading or order gates did
    not propagate. It now inherits all of that and overrides only what makes
    it a simulation: the fill engine, the cash ledger, and the market data
    that would otherwise come from a venue.
    """

    #: No fallback universe — paper serves only what it was handed.
    _fallback_equities: tuple[str, ...] = ()

    def __init__(
        self,
        starting_cash: Decimal | None = None,
        auto_fill: bool = False,
        project_positions: bool = True,
        order_book: bool = True,
        *,
        enforce_margin: bool = True,
        enforce_session: bool = True,
        calendar: NSETradingCalendar | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Build a paper broker.

        Parameters
        ----------
        starting_cash:
            Paper cash balance.  Defaults to 100,000.
        auto_fill:
            Fill resting orders as soon as a matching quote/depth arrives.
        project_positions:
            Maintain this adapter's own position book from its fills.
        order_book:
            Walk a multi-level book for fills (partial fills, weighted-average
            price, per-level cash impact).  **Defaults to ``True``** (C5a):
            the previous default of ``False`` took the unconditional-100%-fill
            path, so the default configuration of a paper broker was the one
            that could never produce a partial fill.  Pass ``False`` only to
            deliberately restore that old behaviour.
        enforce_margin:
            Refuse a buy the paper cash balance cannot pay for.  ``True`` by
            default; ``False`` restores the unguarded scalar.
        enforce_session:
            Refuse fills outside the NSE/BSE cash session.  ``True`` by
            default; ``False`` restores 24x7 filling.
        calendar:
            Session calendar.  Defaults to
            :class:`~tradex_domain.trading_calendar.NSETradingCalendar` with
            the published 2026 NSE holiday list, so weekends and exchange
            holidays are both refused.  Inject your own for a different
            venue or a pinned holiday set.
        clock:
            Zero-argument callable returning the current time.  Defaults to
            ``datetime.now``.  The session gate reads it, which is what makes
            "3am on a Sunday" reproducible in a test and lets a replay pin a
            simulated session time instead of inheriting the host clock.
        """
        super().__init__(capabilities=paper_capabilities())
        self._auto_fill = auto_fill
        self._project_positions = project_positions
        self._order_book_enabled = order_book
        self._enforce_margin = enforce_margin
        self._enforce_session = enforce_session
        self._calendar = calendar if calendar is not None else NSETradingCalendar(
            holidays=set(NSE_HOLIDAYS_2026)
        )
        self._clock = clock if clock is not None else _DEFAULT_CLOCK
        self._cash = starting_cash if starting_cash is not None else _PAPER_STARTING_CASH
        self._quotes: dict[str, Quote] = {}
        self._depths: dict[str, Depth] = {}
        self._instruments: dict[str, Instrument] = {}
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._state_lock = threading.RLock()
        self._cache: TradingCacheProtocol = _PaperCache()
        self.connect()  # paper broker is connected on construction

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    # ``connect``/``close`` are inherited: they only flip ``_connected``.
    # ``_require`` is the seam a pass-through gate calls before delegating to
    # a transport — paper has no transport, so it refuses instead of handing
    # back ``None`` and letting a caller dereference it.

    def _require(self) -> Any:
        if not self._connected:
            raise BrokerUnavailableError("paper broker not connected")
        raise BrokerUnavailableError(
            "paper broker has no transport — market data comes from its own sim"
        )

    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerUnavailableError("paper broker not connected")

    # ------------------------------------------------------------------
    # orders
    # ------------------------------------------------------------------

    def submit_order(self, request: OrderRequest) -> OrderId:
        with self._state_lock:
            self._require_connected()
            return self._submit_order_locked(request)

    def _submit_order_locked(self, request: OrderRequest) -> OrderId:
        order_id = OrderId(value=f"PAPER-{uuid4().hex[:12]}")
        order = Order(
            order_id=order_id,
            instrument=request.instrument,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
            time_in_force=request.time_in_force,
            status=OrderStatus.PENDING,
            correlation_id=request.correlation_id,
            trigger_price=request.trigger_price,
            product_type=request.product_type,
            tag=request.tag,
            target_price=request.target_price,
            stop_loss_price=request.stop_loss_price,
            trailing_jump=request.trailing_jump,
        )
        if request.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            if request.trigger_price is None:
                raise OrderRejectedError(f"{request.order_type.value} requires trigger_price")
            if request.order_type is OrderType.STOP_LIMIT and request.price is None:
                raise OrderRejectedError("STOP_LIMIT requires price")
        order = order.transition_to(OrderStatus.ACK)
        self._register(request.instrument)
        if self._auto_fill:
            self._attempt_fill(order)
        # Check if the order was filled (status changed from ACK)
        stored = self._orders.get(order_id.value)
        if stored is None or stored.status is OrderStatus.ACK:
            # Not filled — store the ACK order
            self._orders[order_id.value] = order
        return order_id

    def _attempt_fill(self, order: Order) -> None:
        """Try to fill ``order``, turning a gate refusal into "not filled".

        A paper fill is *driven* by three very different callers: an order
        submission, a quote tick, and a depth update.  A refusal from the
        session or margin gate is a normal market outcome, not a programming
        error, so it must not escape as an exception out of any of them —
        least of all ``set_quote``, which is a market-data call that a caller
        has no reason to expect can fail because an order could not be
        funded.

        The order is left resting (ACK) and a warning is logged, which is
        what a live venue does with an order it will not fill yet: keep it
        on the book, fill it when the obstacle clears.  Genuine bugs still
        propagate; only :class:`OrderRejectedError` — the gates' own signal —
        is absorbed.
        """
        try:
            if self._order_book_enabled:
                self._try_fill_against_book(order)
            elif self._can_fill(order):
                self._fill_order(order)
        except OrderRejectedError as exc:
            log.warning(
                "paper refused to fill %s (%s %s x %s): %s",
                order.order_id.value,
                order.side.value,
                order.filled_quantity.value or order.quantity.value,
                order.price.value if order.price is not None else "MKT",
                exc,
            )

    def cancel_order(self, order_id: OrderId) -> Order:
        with self._state_lock:
            self._require_connected()
            order = self._get_order(order_id)
            cancelled = order.transition_to(OrderStatus.CANCELLED)
            self._orders[order_id.value] = cancelled
            return cancelled

    def modify_order(self, order_id: OrderId, request: OrderRequest) -> Order:
        with self._state_lock:
            self._require_connected()
            order = self._get_order(order_id)
            if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
                raise OrderRejectedError(f"cannot modify order in {order.status.value} status")
            modified = replace(
                order,
                price=request.price,
                quantity=request.quantity,
                target_price=request.target_price,
                stop_loss_price=request.stop_loss_price,
                trailing_jump=request.trailing_jump,
            )
            self._orders[order_id.value] = modified
            return modified

    def get_order(self, order_id: OrderId) -> Order:
        with self._state_lock:
            self._require_connected()
            return self._get_order(order_id)

    def get_orderbook(self) -> list[Order]:
        with self._state_lock:
            self._require_connected()
            return list(self._orders.values())

    def get_order_list(
        self,
        *,
        status: str = "ALL",
        from_date: str | None = None,
        to_date: str | None = None,
        sector: str | None = None,
    ) -> list[Order]:
        """Filtered order list (status filter only for paper)."""
        with self._state_lock:
            self._require_connected()
            orders = list(self._orders.values())
        if status != "ALL":
            status_upper = status.upper()
            orders = [o for o in orders if o.status.value.upper() == status_upper]
        return orders

    def _get_order(self, order_id: OrderId) -> Order:
        order = self._orders.get(order_id.value)
        if order is None:
            raise KeyError(f"unknown order {order_id.value}")
        return order

    # ------------------------------------------------------------------
    # portfolio
    # ------------------------------------------------------------------

    def get_positions(self) -> list[Position]:
        with self._state_lock:
            return [p for p in self._positions.values() if p.quantity.value != 0]

    def get_holdings(self) -> list[Position]:
        return self.get_positions()

    def get_account(self) -> Account:
        with self._state_lock:
            return Account(
                account_id=AccountId(value="PAPER"),
                balance=Money(amount=self._cash, currency=_CURRENCY),
                margin=Money(amount=Decimal(0), currency=_CURRENCY),
                equity=Money(amount=self._cash, currency=_CURRENCY),
            )

    def get_portfolio(self) -> PortfolioSnapshot:
        with self._state_lock:
            return PortfolioSnapshot(
                positions=self.get_positions(),
                account=Account(
                    account_id=AccountId(value="PAPER"),
                    balance=Money(amount=self._cash, currency=_CURRENCY),
                    margin=Money(amount=Decimal(0), currency=_CURRENCY),
                    equity=Money(amount=self._cash, currency=_CURRENCY),
                ),
            )

    def convert_position(
        self,
        instrument: Instrument,
        *,
        from_product: ProductType,
        to_product: ProductType,
        quantity: int,
        position_type: str = "LONG",
    ) -> dict[str, object]:
        """Simulated position conversion (no-op for paper)."""
        with self._state_lock:
            self._require_connected()
            return {
                "status": "converted",
                "instrument": str(instrument.instrument_id),
                "from_product": from_product.value,
                "to_product": to_product.value,
                "quantity": quantity,
            }

    def exit_all(self) -> dict[str, object]:
        """Cancel all open orders and flatten all positions.

        Open positions are settled at the last known quote (mid-price
        fallback to avg_price) so the cash ledger reflects the proceeds
        of closing every position — previously the cash was left stale
        and ``flattened_positions`` was hardcoded to 0.
        """
        with self._state_lock:
            self._require_connected()
            cancelled_count = 0
            for order in list(self._orders.values()):
                if order.status in (OrderStatus.PENDING, OrderStatus.ACK):
                    cancelled = order.transition_to(OrderStatus.CANCELLED)
                    self._orders[order.order_id.value] = cancelled
                    cancelled_count += 1
            # Settle open positions into cash before clearing
            flattened_count = 0
            for pos in list(self._positions.values()):
                if pos.quantity.value == 0:
                    continue
                inst_key = str(pos.instrument.instrument_id)
                quote = self._quotes.get(inst_key)
                if quote is not None:
                    if pos.quantity.value > 0 and quote.bid is not None:
                        settle_price = quote.bid.value
                    elif pos.quantity.value < 0 and quote.ask is not None:
                        settle_price = quote.ask.value
                    else:
                        settle_price = quote.ltp.value
                else:
                    settle_price = pos.avg_price.value
                self._cash += pos.quantity.value * settle_price
                flattened_count += 1
            self._positions.clear()
            return {
                "cancelled_orders": cancelled_count,
                "flattened_positions": flattened_count,
            }

    def mass_status(self) -> dict[str, object]:
        """Whole-account snapshot: orders + positions + account."""
        with self._state_lock:
            return {
                "orders": list(self._orders.values()),
                "positions": self.get_positions(),
                "account": self.get_account(),
            }

    # ------------------------------------------------------------------
    # market data
    # ------------------------------------------------------------------

    def set_quote(
        self,
        instrument: Instrument,
        *,
        ltp: Price | None = None,
        bid: Price | None = None,
        ask: Price | None = None,
    ) -> None:
        """Seed the tape for an instrument. ``ltp`` is used for MARKET fills."""
        with self._state_lock:
            self._set_quote_locked(instrument, ltp=ltp, bid=bid, ask=ask)

    def set_depth(
        self,
        instrument: Instrument,
        bids: list[tuple[Price, Quantity]],
        asks: list[tuple[Price, Quantity]],
    ) -> None:
        """Seed a multi-level order book for an instrument.

        When order_book mode is enabled, fills walk the book levels within
        the order's price bound, supporting partial fills.
        """
        with self._state_lock:
            self._register(instrument)
            depth = Depth(
                instrument=instrument,
                bids=tuple(bids),
                asks=tuple(asks),
                timestamp=datetime.now(UTC),
            )
            self._depths[str(instrument.instrument_id)] = depth
            # Derive a quote from the top of book for LTP/bid/ask
            top_bid = bids[0][0] if bids else None
            top_ask = asks[0][0] if asks else None
            ltp = top_bid if top_bid is not None else (
                top_ask if top_ask is not None else _DEFAULT_LTP
            )
            self._set_quote_locked(instrument, ltp=ltp, bid=top_bid, ask=top_ask)
            # Try to fill pending orders against the new depth
            for order in list(self._orders.values()):
                if (
                    order.instrument.instrument_id == instrument.instrument_id
                    and order.status is OrderStatus.ACK
                ):
                    self._attempt_fill(order)

    def _set_quote_locked(
        self,
        instrument: Instrument,
        *,
        ltp: Price | None,
        bid: Price | None,
        ask: Price | None,
    ) -> None:
        if ltp is None and bid is not None and ask is not None:
            ltp = Price(value=(bid.value + ask.value) / Decimal(2))
        if ltp is None:
            raise ValueError("set_quote requires ltp (or both bid and ask)")
        self._register(instrument)
        self._quotes[str(instrument.instrument_id)] = Quote(
            instrument=instrument,
            ltp=ltp,
            bid=bid,
            ask=ask,
            timestamp=datetime.now(UTC),
            exchange=instrument.exchange.value,
            provider="paper",
        )
        # Always try to fill pending orders when a new quote is set
        for order in list(self._orders.values()):
            if (
                order.instrument.instrument_id == instrument.instrument_id
                and order.status is OrderStatus.ACK
            ):
                self._attempt_fill(order)

    def _synthetic_quote(self, instrument: Instrument) -> Quote:
        """Return a default synthetic quote for an unseeded instrument."""
        return Quote(
            instrument=instrument,
            ltp=_DEFAULT_LTP,
            bid=_DEFAULT_BID,
            ask=_DEFAULT_ASK,
            timestamp=datetime.now(UTC),
            exchange=instrument.exchange.value,
            provider="paper",
        )

    def get_quote(self, instrument: Instrument) -> Quote:
        with self._state_lock:
            quote = self._quotes.get(str(instrument.instrument_id))
        if quote is None:
            return self._synthetic_quote(instrument)
        return quote

    def ltp(self, instrument: Instrument) -> Price:
        return self.get_quote(instrument).ltp

    def depth(self, instrument: Instrument) -> Depth:
        require_depth_supported(instrument)
        with self._state_lock:
            quote = self._quotes.get(str(instrument.instrument_id))
        if quote is None:
            return Depth(instrument=instrument, bids=(), asks=())
        bids = () if quote.bid is None else ((quote.bid, _DEPTH_SIZE),)
        asks = () if quote.ask is None else ((quote.ask, _DEPTH_SIZE),)
        return Depth(instrument=instrument, bids=bids, asks=asks, timestamp=datetime.now(UTC))

    def history(
        self,
        instrument: Instrument,
        timeframe: Timeframe | str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        interval: str | None = None,
        lookback_days: int | None = None,
    ) -> HistoricalSeries:
        """Always raises :class:`PaperHistoryNotImplemented` (C5d).

        Paper has no historical data source — no datalake, no vendor, no
        recorded tape.  The signature is kept so the adapter stays
        substitutable for :class:`~tradex_brokers.common.base.BaseBroker` and
        so the *argument* validation still runs before the raise, which keeps
        the cross-provider contract tests meaningful.

        It used to return ``HistoricalSeries(candles=[])`` with a plausible
        ``start``/``end`` window.  That answer is a lie in both directions:
        it reads as "the market was shut for 30 days" to a chart, and as a
        legitimate zero-row result to a backtest loader that then computes
        indicators over nothing.  Distinguishing *no data* from *not
        implemented* requires not fabricating a window, so the raise is the
        fix.  Callers that want best-effort behaviour should branch on
        :attr:`supports_history` or catch the error explicitly.
        """
        if interval is not None:
            timeframe = Timeframe(interval)
        if timeframe is None:
            raise ValueError("history requires a timeframe or interval")
        if not isinstance(timeframe, Timeframe):
            timeframe = Timeframe(timeframe)
        raise PaperHistoryNotImplemented(
            f"paper broker has no historical data source "
            f"({instrument.instrument_id} {timeframe.value}); "
            f"guard with PaperBroker.supports_history or catch this explicitly"
        )

    @property
    def supports_history(self) -> bool:
        """Always ``False`` — lets a caller branch without a try/except."""
        return False

    def get_option_chain(
        self,
        underlying: Instrument,
        expiry: date | str | None = None,
    ) -> OptionChain:
        require_capability(self.capabilities, "supports_option_chain")
        raise CapabilityNotSupportedError("paper broker does not support option chains")

    def search(self, query: str) -> list[Instrument]:
        with self._state_lock:
            q = query.strip().upper()
            return [i for i in self._instruments.values() if q in i.symbol.upper()]

    # ------------------------------------------------------------------
    # instruments
    # ------------------------------------------------------------------

    def load_instruments(
        self, rows: Iterable[Mapping[str, Any]] | None = None
    ) -> None:
        """No-op — paper broker has no external instrument source.

        Accepts the same ``rows`` argument as :meth:`BaseBroker.load_instruments`
        (and ignores it) so the override stays substitutable for the base class.
        """

    def stream_backend(self, *, ws_factory: object | None = None) -> object:
        """Order/portfolio update stream backend (none for paper broker)."""
        return None

    def market_stream_backend(self, *, ws_factory: object | None = None) -> object:
        """Quote-tick market-data stream backend (none for paper broker)."""
        return None

    def depth_stream_backend(
        self,
        *,
        total_slots: int = 20,
        ws_factory: object | None = None,
    ) -> object:
        """Depth stream backend (none for paper broker)."""
        return None

    def subscribe_quotes(self, instruments: object, handler: object) -> object:
        """Subscribe to quote stream (no-op for paper broker)."""
        return None

    def subscribe_depth(self, instrument: object, handler: object) -> object:
        """Subscribe to depth stream (no-op for paper broker)."""
        return None

    def unsubscribe(self, subscription: object) -> None:
        """Unsubscribe from a stream (no-op for paper broker)."""

    def unsubscribe_instruments(self, instruments: object) -> None:
        """Drop instruments from the live wire set (no-op for paper broker)."""

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _register(self, instrument: Instrument) -> None:
        self._instruments[str(instrument.instrument_id)] = instrument

    def _can_fill(self, order: Order) -> bool:
        if order.order_type not in (OrderType.STOP, OrderType.STOP_LIMIT):
            return True
        quote = self._quotes.get(str(order.instrument.instrument_id))
        trigger = order.trigger_price
        if quote is None or trigger is None:
            return False
        if order.side is OrderSide.BUY:
            return quote.ltp.value >= trigger.value
        return quote.ltp.value <= trigger.value

    def _fill_price(self, order: Order) -> Price:
        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            if order.price is None:
                raise OrderRejectedError(f"{order.order_type.value} requires price")
            return order.price
        quote = self._quotes.get(str(order.instrument.instrument_id))
        if quote is None:
            raise ValueError("market or triggered stop order requires a tape quote")
        # Order-book mode: MARKET walks the book (ask for BUY, bid for SELL)
        if self._order_book_enabled:
            depth = self._depths.get(str(order.instrument.instrument_id))
            if depth is not None:
                if order.side is OrderSide.BUY and depth.asks:
                    return depth.asks[0][0]
                if order.side is OrderSide.SELL and depth.bids:
                    return depth.bids[0][0]
        return quote.ltp

    def _try_fill_against_book(self, order: Order) -> None:
        """Try to fill an order against the multi-level order book.

        Walks book levels within the order's price bound, supporting partial
        fills. Consumed levels are removed from the book.
        """
        # Stop orders: check trigger first
        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            quote = self._quotes.get(str(order.instrument.instrument_id))
            trigger = order.trigger_price
            if quote is None or trigger is None:
                return
            if order.side is OrderSide.BUY and quote.ltp.value < trigger.value:
                return
            if order.side is OrderSide.SELL and quote.ltp.value > trigger.value:
                return

        inst_key = str(order.instrument.instrument_id)
        depth = self._depths.get(inst_key)
        if depth is None:
            # No book — fall back to quote-based fill if possible
            if self._can_fill(order):
                self._fill_order(order)
            return

        remaining_qty = order.quantity.value
        if order.status is OrderStatus.PARTIALLY_FILLED:
            remaining_qty = order.quantity.value - order.filled_quantity.value
        if remaining_qty <= Decimal("0"):
            return

        # Determine which side of the book to walk
        if order.side is OrderSide.BUY:
            levels = list(depth.asks)
            # BUY: limit price is the max we'll pay; MARKET has no limit
            limit = order.price.value if order.price is not None else Decimal("Infinity")
        else:
            levels = list(depth.bids)
            # SELL: limit price is the min we'll accept; MARKET has no limit
            limit = order.price.value if order.price is not None else Decimal("0")

        filled_qty = Decimal("0")
        total_cost = Decimal("0")
        levels_to_keep: list[tuple[Price, Quantity]] = []

        for level_price, level_qty in levels:
            price_val = level_price.value
            # Check if this level is within the order's price bound
            if order.side is OrderSide.BUY and price_val > limit:
                levels_to_keep.append((level_price, level_qty))
                continue
            if order.side is OrderSide.SELL and price_val < limit:
                levels_to_keep.append((level_price, level_qty))
                continue

            # Fill against this level
            fill_qty = min(remaining_qty, level_qty.value)
            filled_qty += fill_qty
            total_cost += price_val * fill_qty
            remaining_qty -= fill_qty

            if fill_qty < level_qty.value:
                # Partial level fill — keep the remainder
                levels_to_keep.append((level_price, Quantity(value=level_qty.value - fill_qty)))

            if remaining_qty <= Decimal("0"):
                break

        if filled_qty <= Decimal("0"):
            return

        # Marginal fill price for THIS increment (correct cash impact — C1 fix).
        # The overall weighted average (across all fills) is tracked separately
        # for position bookkeeping and stored in avg_price_traded.
        marginal_price = total_cost / filled_qty if filled_qty > Decimal("0") else Decimal("0")

        # Compute the overall weighted-average fill price across all fills.
        # For subsequent partial fills the stored order's price is the original
        # limit (M4 fix), so the previous average comes from avg_price_traded.
        if order.status is OrderStatus.PARTIALLY_FILLED:
            prev_avg = (
                order.avg_price_traded.value
                if order.avg_price_traded is not None
                else (order.price.value if order.price is not None else Decimal("0"))
            )
            prev_qty = order.filled_quantity.value
            overall_avg = (
                (prev_avg * prev_qty + total_cost) / (prev_qty + filled_qty)
                if (prev_qty + filled_qty) > Decimal("0")
                else marginal_price
            )
        else:
            overall_avg = marginal_price

        # The gates run *before* the book is touched.  A refused fill never
        # traded, so it must not have taken liquidity off the book either;
        # checking after the mutation would need a rollback, and getting that
        # rollback wrong silently thins the book and makes every later order
        # look partially filled for no reason.  ``depth`` is the untouched
        # pre-fill snapshot, so ordering this ahead of the mutation is both
        # simpler and exact.
        self._require_market_session()
        incremental_notional = total_cost
        if order.side is OrderSide.BUY:
            self._require_buying_power(incremental_notional, order)

        # Update the book
        if order.side is OrderSide.BUY:
            new_depth = Depth(
                instrument=depth.instrument,
                bids=depth.bids,
                asks=tuple(levels_to_keep),
                timestamp=depth.timestamp,
            )
        else:
            new_depth = Depth(
                instrument=depth.instrument,
                bids=tuple(levels_to_keep),
                asks=depth.asks,
                timestamp=depth.timestamp,
            )
        self._depths[inst_key] = new_depth

        # Apply the fill
        if filled_qty >= order.quantity.value:
            # Full fill — keep original limit price, record fill average
            # separately (M4: limit price is never overwritten for LIMIT
            # orders; MARKET orders have no limit so we stamp the fill price
            # so _apply_fill can compute cash impact).
            filled = order.transition_to(OrderStatus.FILLED)
            fill_price_for_order = (
                Price(value=overall_avg)
                if order.price is None
                else order.price
            )
            filled = replace(
                filled,
                price=fill_price_for_order,
                avg_price_traded=Price(value=overall_avg),
                filled_quantity=order.quantity,
            )
            cash_before = self._cash
            try:
                self._apply_fill(filled)
            except Exception:
                self._cash = cash_before
                raise
            self._orders[order.order_id.value] = filled
        else:
            # Partial fill — apply only the incremental fill.
            # The incremental uses the marginal price so cash impact is
            # correct (C1: marginal_price * filled_qty, not overall avg).
            # Position math in _update_position re-derives the correct
            # weighted average from (old_avg, old_qty, marginal_price, filled_qty).
            if order.status is OrderStatus.PARTIALLY_FILLED:
                new_filled_qty = order.filled_quantity.value + filled_qty
            else:
                new_filled_qty = filled_qty
            partial = replace(
                order,
                status=OrderStatus.PARTIALLY_FILLED,
                # MARKET orders have no limit price; stamp the marginal fill
                # price so subsequent partial fills can compute prev_avg.
                price=(
                    Price(value=marginal_price)
                    if order.price is None
                    else order.price
                ),
                avg_price_traded=Price(value=overall_avg),
                filled_quantity=Quantity(value=new_filled_qty),
            )
            cash_before = self._cash
            try:
                incremental = replace(
                    order,
                    price=Price(value=marginal_price),
                    avg_price_traded=Price(value=overall_avg),
                    filled_quantity=Quantity(value=filled_qty),
                )
                self._apply_fill(incremental)
            except Exception:
                self._cash = cash_before
                raise
            self._orders[order.order_id.value] = partial

    def _fill_order(self, order: Order) -> None:
        filled = order.transition_to(OrderStatus.FILLED)
        filled = replace(
            filled,
            price=self._fill_price(order),
            filled_quantity=order.quantity,
        )
        cash_before = self._cash
        try:
            self._apply_fill(filled)
        except Exception:
            self._cash = cash_before
            raise
        self._orders[order.order_id.value] = filled

    def _apply_fill(self, order: Order) -> None:
        """Update the cash ledger and optionally project the position.

        Both realism gates live here rather than in the fill paths, because
        this is the single choke point every fill funnels through — the
        unconditional path (:meth:`_fill_order`) and the book-walking path
        (:meth:`_try_fill_against_book`) both land on it, full and partial
        alike.  A gate added to one path only is how the other path leaks.

        Ordering matters: **both gates run before any state is mutated**, so
        a refusal leaves cash and positions exactly as they were.  The
        callers still wrap this in a cash rollback for defence in depth, but
        the gates no longer depend on it.
        """
        if order.price is None:
            raise ValueError("filled paper order requires a price")
        self._require_market_session()
        notional = order.price.value * order.filled_quantity.value
        if order.side is OrderSide.BUY:
            self._require_buying_power(notional, order)
            self._cash -= notional
        else:
            self._cash += notional
        if self._project_positions:
            self._update_position(order)

    def _require_market_session(self) -> None:
        """Refuse a fill outside the NSE/BSE cash session (C5c).

        A live venue does not fill a resting order at 3am on a Sunday, so a
        simulator that does will book fills — and therefore approve
        strategies — that could never have happened.  The calendar is the
        shared :class:`~tradex_domain.trading_calendar.NSETradingCalendar`
        (09:15-15:30 IST, weekdays, minus the 2026 NSE holiday list), not a
        local literal, so paper and live agree on when the market is shut.

        Refused means **not filled**: the fill is skipped, not turned into a
        rejection the caller must handle.  A caller replaying a recorded
        tape outside session hours gets a resting order that fills when the
        next in-session tick arrives, which is what a live venue does.
        """
        if not self._enforce_session:
            return
        now = self._now_ist()
        if not self._calendar.is_market_open(now):
            raise OrderRejectedError(
                f"market closed at {now.isoformat()}: paper refuses to fill "
                f"outside the NSE/BSE cash session "
                f"(pass enforce_session=False to simulate 24x7)"
            )

    def _require_buying_power(self, notional: Decimal, order: Order) -> None:
        """Refuse a buy the paper cash balance cannot cover (C5b).

        The old code subtracted unconditionally, so 100 lots at 100 on a
        1,000 balance "filled" and drove the balance to -9,000 — an
        unaffordable order booked as a success, which is exactly the kind of
        optimism paper mode must not have.  The engine's ``CashLedger`` is a
        separate, engine-owned check on a separate balance; it does not
        cover this scalar.

        Sells are not gated: a sell adds to cash, and blocking it would stop
        an exit, which is the one thing a risk layer should never do.
        """
        if not self._enforce_margin:
            return
        required = notional * (Decimal("1") + _MARGIN_BUFFER)
        if required > self._cash:
            raise OrderRejectedError(
                f"insufficient buying power: {order.side.value} "
                f"{order.filled_quantity.value} x {order.price.value} needs "
                f"{required} (incl. {_MARGIN_BUFFER:.0%} margin), "
                f"cash is {self._cash} (order {order.order_id.value})"
            )

    def _now_ist(self) -> datetime:
        """Return the current time as a tz-naive IST datetime.

        :meth:`NSETradingCalendar.is_market_open` compares wall-clock bounds
        (09:15/15:30) and derives the trading day from ``dt.date()``, so it
        needs IST and no tzinfo.  A naive value from an injected clock is
        treated as already-IST rather than reinterpreted, which keeps a
        pinned test clock from shifting across the session boundary.
        """
        return to_ist_naive(self._clock())

    def _update_position(self, order: Order) -> None:
        """Update position for an instrument after a fill.

        Delegates to the canonical position-accounting function in
        :mod:`tradex_domain.position_math` (M8 fix — no longer duplicates
        the weighted-average math from trading/position_math).
        """
        instrument_id = str(order.instrument.instrument_id)
        current = self._positions.get(instrument_id)

        # ``Fill.price`` is required, but ``Order.price`` is optional; the only
        # caller (``_apply_fill``) has already rejected a price-less fill by the
        # time it gets here. Re-checking and narrowing keeps the type honest
        # without changing behaviour (the branch is unreachable via _apply_fill).
        price = order.price
        if price is None:
            raise ValueError("filled paper order requires a price")
        fill = Fill(
            order_id=order.order_id,
            instrument=order.instrument,
            side=order.side,
            quantity=order.filled_quantity,
            price=price,
            timestamp=datetime.now(UTC),
            # The paper book is the one place a DELIVERY order is guaranteed to
            # be affordable to inspect, so the label belongs here too: a fill
            # with no product is priced as INTRADAY and counted.
            product_type=order.product_type,
        )
        self._positions[instrument_id] = _domain_apply_fill(current, fill)

    @property
    def synchronous_fill(self) -> bool:
        """Whether a fill is observable by the time a mutation call returns.

        Always ``True`` for paper, and the guarantee is narrow and
        mechanical (C5e).  **What it means:** when ``submit_order`` /
        ``set_quote`` / ``set_depth`` returns, any fill that was going to
        happen against the state they installed has already been applied to
        ``self._orders``, ``self._positions`` and the cash balance.  There is
        no background thread, no queue and no network hop, so a read of
        ``get_order`` immediately afterwards cannot miss a fill that paper
        had already decided on.  That is what makes paper's determinism
        usable in a test.

        **What it does NOT mean — do not read this as a live guarantee:**

        * It is **not** a statement that a fill *will* occur.  A fill also
          requires a matching quote/depth (``auto_fill``), an open market
          session, and enough buying power.  "Synchronous" describes *when*
          a decided fill lands, never *whether* one happens.
        * It is **not** latency modelling.  Paper has zero modelled
          round-trip: no order-ack delay, no exchange latency, no partial
          fill over time, no rejects that arrive a second later.  A strategy
          tuned against paper therefore has not been stress-tested against
          any of those, and "synchronous" must not be read as "fast enough
          that latency does not matter".
        * It does **not** make read-after-write safe in the live sense.  A
          live venue ACKs an order with no fill, then delivers the fill
          later over a stream that can be reordered, duplicated or lost.
          Code that skips its fill subscription because the paper flag said
          ``True`` will silently book nothing live.  Consume fills from the
          fill stream regardless of this flag; treat it as a paper-only
          testing affordance.
        """
        return True

    @property
    def owns_position_projection(self) -> bool:
        """Whether this adapter applies filled orders to its own OMS cache."""
        return self._project_positions

    @property
    def trading_cache(self) -> TradingCacheProtocol:
        """The broker-owned projection cache used by runtime boot."""
        return self._cache

    def configure_runtime_cache(self, cache: TradingCacheProtocol) -> None:
        """Use a runtime OMS cache while preserving existing projected state."""
        if cache is self._cache:
            self._project_positions = False
            return
        if self._cache is not None and hasattr(self._cache, "snapshot"):
            snapshot = self._cache.snapshot()
            if hasattr(cache, "restore"):
                cache.restore(snapshot)
        self._cache = cache
        self._project_positions = False


__all__ = ["PaperBroker", "PaperHistoryNotImplemented"]
