"""Dhan capability table — broker-side facts.

Extracted from ``common/capabilities.py`` so each broker owns its truth table.
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
        supports_same_day_intraday=True,
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
