"""TradeX v4 broker SDK — Dhan, Upstox, Paper adapters.

Adapters are constructed directly (``DhanBroker()``, ``UpstoxBroker()``,
``PaperBroker()``) or via ``from_fetch``; ``runtime.startup.boot`` is the
single composition root that decides which one a session uses.
"""

from tradex_brokers.dhan.adapter import DhanBroker
from tradex_brokers.paper.adapter import PaperBroker
from tradex_brokers.upstox.adapter import UpstoxBroker

__all__ = [
    "DhanBroker",
    "PaperBroker",
    "UpstoxBroker",
]
