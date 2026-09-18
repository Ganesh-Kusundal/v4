"""Paper broker capability table — broker-side facts.

Extracted from ``common/capabilities.py`` so each broker owns its truth table.
"""

from __future__ import annotations

from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.enums import AssetClass


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
