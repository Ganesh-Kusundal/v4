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
    depth_obj: Depth | None = None
    if buys or sells:
        # Normalize the book (bids price-descending, asks ascending) so the
        # depth invariant ``Depth.best_bid/best_ask == [0]`` holds even if the
        # provider streams levels out of order.
        bid_levels = tuple(
            sorted(
                (level_pair(b) for b in buys),
                key=lambda level: level[0].value,
                reverse=True,
            )
        )
        ask_levels = tuple(
            sorted(
                (level_pair(a) for a in sells),
                key=lambda level: level[0].value,
            )
        )
        depth_obj = Depth(
            instrument=instrument,
            bids=bid_levels,
            asks=ask_levels,
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
