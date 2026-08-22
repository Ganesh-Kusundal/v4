"""Broker capability mechanism (§3/§4, D-14..D-16).

``BrokerCapabilities`` is a fail-closed declarative truth table: nothing is
claimed unless a broker's table explicitly enables it. ``require_capability``
is the single capability-loud gate used by the SDK services (D-8).

The per-provider tables (``dhan_capabilities`` etc.) live in the broker
packages (``tradex_brokers.common.capabilities``) — provider facts belong to
providers, not to the domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tradex_domain.enums import AssetClass
from tradex_domain.errors import CapabilityNotSupportedError

#: Every boolean capability flag on ``BrokerCapabilities``. Passing any other
#: string to :func:`require_capability` is a static type error instead of a
#: silent always-False getattr lookup.
CapabilityName = Literal[
    "supports_market_order",
    "supports_limit_order",
    "supports_stop_order",
    "supports_modify",
    "supports_super_order",
    "supports_forever_order",
    "supports_slice_order",
    "supports_edis",
    "supports_batch_market_data",
    "supports_portfolio_stream",
    "supports_option_chain",
    "supports_future_chain",
    "supports_kill_switch",
    "supports_news",
    "supports_fundamentals",
]


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


def require_capability(capabilities: BrokerCapabilities, name: CapabilityName) -> None:
    """Raise ``CapabilityNotSupportedError`` when the named flag is false (D-8)."""
    if not getattr(capabilities, name):
        raise CapabilityNotSupportedError(f"broker does not support capability: {name}")


__all__ = [
    "BrokerCapabilities",
    "CapabilityName",
    "require_capability",
]
