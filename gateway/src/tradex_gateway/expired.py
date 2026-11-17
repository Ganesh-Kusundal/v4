"""Expired F&O contracts: contract lists and historical candles.

Upstox exposes a dedicated expired-instruments API (Plus tier); brokers
without it simply lack the adapter methods and the router fails over or
reports the capability as unsupported.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from tradex_domain.errors import CapabilityNotSupportedError

from tradex_gateway.directory import InstrumentDirectory
from tradex_gateway.errors import BadRequest
from tradex_gateway.router import MarketDataRouter

_EXPIRED_INTERVALS = {"1minute", "3minute", "5minute", "15minute", "30minute", "day"}


def _expiry_date(value: str | None) -> date:
    if not value:
        raise BadRequest("expiry is required (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise BadRequest(f"invalid expiry: {value!r}") from exc


def _query_date(value: str | None, name: str) -> date:
    if not value:
        raise BadRequest(f"{name} is required (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise BadRequest(f"invalid {name}: {value!r}") from exc


def _normalize_candles(rows: Any) -> list[dict[str, Any]]:
    """Upstox expired candles arrive as ``[ts, o, h, l, c, v, oi?]`` arrays."""
    bars: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return bars
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            ts = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
            bar = {
                "t": int(ts.timestamp() * 1000),
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5]),
            }
        except (ValueError, TypeError):
            continue
        if len(row) > 6 and row[6] is not None:
            try:
                bar["oi"] = float(row[6])
            except (ValueError, TypeError):
                pass
        bars.append(bar)
    return bars


class ExpiredService:
    def __init__(self, directory: InstrumentDirectory, router: MarketDataRouter) -> None:
        self._directory = directory
        self._router = router

    def option_contracts(
        self, underlying: str, expiry: str, broker: str | None = None
    ) -> dict[str, Any]:
        return self._contracts(underlying, expiry, "option", broker)

    def future_contracts(
        self, underlying: str, expiry: str, broker: str | None = None
    ) -> dict[str, Any]:
        return self._contracts(underlying, expiry, "future", broker)

    def _contracts(
        self, underlying: str, expiry: str, kind: str, broker: str | None
    ) -> dict[str, Any]:
        resolved = self._directory.resolve(underlying)
        expiry_date = _expiry_date(expiry)

        def fn(adapter: Any) -> Any:
            method = getattr(adapter, "expired_contracts", None)
            if method is None:
                raise CapabilityNotSupportedError(
                    f"{kind} expired contracts are not supported by this broker"
                )
            return method(resolved.instrument, expiry_date, kind)

        rows, name = self._router.call("expired", fn, preferred=broker)
        return {
            "underlying": str(resolved.instrument_id),
            "expiry": expiry_date.isoformat(),
            "kind": kind,
            "contracts": rows,
            "broker": name,
        }

    def candles(
        self,
        key: str,
        interval: str,
        start: date,
        end: date,
        broker: str | None = None,
    ) -> dict[str, Any]:
        if interval not in _EXPIRED_INTERVALS:
            raise BadRequest(f"unsupported interval: {interval!r}")
        if end <= start:
            raise BadRequest("'to' must be later than 'from'")

        def fn(adapter: Any) -> Any:
            method = getattr(adapter, "expired_candles", None)
            if method is None:
                raise CapabilityNotSupportedError(
                    "expired-contract candles are not supported by this broker"
                )
            return method(key, interval, start, end)

        rows, name = self._router.call("expired", fn, preferred=broker)
        return {
            "key": key,
            "interval": interval,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "candles": _normalize_candles(rows),
            "broker": name,
        }
