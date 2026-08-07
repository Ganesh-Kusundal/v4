"""Capability-gated broker-extra protocols for the SDK services (D-4/D-5).

These express the optional broker surfaces that SDK services reach for behind a
``BrokerCapabilities`` gate (``supports_batch_market_data``,
``supports_future_chain``). They are ``runtime_checkable`` so a consumer can
bind a broker structurally with ``isinstance`` instead of ``cast``.

Kept out of ``tradex_domain`` on purpose: they describe capabilities only the
trading SDK consumes, not the domain core's full adapter contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from tradex_domain.instruments import Instrument
from tradex_domain.market import Quote
from tradex_domain.value_objects import InstrumentId, Price

__all__ = ["BatchMarketAdapter", "FutureChainAdapter"]


@runtime_checkable
class BatchMarketAdapter(Protocol):
    """Optional batch market-data surface (live brokers only).

    Consumed by :class:`tradex_trading.sdk.services.market.MarketService` behind
    the ``supports_batch_market_data`` capability gate.
    """

    def ltp_batch(self, instruments: Sequence[Instrument]) -> dict[InstrumentId, Price]: ...

    def quote_batch(self, instruments: Sequence[Instrument]) -> dict[InstrumentId, Quote]: ...


@runtime_checkable
class FutureChainAdapter(Protocol):
    """Optional futures-chain surface (live brokers only).

    Consumed by :class:`tradex_trading.sdk.services.market.MarketService` behind
    the ``supports_future_chain`` capability gate.
    """

    def future_chain(self, underlying: Instrument) -> Sequence[object]: ...
