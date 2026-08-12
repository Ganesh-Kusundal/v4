"""Extension scanner — Nifty 500 technical screener (example).

Screens the whole Nifty 500 universe on price + momentum: price above a
floor and a positive 10-bar Rate of Change. Shows the extensions path for a
large-universe screener and exercises the ``roc`` indicator added to the
AnalyticsEngine registry.

The universe is loaded at import and guarded: if the constituent CSV is
missing or malformed, discovery degrades to a tiny example universe with a
logged warning instead of failing the whole boot (auto-discovery is
fail-closed by design — a broken user extension must never take the
platform down).
"""

from __future__ import annotations

import logging
from typing import cast

from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity, Instrument
from tradex_domain.strategy import Condition, ScannerDefinition

log = logging.getLogger(__name__)

try:
    from tradex_trading.datalake.universe import load_universe

    _UNIVERSE: list[Instrument] = cast("list[Instrument]", load_universe("nifty500"))
except (FileNotFoundError, OSError, ImportError, KeyError) as exc:  # pragma: no cover
    log.warning("nifty500 universe unavailable (%s); falling back to example screen", exc)
    _UNIVERSE = [
        Equity.of(ExchangeId.NSE, "RELIANCE"),
        Equity.of(ExchangeId.NSE, "TCS"),
        Equity.of(ExchangeId.NSE, "INFY"),
    ]

nifty500_technical_scanner = ScannerDefinition(
    universe=_UNIVERSE,
    conditions=[
        # Price floor — screens out sub-100 rupee names.
        Condition(name="close", params={}, operator=">", threshold=100.0),
        # Positive 10-day momentum.
        Condition(name="roc", params={"period": 10}, operator=">", threshold=0.0),
    ],
    rank_by="score",
    limit=20,
)

__all__ = ["nifty500_technical_scanner"]
