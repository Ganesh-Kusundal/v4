"""Broker capability matrix (§3/§4, D-14..D-16).

``BrokerCapabilities`` defaults are fail-closed: nothing is claimed unless the
broker's truth table explicitly enables it. ``require_capability`` is the
single capability-loud gate used by the SDK services (D-8).
"""

from __future__ import annotations

from dataclasses import dataclass

from tradex_domain.enums import AssetClass
from tradex_domain.errors import CapabilityNotSupportedError


@dataclass(frozen=True, slots=True)
class BrokerCapabilities:
    """Declarative truth table a broker adapter must fill in (fail-closed defaults)."""

    supports_market_order: bool = False
    supports_limit_order: bool = False
    supports_stop_order: bool = False
    supports_modify: bool = False
    supports_super_order: bool = False
    supports_forever_order: bool = False
    supports_slice_order: bool = False
    supports_edis: bool = False
    supports_batch_market_data: bool = False
    supports_portfolio_stream: bool = False
    supports_option_chain: bool = False
    supports_future_chain: bool = False
    supports_kill_switch: bool = False
    supports_news: bool = False
    supports_fundamentals: bool = False
    #: Levels in the deepest market-depth stream (0 = no depth feed).
    #: Dhan depth-20 -> 20; Upstox full_d30 -> 30.
    depth_levels: int = 0
    #: Instruments a single live WebSocket connection may carry (None = no
    #: known cap). Dhan quotes cap at 1000 per connection; Upstox at 500.
    max_stream_instruments: int | None = None
    supported_asset_classes: tuple[AssetClass, ...] = (AssetClass.EQUITY,)


def dhan_capabilities() -> BrokerCapabilities:
    return BrokerCapabilities(
        supports_market_order=True,
        supports_limit_order=True,
        supports_stop_order=True,
        supports_modify=True,
        supports_super_order=True,
        supports_forever_order=True,
        supports_slice_order=True,
        supports_edis=True,
        supports_batch_market_data=True,
        supports_portfolio_stream=False,
        supports_option_chain=True,
        supports_future_chain=True,
        supports_kill_switch=True,
        supports_news=False,
        supports_fundamentals=False,
        depth_levels=20,
        max_stream_instruments=1000,
        supported_asset_classes=(
            AssetClass.EQUITY,
            AssetClass.INDEX,
            AssetClass.FUTURE,
            AssetClass.OPTION,
            AssetClass.CURRENCY,
            AssetClass.COMMODITY,
        ),
    )


def upstox_capabilities() -> BrokerCapabilities:
    return BrokerCapabilities(
        supports_market_order=True,
        supports_limit_order=True,
        supports_stop_order=True,
        supports_modify=True,
        supports_super_order=False,
        supports_forever_order=True,
        supports_slice_order=True,
        supports_edis=False,
        supports_batch_market_data=True,
        supports_portfolio_stream=True,
        supports_option_chain=True,
        supports_future_chain=True,
        supports_kill_switch=True,
        supports_news=True,
        supports_fundamentals=False,
        depth_levels=30,
        max_stream_instruments=500,
        supported_asset_classes=(
            AssetClass.EQUITY,
            AssetClass.INDEX,
            AssetClass.FUTURE,
            AssetClass.OPTION,
        ),
    )


def paper_capabilities() -> BrokerCapabilities:
    return BrokerCapabilities(
        supports_market_order=True,
        supports_limit_order=True,
        supports_stop_order=True,
        supports_modify=True,
        supports_super_order=False,
        supports_forever_order=False,
        supports_slice_order=False,
        supports_edis=False,
        supports_batch_market_data=False,
        supports_portfolio_stream=False,
        supports_option_chain=False,
        supports_future_chain=False,
        supports_kill_switch=False,
        supports_news=False,
        supports_fundamentals=False,
        supported_asset_classes=(
            AssetClass.EQUITY,
            AssetClass.INDEX,
            AssetClass.FUTURE,
            AssetClass.OPTION,
        ),
    )


def require_capability(capabilities: BrokerCapabilities, name: str) -> None:
    """Raise ``CapabilityNotSupportedError`` when the named flag is false (D-8)."""
    if not getattr(capabilities, name):
        raise CapabilityNotSupportedError(f"broker does not support capability: {name}")


__all__ = [
    "BrokerCapabilities",
    "dhan_capabilities",
    "paper_capabilities",
    "require_capability",
    "upstox_capabilities",
]
