"""Dhan binary depth frame parser.

Parses Dhan's proprietary binary WebSocket frame format into depth levels.
Supports both depth-20 and depth-200 feeds.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from tradex_domain.instruments import Instrument
from tradex_domain.market import Depth
from tradex_domain.value_objects import Price, Quantity

# Wire layout constants
HEADER_SIZE = 12
LEVEL_SIZE = 16  # 8 bytes price + 4 bytes quantity + 4 bytes padding
BID_RESPONSE_CODE = 41
ASK_RESPONSE_CODE = 51


def parse_depth_frame(
    raw: bytes,
    *,
    total_slots: int = 20,
    header_carries_security_id: bool = True,
    security_id: int | None = None,
) -> list[dict[str, Any]]:
    """Parse one or more depth sub-packets from a binary WS frame.

    Returns a list of dicts shaped for depth construction:
    ``{"side": "bids"|"asks", "levels": [...], "security_id": int}``.
    """
    packet_size = HEADER_SIZE + total_slots * LEVEL_SIZE
    result: list[dict[str, Any]] = []

    if len(raw) < HEADER_SIZE:
        return result

    offset = 0
    while offset + HEADER_SIZE <= len(raw):
        chunk = raw[offset : offset + packet_size]
        if len(chunk) < HEADER_SIZE:
            break

        response_code = chunk[2]
        if response_code not in (BID_RESPONSE_CODE, ASK_RESPONSE_CODE):
            offset += packet_size
            continue

        side = "bids" if response_code == BID_RESPONSE_CODE else "asks"
        levels = _parse_levels(chunk, total_slots)

        if header_carries_security_id:
            sec_id = struct.unpack_from("<I", chunk, 4)[0]
        else:
            sec_id = security_id or 0

        result.append({"side": side, "levels": levels, "security_id": sec_id})
        offset += packet_size

    return result


def _parse_levels(data: bytes, total_slots: int) -> list[tuple[Price, Quantity]]:
    """Parse depth levels from a binary chunk."""
    levels: list[tuple[Price, Quantity]] = []
    for i in range(total_slots):
        offset = HEADER_SIZE + (i * LEVEL_SIZE)
        if offset + LEVEL_SIZE > len(data):
            break
        price = struct.unpack_from("<d", data, offset)[0]
        quantity = struct.unpack_from("<I", data, offset + 8)[0]
        if quantity > 0:
            levels.append((
                Price(value=Decimal(str(round(price, 2)))),
                Quantity(value=Decimal(str(quantity))),
            ))
    return levels


def depth_frame_to_depth(
    raw: bytes,
    instrument: Instrument,
    *,
    total_slots: int = 20,
    header_carries_security_id: bool = True,
    security_id: int | None = None,
) -> Depth | None:
    """Parse a binary depth frame into a domain ``Depth`` object.

    Returns ``None`` for empty/unparseable frames.
    """
    packets = parse_depth_frame(
        raw,
        total_slots=total_slots,
        header_carries_security_id=header_carries_security_id,
        security_id=security_id,
    )
    if not packets:
        return None

    bids: list[tuple[Price, Quantity]] = []
    asks: list[tuple[Price, Quantity]] = []
    for pkt in packets:
        target = bids if pkt["side"] == "bids" else asks
        target.extend(pkt["levels"])

    return Depth(
        instrument=instrument,
        bids=tuple(bids),
        asks=tuple(asks),
        timestamp=datetime.now(UTC),
    )


__all__ = ["depth_frame_to_depth", "parse_depth_frame"]
