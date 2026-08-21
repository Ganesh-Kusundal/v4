"""Single-source Depth builder (SMELL-04).

The ``sorted(bids descending) / sorted(asks ascending)`` + ``(Price, Quantity)``
contract was copy-pasted in 5 places:
  dhan/_marketdata.depth, upstox/_marketdata.depth, dhan/depth_parser,
  common/ws_shared, upstox/ws_streams

Ponytail: one function, zero magic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tradex_domain.instruments import Instrument
from tradex_domain.market import Depth

from tradex_brokers.common.provider_common import as_decimal, as_price
from tradex_domain.value_objects import Price, Quantity


def build_depth(
    instrument: Instrument,
    *,
    bids_raw: list[dict[str, Any]] | None = None,
    asks_raw: list[dict[str, Any]] | None = None,
    # Dhan wire style: depth_data dict with "buy"/"sell" keys
    depth_data: dict[str, Any] | None = None,
    price_key: str = "price",
    qty_key: str = "quantity",
    timestamp: datetime | None = None,
) -> Depth:
    """Build a sorted, validated ``Depth`` from raw provider levels."""

    def _level(d: dict[str, Any]) -> tuple[Price, Quantity]:
        # ponytail: coerce qty via str so float/int both work (Upstox uses int)
        q = d.get(qty_key, 0)
        return as_price(d.get(price_key)), Quantity(value=as_decimal(str(q) if q is not None else 0))

    if depth_data is not None:
        bids_raw = [x for x in depth_data.get("buy", []) if isinstance(x, dict)]
        asks_raw = [x for x in depth_data.get("sell", []) if isinstance(x, dict)]
    bids_raw = bids_raw or []
    asks_raw = asks_raw or []
    bids = tuple(sorted((_level(x) for x in bids_raw if isinstance(x, dict)), key=lambda lv: lv[0].value, reverse=True))
    asks = tuple(sorted((_level(x) for x in asks_raw if isinstance(x, dict)), key=lambda lv: lv[0].value))
    return Depth(instrument=instrument, bids=bids, asks=asks, timestamp=timestamp or datetime.now(UTC))


__all__ = ["build_depth"]
