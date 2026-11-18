"""Quote, depth, option-chain and futures services."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from tradex_domain.instruments import Option
from tradex_domain.market import Depth, Quote
from tradex_domain.options import OptionChain

from tradex_gateway.cache import SingleFlight, TTLCache
from tradex_gateway.directory import InstrumentDirectory, ResolvedInstrument
from tradex_gateway.errors import BadRequest, CapabilityUnsupported, GatewayError
from tradex_gateway.history import to_epoch_ms
from tradex_gateway.router import MarketDataRouter

_QUOTE_BATCH = 500


@dataclass(frozen=True)
class CachedDepth:
    """A live book plus the hub receipt time used to judge its freshness."""

    depth: Depth
    broker: str
    received_at: datetime


def _price(value: Any) -> float | None:
    return float(value.value) if value is not None else None


def _qty(value: Any) -> float | None:
    return float(value.value) if value is not None else None


def quote_payload(quote: Quote, broker: str) -> dict[str, Any]:
    ohlc = quote.ohlc
    payload: dict[str, Any] = {
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
    meta = getattr(quote, "metadata", None)
    if isinstance(meta, dict):
        if isinstance(meta.get("greeks"), dict):
            payload["greeks"] = dict(meta["greeks"])
        if meta.get("iv") is not None:
            payload["iv"] = meta["iv"]
    return payload


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


def _serialize_leg(
    option: Option,
    leg: dict[str, object] | None,
    lot_size_for: Callable[[Any], int | None] | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "instrument_id": str(option.instrument_id),
        "lot_size": lot_size_for(option.instrument_id) if lot_size_for is not None else None,
    }
    if leg:
        out["leg"] = dict(leg)
    return out


def serialize_chain(
    chain: OptionChain,
    broker: str,
    lot_size_for: Callable[[Any], int | None] | None = None,
) -> dict[str, Any]:
    expiries = chain.expiries()
    selected = next((e for e in expiries if e.pairs), expiries[0]) if expiries else None
    strikes: list[dict[str, Any]] = []
    if selected is not None:
        for pair in selected.pairs:
            strikes.append(
                {
                    "strike": str(pair.strike.value),
                    "ce": _serialize_leg(pair.call, pair.call_leg, lot_size_for),
                    "pe": _serialize_leg(pair.put, pair.put_leg, lot_size_for),
                }
            )
    return {
        "underlying": str(chain.underlying.instrument_id),
        "expiry": selected.expiry_date.isoformat() if selected else None,
        "expiries": [expiry.expiry_date.isoformat() for expiry in expiries],
        "reference_price": _price(selected.reference_price) if selected else None,
        "strikes": strikes,
        "broker": broker,
        "cached": False,
    }


class QuoteService:
    def __init__(
        self,
        directory: InstrumentDirectory,
        router: MarketDataRouter,
        fallback_cap: int = 25,
    ) -> None:
        self._directory = directory
        self._router = router
        self._fallback_cap = fallback_cap

    def quote(self, instrument_id: str, broker: str | None = None) -> dict[str, Any]:
        resolved = self._directory.resolve(instrument_id)
        quote, name = self._router.call(
            "quotes",
            lambda adapter: adapter.get_quote(resolved.instrument),
            preferred=broker,
        )
        return quote_payload(quote, name)

    def quotes(self, instrument_ids: list[str], broker: str | None = None) -> dict[str, Any]:
        resolved: list[ResolvedInstrument] = []
        errors: dict[str, dict[str, Any]] = {}
        for raw_id in instrument_ids:
            try:
                resolved.append(self._directory.resolve(raw_id))
            except GatewayError as exc:
                errors[raw_id] = {"code": exc.code, "message": exc.message}
        if not resolved:
            return {"quotes": {}, "errors": errors}
        instruments = [item.instrument for item in resolved]
        try:
            batch, name = self._router.call(
                "quotes",
                lambda adapter: adapter.quote_batch(instruments),
                preferred=broker,
            )
        except CapabilityUnsupported:
            # A broker without batch support still answers per-instrument calls.
            # Any non-capability failure surfaces to the caller instead of
            # being masked by N extra per-instrument calls.
            return self._quotes_individually(resolved, broker, errors)
        quotes: dict[str, dict[str, Any]] = {}
        for quote in batch.values():
            key = str(quote.instrument.instrument_id)
            quotes[key] = quote_payload(quote, name)
        for item in resolved:
            if str(item.instrument_id) not in quotes:
                errors[str(item.instrument_id)] = {
                    "code": "instrument_not_found",
                    "message": "broker returned no quote",
                }
        return {"quotes": quotes, "errors": errors}

    def _quotes_individually(
        self,
        resolved: list[ResolvedInstrument],
        broker: str | None,
        errors: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        quotes: dict[str, dict[str, Any]] = {}
        for item in resolved[: self._fallback_cap]:
            try:
                quotes[str(item.instrument_id)] = self.quote(str(item.instrument_id), broker)
            except GatewayError as exc:
                errors[str(item.instrument_id)] = {
                    "code": exc.code,
                    "message": exc.message,
                }
        for item in resolved[self._fallback_cap :]:
            errors[str(item.instrument_id)] = {
                "code": "capability_not_supported",
                "message": "quote_batch unsupported; fallback capped",
            }
        return {"quotes": quotes, "errors": errors}


def _fresh(received_at: datetime | None, now: datetime, ttl: float) -> bool:
    if received_at is None or received_at.tzinfo is None:
        return False
    age = (now - received_at).total_seconds()
    return 0 <= age <= ttl


class DepthService:
    def __init__(
        self,
        directory: InstrumentDirectory,
        router: MarketDataRouter,
        *,
        live_depth: Callable[[str], CachedDepth | None] | None = None,
        live_depth_ttl: float = 5.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._directory = directory
        self._router = router
        self._live_depth = live_depth
        self._live_depth_ttl = live_depth_ttl
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    def depth(
        self, instrument_id: str, levels: int = 5, broker: str | None = None
    ) -> dict[str, Any]:
        resolved = self._directory.resolve(instrument_id)
        if levels > 5 and self._live_depth is not None:
            cached = self._live_depth(str(resolved.instrument_id))
            if cached is not None and _fresh(
                cached.received_at, self._clock(), self._live_depth_ttl
            ):
                depth = cached.depth
                return depth_payload(
                    depth,
                    cached.broker,
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


def _chain_has_pairs(chain: OptionChain) -> bool:
    """True when any expiry carries at least one paired strike.

    ``expiries()`` lists ``Expiry`` rows even when every strike failed to
    pair, so emptiness is judged on pairs, not on the expiry list.
    """
    return any(expiry.pairs for expiry in chain.expiries())


def _call_option_chain(adapter: Any, underlying: Any, expiry_date: date | None) -> OptionChain:
    return adapter.get_option_chain(underlying, expiry_date, allow_master_fallback=False)


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
        self._cache = TTLCache()
        self._flight = SingleFlight()

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
            raise BadRequest("expiry is required and no non-expired expiry could be derived")
        key = (str(resolved.instrument_id), expiry_date.isoformat(), live, broker)
        now = self._clock()
        cached = self._cache.get(key, now)
        if cached is not None:
            return {**cached, "cached": True}
        with self._flight.lock(key):
            cached = self._cache.get(key, now)
            if cached is not None:
                return {**cached, "cached": True}
            def _fetch(adapter: Any) -> OptionChain:
                res = _call_option_chain(adapter, resolved.instrument, expiry_date)
                if expiry is None and not _chain_has_pairs(res):
                    try:
                        return _call_option_chain(adapter, resolved.instrument, None)
                    except ValueError:
                        return res
                return res

            chain, name = self._router.call(
                "chain",
                _fetch,
                preferred=broker,
            )
            payload = serialize_chain(
                chain,
                name,
                lot_size_for=lambda iid: self._directory.lot_size(iid, broker=name),
            )
            for row in payload["strikes"]:
                for side in ("ce", "pe"):
                    leg = row[side]
                    leg["tick_size"] = self._directory.tick_size(leg["instrument_id"], broker=name)
            if live:
                payload = self._enrich(chain, payload, name)
            self._cache.put(key, payload, 10.0, now)
            return payload

    def _enrich(self, chain: OptionChain, payload: dict[str, Any], broker: str) -> dict[str, Any]:
        legs: dict[str, dict[str, Any]] = {}
        instruments: list[Option] = []
        expiries = chain.expiries()
        selected = next((e for e in expiries if e.pairs), expiries[0]) if expiries else None
        if selected is not None:
            for pair in selected.pairs:
                for option in (pair.call, pair.put):
                    instruments.append(option)
                    legs[str(option.instrument_id)] = {}
        enriched = False
        for start in range(0, len(instruments), _QUOTE_BATCH):
            chunk = instruments[start : start + _QUOTE_BATCH]
            try:
                batch, _ = self._router.call(
                    "quotes",
                    lambda adapter: adapter.quote_batch(chunk),
                    preferred=broker,
                )
            except GatewayError:
                continue
            for quote in batch.values():
                leg = legs.get(str(quote.instrument.instrument_id))
                if leg is None:
                    continue
                leg.update(
                    {
                        key: value
                        for key, value in (
                            ("ltp", _price(quote.ltp)),
                            ("oi", _qty(quote.open_interest)),
                            ("volume", _qty(quote.volume)),
                        )
                        if value is not None
                    }
                )
                enriched = True
        for row in payload["strikes"]:
            for side in ("ce", "pe"):
                entry = row[side]
                live = legs.get(entry["instrument_id"], {})
                # An OC-attached leg is the baseline; live quotes overlay only
                # ltp/oi/volume and never add greeks (spec §6).
                if isinstance(entry.get("leg"), dict):
                    entry["leg"].update(live)
                else:
                    entry.update(live)
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
                    "lot_size": self._directory.lot_size(item.instrument_id, broker=name),
                    "tick_size": self._directory.tick_size(item.instrument_id, broker=name),
                }
                # nearest expiry first, so futures[0] is the front contract
                for item in sorted(futures, key=lambda item: item.expiry or date.max)
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
