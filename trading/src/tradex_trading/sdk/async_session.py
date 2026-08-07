"""Async trading session — async wrapper around TradingSession.

Provides async/await interface for all trading operations using asyncio.to_thread
for non-blocking I/O. Includes async stream bridges via asyncio.Queue.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar

from tradex_domain import BrokerId
from tradex_domain.events import OrderFilled
from tradex_domain.execution import (
    Account,
    Order,
    OrderReceipt,
    OrderRequest,
    PortfolioSnapshot,
    Position,
)
from tradex_domain.instruments import Instrument
from tradex_domain.market import Depth, HistoricalSeries, Quote
from tradex_domain.options import OptionChain
from tradex_domain.value_objects import OrderId, Price

from tradex_trading.sdk.session import SessionState, TradingSession

T = TypeVar("T")


class AsyncTradingSession:
    """Async wrapper around TradingSession.

    Provides async/await interface for all trading operations.
    Useful for Jupyter notebooks and async applications.

    Example
    -------
    ```python
    async with AsyncTradingSession(session) as aSession:
        quote = await aSession.quote(instrument)
        print(quote)
    ```
    """

    def __init__(self, session: TradingSession) -> None:
        """Initialize async session wrapper.

        Parameters
        ----------
        session : TradingSession
            The underlying sync session to wrap.
        """
        self._session = session

    async def __aenter__(self) -> AsyncTradingSession:
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Async context manager exit."""
        await self.stop()

    async def stop(self) -> None:
        """Stop the session."""
        await asyncio.to_thread(self._session.stop)

    @property
    def state(self) -> SessionState:
        """Current session state."""
        return self._session.state

    @property
    def broker_id(self) -> BrokerId:
        """Broker identifier."""
        return self._session.broker_id

    @property
    def session(self) -> TradingSession:
        """Access the underlying sync session."""
        return self._session

    # -- Market service (async) -----------------------------------------------

    async def quote(self, instrument: Instrument) -> Quote:
        """Get a quote for an instrument."""
        return await asyncio.to_thread(self._session.market.quote, instrument)

    async def ltp(self, instrument: Instrument) -> Price:
        """Get last traded price."""
        return await asyncio.to_thread(self._session.market.ltp, instrument)

    async def depth(self, instrument: Instrument) -> Depth:
        """Get market depth."""
        return await asyncio.to_thread(self._session.market.depth, instrument)

    async def history(
        self,
        instrument: Instrument,
        timeframe: Any,
        start: Any,
        end: Any,
    ) -> HistoricalSeries:
        """Get historical data."""
        return await asyncio.to_thread(
            self._session.market.history, instrument, timeframe, start, end
        )

    async def search(self, query: str) -> list[Instrument]:
        """Search instruments by symbol substring."""
        return await asyncio.to_thread(self._session.market.search, query)

    async def option_chain(
        self,
        underlying: Instrument,
        expiry: Any = None,
    ) -> OptionChain:
        """Get option chain for an underlying."""
        return await asyncio.to_thread(
            self._session.market.option_chain, underlying, expiry
        )

    async def ltp_batch(self, instruments: list[Instrument]) -> dict:
        """Batch LTP lookup."""
        return await asyncio.to_thread(self._session.market.ltp_batch, instruments)

    async def quote_batch(self, instruments: list[Instrument]) -> dict:
        """Batch quote lookup."""
        return await asyncio.to_thread(self._session.market.quote_batch, instruments)

    async def future_chain(self, underlying: Instrument) -> list:
        """Get future chain for an underlying."""
        return await asyncio.to_thread(self._session.market.future_chain, underlying)

    async def news(
        self,
        category: str,
        *,
        instrument_keys: list[str] | None = None,
        page_number: int | None = None,
        page_size: int | None = None,
    ) -> list[dict[str, object]]:
        """Get news headlines (requires ``supports_news``)."""
        return await asyncio.to_thread(
            self._session.market.news,
            category,
            instrument_keys=instrument_keys,
            page_number=page_number,
            page_size=page_size,
        )

    # -- Trade service (async) ------------------------------------------------

    async def submit(self, request: OrderRequest) -> OrderReceipt:
        """Submit an order."""
        return await asyncio.to_thread(self._session.trade.submit, request)

    async def cancel(self, order_id: Any) -> Order:
        """Cancel an order."""
        return await asyncio.to_thread(self._session.trade.cancel, order_id)

    async def get_orderbook(self) -> list[Order]:
        """Get order book."""
        return await asyncio.to_thread(self._session.trade.get_orderbook)

    async def get_order(self, order_id: OrderId) -> Order | None:
        """Get a single order by ID."""
        return await asyncio.to_thread(self._session.trade.get_order, order_id)

    # -- Portfolio service (async) --------------------------------------------

    async def positions(self) -> list[Position]:
        """Get all positions."""
        return await asyncio.to_thread(self._session.portfolio.positions)

    async def account(self) -> Account:
        """Get account info."""
        return await asyncio.to_thread(self._session.portfolio.account)

    async def portfolio(self) -> PortfolioSnapshot:
        """Get portfolio snapshot."""
        return await asyncio.to_thread(self._session.portfolio.portfolio)

    # -- Scanner service (async) ----------------------------------------------

    async def scanner_run(self, definition: Any) -> list:
        """Run a scanner definition."""
        return await asyncio.to_thread(self._session.scanner.run, definition)

    async def scanner_top(self, definition: Any, limit: int = 20) -> list:
        """Get top scanner results."""
        return await asyncio.to_thread(self._session.scanner.top, definition, limit)

    # -- Analytics service (async) --------------------------------------------

    async def indicator(self, series: Any, names: list[str], **params: Any) -> Any:
        """Compute technical indicators."""
        return await asyncio.to_thread(
            self._session.analytics.indicators, series, names, **params
        )

    # -- Extension service (async) --------------------------------------------

    async def kill_switch(self, enable: bool = True) -> Any:
        """Enable or disable the kill switch."""
        return await asyncio.to_thread(self._session.extension.kill_switch, enable)

    async def status_kill_switch(self) -> Any:
        """Get kill switch status."""
        return await asyncio.to_thread(self._session.extension.status_kill_switch)

    # -- Async stream bridges -------------------------------------------------

    async def subscribe_quotes_async(
        self, handler: Callable[[Quote], None] | None = None
    ) -> AsyncIterator[Quote]:
        """Subscribe to quote stream as an async iterator.

        If handler is provided, it's called for each quote. Otherwise, quotes
        are yielded via the async iterator.
        """
        queue: asyncio.Queue[Quote] = asyncio.Queue()

        def _on_quote(quote: Quote) -> None:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(queue.put_nowait, quote)
            if handler is not None:
                handler(quote)

        sub = await asyncio.to_thread(self._session.stream.subscribe_quotes, _on_quote)
        try:
            while True:
                quote = await queue.get()
                yield quote
        finally:
            await asyncio.to_thread(self._session.stream.unsubscribe, sub)

    async def subscribe_fills_async(
        self, handler: Callable[[OrderFilled], None] | None = None
    ) -> AsyncIterator[OrderFilled]:
        """Subscribe to fill stream as an async iterator."""
        queue: asyncio.Queue[OrderFilled] = asyncio.Queue()

        def _on_fill(fill: OrderFilled) -> None:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(queue.put_nowait, fill)
            if handler is not None:
                handler(fill)

        sub = await asyncio.to_thread(self._session.stream.subscribe_fills, _on_fill)
        try:
            while True:
                fill = await queue.get()
                yield fill
        finally:
            await asyncio.to_thread(self._session.stream.unsubscribe, sub)

    async def subscribe_depth_async(
        self, handler: Callable[[Depth], None] | None = None
    ) -> AsyncIterator[Depth]:
        """Subscribe to depth stream as an async iterator."""
        queue: asyncio.Queue[Depth] = asyncio.Queue()

        def _on_depth(depth: Depth) -> None:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(queue.put_nowait, depth)
            if handler is not None:
                handler(depth)

        sub = await asyncio.to_thread(self._session.stream.subscribe_depth, _on_depth)
        try:
            while True:
                depth = await queue.get()
                yield depth
        finally:
            await asyncio.to_thread(self._session.stream.unsubscribe, sub)


__all__ = ["AsyncTradingSession"]
