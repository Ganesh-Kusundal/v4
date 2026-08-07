"""Upstox V3 protobuf feed decoder.

Decodes binary WebSocket frames into REST-shaped row dicts matching the
shape produced by the polling quote endpoints.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tradex_brokers.proto.MarketDataFeed_pb2 import FeedResponse  # type: ignore[attr-defined]


def parse_feed_response(raw: bytes) -> dict[str, dict[str, Any]]:
    """Decode one WS binary frame into ``{instrument_key: REST-shaped row}``.

    Returns an empty dict for a ``market_info`` frame (no feeds), a
    heartbeat, or an unparseable frame.
    """
    response = FeedResponse()
    try:
        response.ParseFromString(raw)
    except Exception:  # noqa: BLE001 — unparseable frame ⇒ skip
        return {}

    ts_iso = _epoch_ms_to_iso(response.currentTs) if response.currentTs else None

    out: dict[str, dict[str, Any]] = {}
    for key, feed in response.feeds.items():
        which = feed.WhichOneof("FeedUnion")
        if which == "ltpc":
            out[key] = _shape_ltpc(feed.ltpc, ts_iso)
        elif which == "fullFeed":
            ff_which = feed.fullFeed.WhichOneof("FullFeedUnion")
            if ff_which == "marketFF":
                out[key] = _shape_market_full(feed.fullFeed.marketFF, ts_iso)
            elif ff_which == "indexFF":
                out[key] = _shape_index_full(feed.fullFeed.indexFF, ts_iso)
        elif which == "firstLevelWithGreeks":
            out[key] = _shape_first_level(feed.firstLevelWithGreeks, ts_iso)
    return out


def _epoch_ms_to_iso(epoch_ms: int) -> str:
    """Convert epoch milliseconds to ISO 8601 string."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).isoformat()


def _shape_depth_rows(bid_ask_quotes: Any) -> dict[str, list[dict[str, Any]]]:
    """Split combined bid/ask rows into separate buy/sell arrays."""
    buy: list[dict[str, Any]] = []
    sell: list[dict[str, Any]] = []
    for row in bid_ask_quotes:
        if row.bidQ > 0:
            buy.append({"price": row.bidP, "quantity": row.bidQ})
        if row.askQ > 0:
            sell.append({"price": row.askP, "quantity": row.askQ})
    return {"buy": buy, "sell": sell}


def _shape_ltpc(ltpc: Any, ts_iso: str | None) -> dict[str, Any]:
    """Shape LTPC feed into REST-shaped row."""
    return {"last_price": ltpc.ltp, "timestamp": ts_iso, "depth": {"buy": [], "sell": []}}


def _shape_greeks(og: Any) -> dict[str, float] | None:
    """Extract option greeks from the proto ``optionGreeks`` message.

    Presence is decided by the *caller* via ``HasField("optionGreeks")`` on the
    parent (message fields always carry presence in proto3); the scalar
    sub-fields are guaranteed set once the message itself is set.
    """
    if og is None:
        return None
    return {
        "delta": og.delta,
        "gamma": og.gamma,
        "theta": og.theta,
        "vega": og.vega,
        "rho": og.rho,
    }


def _shape_market_full(mf: Any, ts_iso: str | None) -> dict[str, Any]:
    """Shape market full feed into REST-shaped row."""
    row: dict[str, Any] = {
        "last_price": mf.ltpc.ltp,
        "timestamp": ts_iso,
        "depth": _shape_depth_rows(mf.marketLevel.bidAskQuote),
        "oi": mf.oi,
        "volume": mf.vtt,
        "average_price": mf.atp,
    }
    if mf.HasField("optionGreeks"):
        row["greeks"] = _shape_greeks(mf.optionGreeks)
    return row


def _shape_index_full(idx: Any, ts_iso: str | None) -> dict[str, Any]:
    """Shape index full feed into REST-shaped row."""
    return {"last_price": idx.ltpc.ltp, "timestamp": ts_iso, "depth": {"buy": [], "sell": []}}


def _shape_first_level(fl: Any, ts_iso: str | None) -> dict[str, Any]:
    """Shape first level with greeks into REST-shaped row."""
    depth = (
        _shape_depth_rows([fl.firstDepth])
        if fl.HasField("firstDepth")
        else {"buy": [], "sell": []}
    )
    row: dict[str, Any] = {
        "last_price": fl.ltpc.ltp,
        "timestamp": ts_iso,
        "depth": depth,
        "oi": fl.oi,
        "volume": fl.vtt,
    }
    if fl.HasField("optionGreeks"):
        row["greeks"] = _shape_greeks(fl.optionGreeks)
    return row


__all__ = ["parse_feed_response"]
