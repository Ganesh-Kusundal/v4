"""Shared WebSocket stream helpers for broker adapters.

Contains common functions used by both Dhan and Upstox WebSocket streams
to avoid code duplication.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from tradex_domain.instruments import Instrument
from tradex_domain.market import Depth, Quote
from tradex_domain.value_objects import Price, Quantity


def level_pair(raw: dict[str, Any]) -> tuple[Price, Quantity]:
    """Build a ``(Price, Quantity)`` depth level from a raw buy/sell row."""
    return (
        Price(value=Decimal(str(raw["price"]))),
        Quantity(value=Decimal(str(raw["quantity"]))),
    )


def row_to_quote(
    instrument: Instrument,
    row: dict[str, Any],
    *,
    provider: str,
) -> Quote:
    """Build a domain ``Quote`` from a REST-shaped row dict."""
    depth_data = row.get("depth") or {}
    buys = depth_data.get("buy") or []
    sells = depth_data.get("sell") or []
    bid = Price(value=Decimal(str(buys[0]["price"]))) if buys else None
    ask = Price(value=Decimal(str(sells[0]["price"]))) if sells else None
    ltp = Price(value=Decimal(str(row.get("last_price", 0))))
    volume = (
        Quantity(Decimal(str(row["volume"])))
        if "volume" in row and row["volume"]
        else None
    )
    open_interest = (
        Quantity(value=Decimal(str(row["oi"]))) if row.get("oi") else None
    )
    ts = None
    if row.get("timestamp"):
        try:
            ts = datetime.fromisoformat(str(row["timestamp"]))
        except (ValueError, TypeError):
            ts = None
    # ponytail: depth construction single-sourced via build_depth (SMELL-04)
    depth_obj: Depth | None = None
    if buys or sells:
        from tradex_brokers.common.market_builders import build_depth

        depth_obj = build_depth(
            instrument,
            bids_raw=[b for b in buys if isinstance(b, dict)],
            asks_raw=[a for a in sells if isinstance(a, dict)],
            timestamp=ts,
        )
    metadata: dict[str, object] | None = None
    greeks = row.get("greeks", row.get("option_greeks"))
    if isinstance(greeks, dict) and greeks:
        metadata = {"greeks": dict(greeks)}
    return Quote(
        instrument=instrument,
        ltp=ltp,
        bid=bid,
        ask=ask,
        volume=volume,
        open_interest=open_interest,
        depth=depth_obj,
        metadata=metadata,
        timestamp=ts or datetime.now(UTC),
        provider=provider,
    )


__all__ = ["level_pair", "row_to_quote"]
