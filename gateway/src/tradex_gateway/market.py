"""Quote, depth, option-chain and futures services."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from typing import Any

from tradex_domain.instruments import Option
from tradex_domain.market import Depth, Quote
from tradex_domain.options import OptionChain

from tradex_gateway.directory import InstrumentDirectory, ResolvedInstrument
from tradex_gateway.errors import BadRequest, GatewayError
from tradex_gateway.history import _SingleFlight, _TTLCache, to_epoch_ms
from tradex_gateway.router import MarketDataRouter

_QUOTE_BATCH = 500


def _price(value: Any) -> float | None:
    return float(value.value) if value is not None else None


def _qty(value: Any) -> float | None:
    return float(value.value) if value is not None else None


def quote_payload(quote: Quote, broker: str) -> dict[str, Any]:
    ohlc = quote.ohlc
    return {
        "instrument": str(quote.instrument.instrument_id),
        "ltp": _price(quote.ltp),
        "bid": _price(quote.bid),
        "ask": _price(quote.ask),
        "volume": _qty(quote.volume),
        "oi": _qty(quote.open_interest),
        "ohlc": (
            {
                "open": _price(ohlc.open),
                "high": _price(ohlc.high),
                "low": _price(ohlc.low),
                "close": _price(ohlc.close),
            }
            if ohlc is not None
            else None
        ),
        "ts": to_epoch_ms(quote.timestamp),
        "broker": broker,
    }


def depth_payload(
    depth: Depth,
    broker: str,
    requested_levels: int,
    *,
    source: str,
    degraded: bool,
) -> dict[str, Any]:
    bids = [[_price(price), _qty(qty)] for price, qty in depth.bids[:requested_levels]]
    asks = [[_price(price), _qty(qty)] for price, qty in depth.asks[:requested_levels]]
    delivered = min(len(bids), len(asks))
    return {
        "instrument": str(depth.instrument.instrument_id),
        "requested_levels": requested_levels,
        "delivered_levels": delivered,
        "bids": bids,
        "asks": asks,
        "ts": to_epoch_ms(depth.timestamp) if depth.timestamp else None,
        "broker": broker,
        "source": source,
        "degraded": degraded or delivered < requested_levels,
    }


def serialize_chain(chain: OptionChain, broker: str) -> dict[str, Any]:
    expiries = chain.expiries()
    strikes: list[dict[str, Any]] = []
    for expiry in expiries:
        for pair in expiry.pairs:
            strikes.append(
                {
                    "strike": str(pair.strike.value),
                    "ce": {"instrument_id": str(pair.call.instrument_id)},
                    "pe": {"instrument_id": str(pair.put.instrument_id)},
                }
            )
    return {
        "underlying": str(chain.underlying.instrument_id),
        "expiry": expiries[0].expiry_date.isoformat() if expiries else None,
        "expiries": [expiry.expiry_date.isoformat() for expiry in expiries],
        "strikes": strikes,
        "broker": broker,
        "cached": False,
    }


class QuoteService:
    def __init__(self, directory: InstrumentDirectory, router: MarketDataRouter) -> None:
        self._directory = directory
        self._router = router

    def quote(self, instrument_id: str, broker: str | None = None) -> dict[str, Any]:
        resolved = self._directory.resolve(instrument_id)
        quote, name = self._router.call(
            "quotes",
            lambda adapter: adapter.get_quote(resolved.instrument),
            preferred=broker,
        )
        return quote_payload(quote, name)

    def quotes(
        self, instrument_ids: list[str], broker: str | None = None
    ) -> dict[str, Any]:
        resolved = [self._directory.resolve(item) for item in instrument_ids]
        instruments = [item.instrument for item in resolved]
        try:
            batch, name = self._router.call(
                "quotes",
                lambda adapter: adapter.quote_batch(instruments),
                preferred=broker,
            )
        except GatewayError:
            # A broker without batch support still answers per-instrument calls.
            return self._quotes_individually(resolved, broker)
        quotes: dict[str, dict[str, Any]] = {}
        for quote in batch.values():
            key = str(quote.instrument.instrument_id)
            quotes[key] = quote_payload(quote, name)
        errors = {
            str(item.instrument_id): {
                "code": "instrument_not_found",
                "message": "broker returned no quote",
            }
            for item in resolved
            if str(item.instrument_id) not in quotes
        }
        return {"quotes": quotes, "errors": errors}

    def _quotes_individually(
        self, resolved: list[ResolvedInstrument], broker: str | None
    ) -> dict[str, Any]:
        quotes: dict[str, dict[str, Any]] = {}
        errors: dict[str, dict[str, Any]] = {}
        for item in resolved:
            try:
                quotes[str(item.instrument_id)] = self.quote(
                    str(item.instrument_id), broker
                )
            except GatewayError as exc:
                errors[str(item.instrument_id)] = {
                    "code": exc.code,
                    "message": exc.message,
                }
        return {"quotes": quotes, "errors": errors}


class DepthService:
    def __init__(
        self,
        directory: InstrumentDirectory,
        router: MarketDataRouter,
        *,
        live_depth: Callable[[str], tuple[Depth, str] | None] | None = None,
    ) -> None:
        self._directory = directory
        self._router = router
        self._live_depth = live_depth

    def depth(
        self, instrument_id: str, levels: int = 5, broker: str | None = None
    ) -> dict[str, Any]:
        resolved = self._directory.resolve(instrument_id)
        if levels > 5 and self._live_depth is not None:
            cached = self._live_depth(str(resolved.instrument_id))
            if cached is not None:
                depth, active = cached
                return depth_payload(
                    depth,
                    active,
                    levels,
                    source="ws_cache",
                    degraded=len(depth.bids) < levels or len(depth.asks) < levels,
                )
        depth, name = self._router.call(
            "depth",
            lambda adapter: adapter.depth(resolved.instrument),
            preferred=broker,
        )
        return depth_payload(
            depth,
            name,
            levels,
            source="rest",
            degraded=levels > 5,
        )


class ChainService:
    def __init__(
        self,
        directory: InstrumentDirectory,
        router: MarketDataRouter,
        quotes: QuoteService,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._directory = directory
        self._router = router
        self._quotes = quotes
        self._clock = clock
        self._cache = _TTLCache()
        self._flight = _SingleFlight()

    def option_chain(
        self,
        underlying: str,
        expiry: str | None = None,
        *,
        live: bool = False,
        broker: str | None = None,
    ) -> dict[str, Any]:
        resolved = self._directory.resolve(underlying)
        expiry_date = _expiry_date(expiry)
        if expiry_date is None:
            expiry_date = self._directory.nearest_expiry(resolved.instrument)
        if expiry_date is None:
            raise BadRequest(
                "expiry is required and no non-expired expiry could be derived"
            )
        key = (str(resolved.instrument_id), expiry_date.isoformat(), live, broker)
        now = self._clock()
        cached = self._cache.get(key, now)
        if cached is not None:
            return {**cached, "cached": True}
        with self._flight.lock_for(key):
            cached = self._cache.get(key, now)
            if cached is not None:
                return {**cached, "cached": True}
            chain, name = self._router.call(
                "chain",
                lambda adapter: adapter.get_option_chain(
                    resolved.instrument, expiry_date
                ),
                preferred=broker,
            )
            payload = serialize_chain(chain, name)
            if live:
                payload = self._enrich(chain, payload)
            self._cache.put(key, payload, 10.0, now)
            return payload

    def _enrich(
        self, chain: OptionChain, payload: dict[str, Any]
    ) -> dict[str, Any]:
        legs: dict[str, dict[str, Any]] = {}
        instruments: list[Option] = []
        for expiry in chain.expiries():
            for pair in expiry.pairs:
                for option in (pair.call, pair.put):
                    instruments.append(option)
                    legs[str(option.instrument_id)] = {}
        enriched = False
        for start in range(0, len(instruments), _QUOTE_BATCH):
            chunk = instruments[start : start + _QUOTE_BATCH]
            try:
                batch, _ = self._router.call(
                    "quotes", lambda adapter: adapter.quote_batch(chunk)
                )
            except GatewayError:
                continue
            for quote in batch.values():
                leg = legs.get(str(quote.instrument.instrument_id))
                if leg is None:
                    continue
                leg.update(
                    {
                        "ltp": _price(quote.ltp),
                        "oi": _qty(quote.open_interest),
                        "volume": _qty(quote.volume),
                    }
                )
                enriched = True
        for row in payload["strikes"]:
            for side in ("ce", "pe"):
                row[side].update(legs.get(row[side]["instrument_id"], {}))
        payload["live"] = enriched
        return payload

    def futures(self, underlying: str, broker: str | None = None) -> dict[str, Any]:
        resolved = self._directory.resolve(underlying)
        futures, name = self._router.call(
            "chain",
            lambda adapter: adapter.future_chain(resolved.instrument),
            preferred=broker,
        )
        return {
            "underlying": str(resolved.instrument_id),
            "futures": [
                {
                    "instrument_id": str(item.instrument_id),
                    "expiry": item.expiry.isoformat() if item.expiry else None,
                }
                for item in futures
            ],
            "broker": name,
        }


def _expiry_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BadRequest(f"invalid expiry: {value!r}") from exc
