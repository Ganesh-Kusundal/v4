"""Indicator stream subscription handling — extracted from ws_stream.

Owns the indicator subscription/unsubscription logic for the websocket
stream. Self-contained: needs only the registry and an ack callable.
"""

from __future__ import annotations

from typing import Any

from tradex_interfaces.routes.stream_indicators import IndicatorStreamRegistry


class IndicatorStream:
    """Handle indicator sub/unsub websocket messages.

    Parameters
    ----------
    registry:
        The indicator stream registry tracking active subscriptions.
    ack:
        Callable that sends a websocket message back to the client.
    """

    def __init__(
        self,
        registry: IndicatorStreamRegistry,
        ack: Any,
    ) -> None:
        self._registry = registry
        self._ack = ack

    def sub(self, msg: dict[str, Any]) -> None:
        """Handle an indicator-sub message."""
        items = msg.get("indicators")
        if not isinstance(items, list) or not items:
            self._ack({
                "type": "error",
                "message": (
                    "indicator-sub needs 'indicators': "
                    "[{instrument, interval, ids, mode?}]"
                ),
            })
            return
        accepted = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                ids = self._registry.add(
                    str(item.get("instrument", "")),
                    str(item.get("interval", "1m")),
                    [str(i) for i in (item.get("ids") or [])],
                    str(item.get("mode", "bar-close")),
                )
            except ValueError as exc:
                self._ack({"type": "error", "message": str(exc)})
                return
            accepted.append({
                "instrument": str(item.get("instrument", "")),
                "interval": str(item.get("interval", "1m")),
                "ids": ids,
                "mode": str(item.get("mode", "bar-close")),
            })
        self._ack({"type": "indicator-subscribed", "indicators": accepted})

    def unsub(self, msg: dict[str, Any]) -> None:
        """Handle an indicator-unsub message."""
        items = msg.get("indicators")
        if not isinstance(items, list) or not items:
            self._ack({
                "type": "error",
                "message": (
                    "indicator-unsub needs 'indicators': "
                    "[{instrument, interval, ids?}]"
                ),
            })
            return
        removed = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ids = item.get("ids")
            dropped_ids = self._registry.remove(
                str(item.get("instrument", "")),
                str(item.get("interval", "1m")),
                [str(i) for i in ids] if ids else None,
            )
            removed.append({
                "instrument": str(item.get("instrument", "")),
                "interval": str(item.get("interval", "1m")),
                "ids": dropped_ids,
            })
        self._ack({"type": "indicator-unsubscribed", "indicators": removed})
