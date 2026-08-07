"""Example extension scanner — price momentum screen (reference material).

Ships with the framework to prove the extensions auto-discovery mechanism:
define a ``ScannerDefinition``, expose it in ``__all__``, and it is
discovered by ``ScannerEngine`` consumers.
"""

from __future__ import annotations

from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity
from tradex_domain.strategy import Condition, ScannerDefinition

momentum_scanner = ScannerDefinition(
    universe=[
        Equity.of(ExchangeId.NSE, "RELIANCE"),
        Equity.of(ExchangeId.NSE, "TCS"),
    ],
    conditions=[
        Condition(name="close", params={}, operator=">", threshold=1000.0),
    ],
    rank_by="score",
    limit=20,
)

__all__ = ["momentum_scanner"]
