"""Example extension scanner — two-condition pullback screen (reference material).

Ships with the framework to exercise the extensions auto-discovery mechanism
harder than the momentum screen: this definition combines **two** conditions
(a price filter *and* an RSI indicator filter) so ``ScannerEngine._evaluate``
scores against both and the scanner only surfaces instruments that clear
both — rank by score, so a 2/2 hit outranks a 1/2 hit.
"""

from __future__ import annotations

from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity
from tradex_domain.strategy import Condition, ScannerDefinition

pullback_scanner = ScannerDefinition(
    universe=[
        Equity.of(ExchangeId.NSE, "RELIANCE"),
        Equity.of(ExchangeId.NSE, "TCS"),
        Equity.of(ExchangeId.NSE, "INFY"),
    ],
    conditions=[
        Condition(name="close", params={}, operator=">", threshold=500.0),
        # RSI < 40 over the trailing window — mean-reversion oversold filter.
        Condition(name="rsi", params={"period": 14}, operator="<", threshold=40.0),
    ],
    rank_by="score",
    limit=20,
)

