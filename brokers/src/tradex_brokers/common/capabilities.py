"""Provider capability tables — unified dispatcher."""

from __future__ import annotations

from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.enums import AssetClass, BrokerId


def get_capabilities(broker_id: BrokerId) -> BrokerCapabilities:
    if broker_id == BrokerId.DHAN:
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
    if broker_id == BrokerId.UPSTOX:
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
    if broker_id == BrokerId.PAPER:
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
    raise ValueError(f"unknown broker: {broker_id}")


def __getattr__(name: str) -> object:
    if name == "dhan_capabilities":
        from tradex_brokers.dhan.capabilities import dhan_capabilities

        return dhan_capabilities
    if name == "upstox_capabilities":
        from tradex_brokers.upstox.capabilities import upstox_capabilities

        return upstox_capabilities
    if name == "paper_capabilities":
        from tradex_brokers.paper.capabilities import paper_capabilities

        return paper_capabilities
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "dhan_capabilities",
    "get_capabilities",
    "paper_capabilities",
    "upstox_capabilities",
]
