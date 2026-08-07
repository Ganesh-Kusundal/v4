"""TradeX v4 broker SDK — Dhan, Upstox, Paper adapters.

Exports the ``BrokerFactory`` plugin registry at the top level.
"""

from tradex_domain import BrokerId

from tradex_brokers.dhan.adapter import DhanBroker
from tradex_brokers.paper.adapter import PaperBroker
from tradex_brokers.registry import BrokerFactory
from tradex_brokers.upstox.adapter import UpstoxBroker

BrokerFactory.register(BrokerId.PAPER, PaperBroker)
BrokerFactory.register(BrokerId.DHAN, DhanBroker)
BrokerFactory.register(BrokerId.UPSTOX, UpstoxBroker)

__all__ = [
    "BrokerFactory",
]
