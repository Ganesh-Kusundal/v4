"""Paper (in-memory simulation) broker adapter.

Provides a fully self-contained ``BrokerAdapter`` implementation that keeps
all state in memory — no external broker connection required. Useful for
unit / integration tests and paper-trading sessions.

Supports auto-fill against a seeded tape, position projection, and cash
ledger management.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from tradex_domain.capabilities import BrokerCapabilities, require_capability
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
from tradex_domain.options import OptionChain
from tradex_domain.position_math import apply_fill as _domain_apply_fill
from tradex_domain.protocols import TradingCacheProtocol
from tradex_domain.value_objects import AccountId, InstrumentId, Money, OrderId, Price, Quantity

from tradex_brokers.common.capabilities import paper_capabilities


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


class PaperBroker:
    """In-memory simulation broker satisfying the ``BrokerAdapter`` protocol."""

    def __init__(
        self,
        starting_cash: Decimal | None = None,
        auto_fill: bool = False,
        project_positions: bool = True,
        order_book: bool = False,
    ) -> None:
        self.capabilities: BrokerCapabilities = paper_capabilities()
        self._auto_fill = auto_fill
        self._project_positions = project_positions
        self._order_book_enabled = order_book
        self._cash = starting_cash if starting_cash is not None else _PAPER_STARTING_CASH
        self._quotes: dict[str, Quote] = {}
        self._depths: dict[str, Depth] = {}
        self._instruments: dict[str, Instrument] = {}
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._connected = False
        self._state_lock = threading.RLock()
        self._cache: TradingCacheProtocol = _PaperCache()
        self.connect()  # paper broker is connected on construction

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Mark the broker as connected."""
        self._connected = True

    def close(self) -> None:
        """Mark the broker as disconnected."""
        self._connected = False

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
            if self._order_book_enabled:
                self._try_fill_against_book(order)
            elif self._can_fill(order):
                self._fill_order(order)
        # Check if the order was filled (status changed from ACK)
        stored = self._orders.get(order_id.value)
        if stored is None or stored.status is OrderStatus.ACK:
            # Not filled — store the ACK order
            self._orders[order_id.value] = order
        return order_id

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
                    self._try_fill_against_book(order)

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
                if self._order_book_enabled:
                    self._try_fill_against_book(order)
                elif self._can_fill(order):
                    self._fill_order(order)

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
        """Historical data — canonical + convenience signatures matching
        ``BaseBroker`` so the REST API and cross-provider callers can use the
        same 2-arg/``interval`` form on every adapter."""
        if interval is not None:
            timeframe = Timeframe(interval)
        if timeframe is None:
            raise ValueError("history requires a timeframe or interval")
        if not isinstance(timeframe, Timeframe):
            timeframe = Timeframe(timeframe)
        if start is None or end is None:
            if lookback_days is None:
                lookback_days = 30
            end = end or datetime.now(UTC)
            start = start or (end - timedelta(days=lookback_days))
        return HistoricalSeries(
            instrument=instrument,
            timeframe=timeframe,
            candles=[],
            start=start,
            end=end,
        )

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

    def load_instruments(self) -> None:
        """No-op — paper broker has no external instrument source."""

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
        """Update the cash ledger and optionally project the position."""
        if order.price is None:
            raise ValueError("filled paper order requires a price")
        notional = order.price.value * order.filled_quantity.value
        if order.side is OrderSide.BUY:
            self._cash -= notional
        else:
            self._cash += notional
        if self._project_positions:
            self._update_position(order)

    def _update_position(self, order: Order) -> None:
        """Update position for an instrument after a fill.

        Delegates to the canonical position-accounting function in
        :mod:`tradex_domain.position_math` (M8 fix — no longer duplicates
        the weighted-average math from trading/position_math).
        """
        instrument_id = str(order.instrument.instrument_id)
        current = self._positions.get(instrument_id)

        fill = Fill(
            order_id=order.order_id,
            instrument=order.instrument,
            side=order.side,
            quantity=order.filled_quantity,
            price=order.price,
            timestamp=datetime.now(UTC),
        )
        self._positions[instrument_id] = _domain_apply_fill(current, fill)

    @property
    def synchronous_fill(self) -> bool:
        """Paper fills are deterministic; live adapters must not use read-after-write."""
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


__all__ = ["PaperBroker"]
