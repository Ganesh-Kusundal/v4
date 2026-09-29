"""Paper broker capability table — re-exported from common."""

from __future__ import annotations

from tradex_brokers.common.capabilities import get_capabilities
from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.enums import BrokerId


def paper_capabilities() -> BrokerCapabilities:
    return get_capabilities(BrokerId.PAPER)
