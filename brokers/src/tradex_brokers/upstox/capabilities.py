"""Upstox capability table — broker-side facts.

Extracted from ``common/capabilities.py`` so each broker owns its truth table.
"""

from __future__ import annotations

from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.enums import AssetClass


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
