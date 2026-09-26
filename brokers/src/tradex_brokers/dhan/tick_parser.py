"""Dhan binary WebSocket tick-frame parser (RequestCode 15 market feed).

Dhan's market-data socket (``wss://api-feed.dhan.co``) does not push JSON
text frames after a v2 subscription. It streams compact binary packets whose
first byte selects the message type (official dhanhq wire format):

====  ================================  =============================
type  payload                           struct
====  ================================  =============================
2     Ticker (LTP / LTT)                ``<BHBIfI``
4     Quote (LTP/LTQ/LTT/avg/volume…)   ``<BHBIfHIfIIIffff``
5     Open interest                     ``<BHBII``
6     Previous close / prev OI          ``<BHBIfI``
8     Full packet (+ 5-level depth)     ``<BHBIfHIfIIIIIIffff100s``
50    Server disconnect (error code)    ``<BHBIH``
====  ================================  =============================

All multi-byte values are little-endian. Each frame carries an
``exchange_segment`` code (1 = NSE_EQ, 2 = NSE_FNO, …) and a numeric
``security_id``; the parser shapes frames into the same REST-shaped row
dicts produced by the polling quote endpoints (``last_price``,
``last_trade_time``, ``depth: {buy/sell}``, ``ohlc``, ``volume``, ``oi``)
so the shared ``_row_to_quote`` mapper can consume them unchanged.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from typing import Any

from tradex_domain.timezones import IST_ZONE as _IST

from tradex_brokers.common.dhan_segments import (
    DHAN_EXCHANGE_SEGMENT,
    DHAN_SEGMENT_EXCHANGE,
    DHAN_WIRE_CODE_EXCHANGE,
)


#: Exchange-segment codes carried in the binary frame header -> canonical v4
#: domain exchange.  These integers are Dhan's *binary protocol* codes and are
#: NOT derivable from :data:`DHAN_EXCHANGE_SEGMENT` — they come from the wire,
#: not from the REST segment vocabulary.  The code list itself is pinned once,
#: in :data:`common.dhan_segments.DHAN_WIRE_CODE_EXCHANGE` (note the gap: Dhan
#: assigns no segment to code 6 and never sends a code for ``NSE_COMM``).
#:
#: This module is the consumer, not a second source: it validates the pinned
#: list against the canonical forward table at import time so a domain
#: exchange added or re-pointed there cannot leave the wire map stale.
def _assert_wire_codes_match_segments() -> dict[int, str]:
    """Validate the wire-code list against the canonical segment tables.

    Raises ``AssertionError`` at import if a code's exchange is unknown to
    :data:`DHAN_EXCHANGE_SEGMENT`, if it does not round-trip to itself, or if
    the list was written as a positional enumeration of the forward table.
    """
    known = set(DHAN_EXCHANGE_SEGMENT)
    unknown = sorted(set(DHAN_WIRE_CODE_EXCHANGE.values()) - known)
    assert not unknown, (
        "dhan wire codes reference exchanges missing from the canonical "
        f"forward table (common.dhan_segments.DHAN_EXCHANGE_SEGMENT): {unknown}"
    )
    # A code resolves back to its own exchange only if the forward table still
    # maps that exchange to that exact segment.  This catches a *misrouted*
    # value that a set-equality check would miss because the exchange is
    # known: re-pointing a forward entry (CDS -> "BSE_CURRENCY") makes both
    # code 3 and code 7 fail, and dropping an entry makes its code fail.
    stale = {
        code: exchange
        for code, exchange in DHAN_WIRE_CODE_EXCHANGE.items()
        if DHAN_SEGMENT_EXCHANGE.get(DHAN_EXCHANGE_SEGMENT[exchange]) != exchange
    }
    assert not stale, (
        f"dhan wire codes disagree with the canonical forward table: {stale}"
    )
    assert DHAN_WIRE_CODE_EXCHANGE != {
        code: exchange for code, exchange in enumerate(DHAN_EXCHANGE_SEGMENT)
    }, (
        "dhan wire codes must stay an explicit protocol table, not an "
        "enumeration of the forward table (Dhan's codes skip 6)"
    )
    return dict(DHAN_WIRE_CODE_EXCHANGE)


#: Public alias: binary wire code -> canonical v4 domain exchange.
#: ``.get(code)`` returns ``None`` for an unknown code (e.g. 6) — unchanged.
SEGMENT_EXCHANGE: dict[int, str] = _assert_wire_codes_match_segments()

#: Dhan LTT is IST wall-clock seconds encoded as a Unix timestamp (as if UTC).
#: Zone from the shared domain timezone module.

#: Struct layout per Dhan binary message type (header + payload fields).
_TICKER = struct.Struct("<BHBIfI")  # type, len, seg, secid, ltp, ltt
_QUOTE = struct.Struct("<BHBIfHIfIIIffff")  # … ltp, ltq, ltt, avg, vol, tsell, tbuy, o,h,l,c
_OI = struct.Struct("<BHBII")  # type, len, seg, secid, oi
_PREV_CLOSE = struct.Struct("<BHBIfI")  # type, len, seg, secid, prev_close, prev_oi
_FULL = struct.Struct("<BHBIfHIfIIIIIIffff100s")  # … + oi, oi_high, oi_low, o,h,l,c, depth
_DISCONNECT = struct.Struct("<BHBIH")  # type, len, seg, reserved, error_code

_DEPTH_LEVEL = struct.Struct("<IIHHff")  # bid_qty, ask_qty, bid_orders, ask_orders, bid, ask
_DEPTH_LEVELS = 5

_MSG_TYPES = {
    2: _TICKER,
    4: _QUOTE,
    5: _OI,
    6: _PREV_CLOSE,
    8: _FULL,
    50: _DISCONNECT,
}


def _epoch_to_iso(epoch: int) -> str:
    """Convert a Dhan LTT epoch to ISO 8601 UTC.

    Measured live (NSE + MCX): the wire integer is IST wall clock encoded as a
    Unix timestamp *as if* those components were UTC. Reading it as UTC leaves
    ``Quote.timestamp`` ~5.5h in the future and breaks mark freshness.
    """
    try:
        # ponytail: one shared decode — all frame types call this
        wall = datetime.fromtimestamp(epoch, tz=UTC).replace(tzinfo=None)
        return wall.replace(tzinfo=_IST).astimezone(UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return datetime.now(UTC).isoformat()


def _depth_rows(raw: bytes) -> dict[str, list[dict[str, Any]]]:
    """Split the 100-byte full-packet depth blob into buy/sell level rows."""
    buy: list[dict[str, Any]] = []
    sell: list[dict[str, Any]] = []
    for index in range(_DEPTH_LEVELS):
        start = index * _DEPTH_LEVEL.size
        chunk = raw[start : start + _DEPTH_LEVEL.size]
        if len(chunk) < _DEPTH_LEVEL.size:
            break
        bid_qty, ask_qty, _bid_orders, _ask_orders, bid_price, ask_price = _DEPTH_LEVEL.unpack(
            chunk
        )
        if bid_qty > 0:
            buy.append({"price": round(bid_price, 2), "quantity": bid_qty})
        if ask_qty > 0:
            sell.append({"price": round(ask_price, 2), "quantity": ask_qty})
    return {"buy": buy, "sell": sell}


def _price(value: float) -> float:
    """Round a float price to 2 decimals (dhanhq ``"{:.2f}"`` parity)."""
    return round(value, 2)


def _base_row(
    segment: int,
    security_id: int,
    *,
    ltp: float | None = None,
    ltt: int | None = None,
    volume: int | None = None,
    oi: int | None = None,
    prev_close: float | None = None,
    ohlc: dict[str, float] | None = None,
    depth: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Build a REST-shaped row shared across all frame types."""
    row: dict[str, Any] = {
        "exchange_segment": segment,
        "security_id": security_id,
        "depth": depth or {"buy": [], "sell": []},
    }
    if ltp is not None:
        row["last_price"] = _price(ltp)
    if ltt:
        row["last_trade_time"] = _epoch_to_iso(ltt)
        row["timestamp"] = row["last_trade_time"]
    if volume is not None:
        row["volume"] = volume
    if oi is not None:
        row["oi"] = oi
    if prev_close is not None:
        row["prev_close"] = _price(prev_close)
    if ohlc is not None:
        row["ohlc"] = {key: _price(value) for key, value in ohlc.items()}
    return row


def parse_tick_frame(raw: bytes | bytearray) -> dict[str, Any] | None:
    """Parse one binary market-feed frame into a REST-shaped row dict.

    Returns ``None`` for JSON/text frames (the order-update feed), unknown
    message types, truncated frames, or unparseable bytes. Disconnect frames
    (type 50) return ``{"type": "disconnect", "error_code": …}`` so callers
    can surface the reason instead of silently dropping the socket.
    """
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 2:
        return None
    data = bytes(raw)
    fmt = _MSG_TYPES.get(data[0])
    if fmt is None:
        return None
    size = fmt.size
    if len(data) < size:
        return None
    try:
        fields = fmt.unpack(data[:size])
    except struct.error:
        return None

    msg_type = data[0]
    if msg_type == 2:
        _t, _l, segment, security_id, ltp, ltt = fields
        return _base_row(segment, security_id, ltp=ltp, ltt=ltt)
    if msg_type == 4:
        (
            _t, _l, segment, security_id, ltp, _ltq, ltt, _avg,
            volume, _tsell, _tbuy, open_, close, high, low,
        ) = fields
        return _base_row(
            segment,
            security_id,
            ltp=ltp,
            ltt=ltt,
            volume=volume,
            ohlc={"open": open_, "high": high, "low": low, "close": close},
        )
    if msg_type == 5:
        _t, _l, segment, security_id, oi = fields
        return _base_row(segment, security_id, oi=oi)
    if msg_type == 6:
        _t, _l, segment, security_id, prev_close, prev_oi = fields
        return _base_row(segment, security_id, prev_close=prev_close, oi=prev_oi)
    if msg_type == 8:
        (
            _t, _l, segment, security_id, ltp, _ltq, ltt, _avg, volume, _tsell, _tbuy,
            oi, _oi_high, _oi_low, open_, close, high, low, depth_blob,
        ) = fields
        return _base_row(
            segment,
            security_id,
            ltp=ltp,
            ltt=ltt,
            volume=volume,
            oi=oi,
            ohlc={"open": open_, "high": high, "low": low, "close": close},
            depth=_depth_rows(depth_blob),
        )
    if msg_type == 50:
        _t, _l, segment, _reserved, error_code = fields
        return {"type": "disconnect", "exchange_segment": segment, "error_code": error_code}
    return None


__all__ = ["SEGMENT_EXCHANGE", "parse_tick_frame"]
