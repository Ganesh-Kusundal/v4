"""Protocol-based type safety in SDK services.

``MarketService`` guards its optional batch/future-chain surface with
``runtime_checkable`` protocols (``BatchMarketAdapter``, ``FutureChainAdapter``).
When a broker *claims* the supporting capability but does not actually
implement the protocol surface, the service must fail closed with
``CapabilityNotSupportedError`` rather than ``AttributeError``.

These tests exercise the protocol conformance gates directly (no full
TradingSession needed).
"""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal
from typing import Any

import pytest
from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.instruments import Equity
from tradex_domain.market import Depth, HistoricalSeries, Quote
from tradex_domain.options import OptionChain
from tradex_domain.value_objects import InstrumentId, Price

from tradex_trading.sdk.services.market import (
    BatchMarketAdapter,
    FutureChainAdapter,
    MarketService,
)


def _equity() -> Equity:
    return Equity.of("NSE", "RELIANCE")


class _BaseBroker:
    """Conforms to BrokerAdapter but to NO optional batch/future surface."""

    capabilities = BrokerCapabilities(
        supports_batch_market_data=True,
        supports_future_chain=True,
        supports_option_chain=True,
    )

    def get_quote(self, instrument: Equity) -> Quote:
        return Quote(instrument=instrument, ltp=Price(value=Decimal("100")))

    def ltp(self, instrument: Equity) -> Price:
        return Price(value=Decimal("100"))

    def depth(self, instrument: Equity) -> Depth:
        return Depth(instrument=instrument)

    def history(self, instrument: Any, timeframe: Any, start: Any, end: Any) -> HistoricalSeries:
        return HistoricalSeries(
            instrument=instrument, timeframe=timeframe, candles=[], start=start, end=end
        )

    def search(self, query: str) -> list[Equity]:
        return [Equity.of("NSE", query.upper())]

    def get_option_chain(self, underlying: Equity, expiry: Any = None) -> OptionChain:
        return OptionChain(underlying=underlying)


class _BatchBroker(_BaseBroker):
    """Conforms to BrokerAdapter AND BatchMarketAdapter."""

    def ltp_batch(self, instruments: list[Equity]) -> dict[InstrumentId, Price]:
        return {i.instrument_id: Price(value=Decimal("100")) for i in instruments}

    def quote_batch(self, instruments: list[Equity]) -> dict[InstrumentId, Quote]:
        return {
            i.instrument_id: Quote(instrument=i, ltp=Price(value=Decimal("100")))
            for i in instruments
        }


class _FutureBroker(_BaseBroker):
    """Conforms to BrokerAdapter AND FutureChainAdapter."""

    def future_chain(self, underlying: Equity) -> list:
        return [underlying]


class TestProtocolTypeSafety:
    """Batch/future protocol gates fail closed on non-conforming brokers."""

    def test_ltp_batch_requires_batch_protocol(self) -> None:
        """Capability true but broker lacks ltp_batch -> CapabilityNotSupportedError."""
        svc = MarketService(_BaseBroker(), _BaseBroker().capabilities)
        with pytest.raises(CapabilityNotSupportedError):
            svc.ltp_batch([_equity()])

    def test_quote_batch_requires_batch_protocol(self) -> None:
        svc = MarketService(_BaseBroker(), _BaseBroker().capabilities)
        with pytest.raises(CapabilityNotSupportedError):
            svc.quote_batch([_equity()])

    def test_future_chain_requires_future_protocol(self) -> None:
        """Capability true but broker lacks future_chain -> CapabilityNotSupportedError."""
        svc = MarketService(_BaseBroker(), _BaseBroker().capabilities)
        with pytest.raises(CapabilityNotSupportedError):
            svc.future_chain(_equity())

    def test_ltp_batch_succeeds_for_conforming_broker(self) -> None:
        broker = _BatchBroker()
        svc = MarketService(broker, broker.capabilities)
        result = svc.ltp_batch([_equity()])
        assert isinstance(result, dict)
        assert all(isinstance(v, Price) for v in result.values())

    def test_quote_batch_succeeds_for_conforming_broker(self) -> None:
        broker = _BatchBroker()
        svc = MarketService(broker, broker.capabilities)
        result = svc.quote_batch([_equity()])
        assert all(isinstance(v, Quote) for v in result.values())

    def test_future_chain_succeeds_for_conforming_broker(self) -> None:
        broker = _FutureBroker()
        svc = MarketService(broker, broker.capabilities)
        result = svc.future_chain(_equity())
        assert isinstance(result, list)

    def test_capability_gate_still_first(self) -> None:
        """Even a conforming broker is rejected if the capability is off."""
        broker = _BatchBroker()
        caps = BrokerCapabilities(supports_batch_market_data=False)
        svc = MarketService(broker, caps)
        with pytest.raises(CapabilityNotSupportedError):
            svc.ltp_batch([_equity()])

    def test_protocol_runtime_checkability(self) -> None:
        """The optional adapters are runtime_checkable protocols."""
        assert isinstance(_BatchBroker(), BatchMarketAdapter)
        assert isinstance(_FutureBroker(), FutureChainAdapter)
        assert not isinstance(_BaseBroker(), BatchMarketAdapter)
        assert not isinstance(_BaseBroker(), FutureChainAdapter)


class TestMarketServiceCore:
    """Core (non-optional) MarketService methods delegate to the broker."""

    def test_quote(self) -> None:
        svc = MarketService(_BaseBroker(), _BaseBroker().capabilities)
        q = svc.quote(_equity())
        assert isinstance(q, Quote)
        assert q.ltp == Price(value=Decimal("100"))

    def test_ltp(self) -> None:
        svc = MarketService(_BaseBroker(), _BaseBroker().capabilities)
        assert svc.ltp(_equity()) == Price(value=Decimal("100"))

    def test_depth(self) -> None:
        svc = MarketService(_BaseBroker(), _BaseBroker().capabilities)
        assert isinstance(svc.depth(_equity()), Depth)

    def test_history(self) -> None:
        from datetime import datetime
        svc = MarketService(_BaseBroker(), _BaseBroker().capabilities)
        series = svc.history(
            _equity(), "1d",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert isinstance(series, HistoricalSeries)

    def test_search_returns_list_of_instruments(self) -> None:
        svc = MarketService(_BaseBroker(), _BaseBroker().capabilities)
        results = svc.search("RELIANCE")
        assert isinstance(results, list)
        assert isinstance(results[0], Equity)

    def test_option_chain_without_expiry(self) -> None:
        svc = MarketService(_BaseBroker(), _BaseBroker().capabilities)
        assert isinstance(svc.option_chain(_equity()), OptionChain)


class _ExtensionBroker:
    """Conforms to the extension adapter protocols (super/forever/slice/edis/kill)."""

    def submit_super_order(self, request: Any) -> str:
        return "s-1"

    def modify_super_order(self, order_id: Any, request: Any) -> dict:
        return {"order_id": "s-1", "status": "modified"}

    def cancel_super_order(self, order_id: Any, leg: str = "ENTRY") -> dict:
        return {"order_id": "s-1", "status": "cancelled"}

    def list_super_orders(self) -> list[dict]:
        return [{"order_id": "s-1"}]

    def submit_forever_order(self, request: Any) -> str:
        return "f-1"

    def modify_forever_order(self, order_id: Any, request: Any) -> dict:
        return {"order_id": "f-1", "status": "modified"}

    def cancel_forever_order(self, order_id: Any) -> dict:
        return {"order_id": "f-1", "status": "cancelled"}

    def list_forever_orders(self) -> list[dict]:
        return [{"order_id": "f-1"}]

    def submit_slice_order(self, request: Any, slices: int, interval: Any = None) -> list:
        return [f"sl-{i}" for i in range(slices)]

    def submit_edis(self, request: Any) -> str:
        return "e-1"

    def generate_tpin(self) -> dict:
        return {"status": "ok"}

    def edis_status(self, isin: str) -> dict:
        return {"isin": isin, "status": "active"}

    def kill_switch(self, enable: bool = True) -> dict:
        return {"kill_switch": enable}

    def status_kill_switch(self) -> dict:
        return {"kill_switch": False}


class TestExtensionAdapterProtocols:
    """New runtime-checkable extension protocols detect conforming brokers."""

    def test_super_order_adapter_detected(self) -> None:
        from tradex_domain.protocols import SuperOrderAdapter
        assert isinstance(_ExtensionBroker(), SuperOrderAdapter)
        assert not isinstance(_BaseBroker(), SuperOrderAdapter)

    def test_forever_order_adapter_detected(self) -> None:
        from tradex_domain.protocols import ForeverOrderAdapter
        assert isinstance(_ExtensionBroker(), ForeverOrderAdapter)

    def test_slice_order_adapter_detected(self) -> None:
        from tradex_domain.protocols import SliceOrderAdapter
        assert isinstance(_ExtensionBroker(), SliceOrderAdapter)

    def test_edis_adapter_detected(self) -> None:
        from tradex_domain.protocols import EdisAdapter
        assert isinstance(_ExtensionBroker(), EdisAdapter)

    def test_kill_switch_adapter_detected(self) -> None:
        from tradex_domain.protocols import KillSwitchAdapter
        assert isinstance(_ExtensionBroker(), KillSwitchAdapter)

