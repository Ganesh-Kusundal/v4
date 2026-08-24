"""Provider capability tables (broker-side facts).

Each broker owns its truth table; the *mechanism* (``BrokerCapabilities``,
``require_capability``) lives in the domain. Defaults are fail-closed: nothing
is claimed unless the broker's table explicitly enables it.
"""

from __future__ import annotations

from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.enums import AssetClass


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


__all__ = [
    "dhan_capabilities",
    "paper_capabilities",
    "upstox_capabilities",
]
