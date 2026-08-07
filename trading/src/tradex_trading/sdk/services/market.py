"""MarketService — quotes, depth, history, batch, search, chains, news."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Protocol, runtime_checkable

from tradex_domain.capabilities import BrokerCapabilities, require_capability
from tradex_domain.enums import Timeframe
from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.instruments import Instrument
from tradex_domain.market import Depth, HistoricalSeries, Quote, require_depth_supported
from tradex_domain.options import OptionChain
from tradex_domain.protocols import BrokerAdapter
from tradex_domain.value_objects import InstrumentId, Price


@runtime_checkable
class BatchMarketAdapter(Protocol):
    """Optional batch market-data surface (live brokers only).

    Kept separate from ``BrokerAdapter`` so Paper's runtime_checkable
    conformance to the base protocol is unaffected.
    """

    def ltp_batch(self, instruments: Sequence[Instrument]) -> dict[InstrumentId, Price]: ...

    def quote_batch(self, instruments: Sequence[Instrument]) -> dict[InstrumentId, Quote]: ...


@runtime_checkable
class FutureChainAdapter(Protocol):
    """Optional futures-chain surface (live brokers only)."""

    def future_chain(self, underlying: Instrument) -> Sequence[object]: ...


@runtime_checkable
class NewsAdapter(Protocol):
    """Optional news/headlines surface (live brokers only)."""

    def get_news(
        self,
        category: str,
        *,
        instrument_keys: list[str] | None = None,
        page_number: int | None = None,
        page_size: int | None = None,
    ) -> list[dict[str, object]]: ...


class MarketService:
    """Quote/ltp/depth/history/search/option-chain/news (D-4/D-5)."""

    def __init__(self, broker: BrokerAdapter, capabilities: BrokerCapabilities) -> None:
        self._broker = broker
        self._capabilities = capabilities

    def quote(self, instrument: Instrument) -> Quote:
        """Get full quote for instrument."""
        return self._broker.get_quote(instrument)

    def ltp(self, instrument: Instrument) -> Price:
        """Get last traded price for instrument."""
        return self._broker.ltp(instrument)

    def depth(self, instrument: Instrument) -> Depth:
        """Get market depth for instrument (NSE only)."""
        require_depth_supported(instrument)
        return self._broker.depth(instrument)

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
        """Get historical data for *instrument*.

        Canonical form: ``history(instrument, timeframe, start, end)``.
        Convenience form: ``history(instrument, interval="5m", lookback_days=5)``
        — or ``lookback_days`` with a ``timeframe`` — computes the window
        ending now. When no window is given, defaults to a 30-day lookback
        (e.g. the HTTP route's ``history(iid, tf)`` call).
        """
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
        return self._broker.history(instrument, timeframe, start, end)

    def ltp_batch(self, instruments: Sequence[Instrument]) -> dict[InstrumentId, Price]:
        """Batch LTP lookup (requires ``supports_batch_market_data``)."""
        require_capability(self._capabilities, "supports_batch_market_data")
        broker = self._broker
        if not isinstance(broker, BatchMarketAdapter):
            raise CapabilityNotSupportedError("broker does not support batch market data")
        return broker.ltp_batch(instruments)

    def quote_batch(self, instruments: Sequence[Instrument]) -> dict[InstrumentId, Quote]:
        """Batch quote lookup (requires ``supports_batch_market_data``)."""
        require_capability(self._capabilities, "supports_batch_market_data")
        broker = self._broker
        if not isinstance(broker, BatchMarketAdapter):
            raise CapabilityNotSupportedError("broker does not support batch market data")
        return broker.quote_batch(instruments)

    def search(self, query: str) -> list[Instrument]:
        """Search instruments by symbol substring."""
        return list(self._broker.search(query))

    def option_chain(
        self,
        underlying: Instrument,
        expiry: date | str | None = None,
    ) -> OptionChain:
        """Full option chain for *underlying* (requires ``supports_option_chain``)."""
        require_capability(self._capabilities, "supports_option_chain")
        if expiry is None:
            return self._broker.get_option_chain(underlying)
        return self._broker.get_option_chain(underlying, expiry)

    def future_chain(self, underlying: Instrument) -> list:
        """Futures on *underlying* (requires ``supports_future_chain``)."""
        require_capability(self._capabilities, "supports_future_chain")
        broker = self._broker
        if not isinstance(broker, FutureChainAdapter):
            raise CapabilityNotSupportedError("broker does not support future chain")
        return list(broker.future_chain(underlying))

    def news(
        self,
        category: str,
        *,
        instrument_keys: list[str] | None = None,
        page_number: int | None = None,
        page_size: int | None = None,
    ) -> list[dict[str, object]]:
        """News headlines (requires ``supports_news``).

        *category* is broker-specific — Upstox accepts ``"instrument_keys"``,
        ``"positions"``, or ``"holdings"``.
        """
        require_capability(self._capabilities, "supports_news")
        broker = self._broker
        if not isinstance(broker, NewsAdapter):
            raise CapabilityNotSupportedError("broker does not support news")
        return broker.get_news(
            category,
            instrument_keys=instrument_keys,
            page_number=page_number,
            page_size=page_size,
        )


__all__ = ["MarketService"]
