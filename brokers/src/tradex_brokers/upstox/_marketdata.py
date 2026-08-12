"""Upstox REST client mixin - MarketDataMixin. """

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from tradex_domain.enums import Timeframe
from tradex_domain.errors import CapabilityNotSupportedError, SDKError
from tradex_domain.instruments import Instrument, Option
from tradex_domain.market import OHLC, Candle, Depth, HistoricalSeries, Quote
from tradex_domain.options import Expiry, OptionChain, OptionPair
from tradex_domain.value_objects import InstrumentId, Price, Quantity

if TYPE_CHECKING:
    from tradex_brokers.upstox._facade import UptoxFacade


from tradex_brokers.common.client_shared import parse_timestamp_fallback
from tradex_brokers.common.provider_common import (
    as_decimal,
    as_price,
    parse_date,
    provider_key,
    unwrap_data,
)

_UPSTOX_MAX_BATCH_SIZE = 500


def _unit_interval(value: str) -> tuple[str, str]:
    """Map a timeframe string to the Upstox v3 (unit, interval) pair."""
    mapping = {
        "1m": ("minutes", "1"), "5m": ("minutes", "5"),
        "15m": ("minutes", "15"), "30m": ("minutes", "30"),
        "1h": ("hours", "1"), "1d": ("days", "1"), "1w": ("weeks", "1"),
    }
    pair = mapping.get(value)
    if pair is None:
        raise ValueError(f"unsupported Upstox timeframe: {value!r}")
    return pair


def _target_timeframe(value: str) -> Timeframe:
    """Map a timeframe string back to the canonical Timeframe."""
    return {
        "1m": Timeframe.M1, "5m": Timeframe.M5, "15m": Timeframe.M15,
        "30m": Timeframe.M30, "1h": Timeframe.H1, "1d": Timeframe.D1,
        "1w": Timeframe.W1,
    }.get(value, Timeframe.D1)


def _candles_from_rows(
    rows: object,
    instrument: Instrument,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
) -> list[Candle]:
    """Parse Upstox candle rows (``[ts, o, h, l, c, v, oi]``) into ``Candle``s."""
    candles: list[Candle] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, list):
            continue
        candles.append(
            Candle(
                instrument=instrument,
                timeframe=timeframe,
                ohlc=OHLC(
                    open=as_price(row[1] if len(row) > 1 else 0),
                    high=as_price(row[2] if len(row) > 2 else 0),
                    low=as_price(row[3] if len(row) > 3 else 0),
                    close=as_price(row[4] if len(row) > 4 else 0)),
                volume=Quantity(value=as_decimal(row[5] if len(row) > 5 else 0)),
                timestamp=parse_timestamp_fallback(row[0] if row else None, start),
            )
        )
    return candles


class MarketDataMixin:
    def ltp(self: UptoxFacade, instrument: Instrument) -> Price:
        """Last traded price via V3 GET /market-quote/ltp."""
        key = provider_key(self._registry, instrument.instrument_id)
        body = self._request(
            "GET", "/market-quote/ltp", host="v3", cache_read=True, params={"instrument_key": key})
        row = self._market_row(unwrap_data(body), key)
        return (
            as_price(row.get("last_price", row.get("ltp")))
            if isinstance(row, dict)
            else as_price(None)
        )

    def ltp_batch(
        self: UptoxFacade, instruments: Sequence[Instrument]
    ) -> dict[InstrumentId, Price]:
        """Native multi-key LTP via GET /market-quote/ltp (chunked <=500)."""
        keys, key_map = self._resolve_batch_keys(instruments)
        result: dict[InstrumentId, Price] = {}
        for start in range(0, len(keys), _UPSTOX_MAX_BATCH_SIZE):
            chunk = keys[start : start + _UPSTOX_MAX_BATCH_SIZE]
            body = self._request(
                "GET", "/market-quote/ltp", host="v3", cache_read=True,
                params={"instrument_key": ",".join(chunk)})
            raw = unwrap_data(body)
            if not isinstance(raw, dict):
                continue
            for row in raw.values():
                if not isinstance(row, dict):
                    continue
                instrument = key_map.get(str(row.get("instrument_token", "")))
                if instrument is None:
                    continue
                result[instrument.instrument_id] = as_price(
                    row.get("last_price", row.get("ltp"))
                )
        return result

    def quote_batch(
        self: UptoxFacade, instruments: Sequence[Instrument]
    ) -> dict[InstrumentId, Quote]:
        """Native multi-key quote via GET /market-quote/quotes (chunked <=500)."""
        keys, key_map = self._resolve_batch_keys(instruments)
        result: dict[InstrumentId, Quote] = {}
        for start in range(0, len(keys), _UPSTOX_MAX_BATCH_SIZE):
            chunk = keys[start : start + _UPSTOX_MAX_BATCH_SIZE]
            body = self._request(
                "GET",
                "/market-quote/quotes",
                cache_read=True,
                params={"instrument_key": ",".join(chunk)})
            raw = unwrap_data(body)
            if not isinstance(raw, dict):
                continue
            for row in raw.values():
                if not isinstance(row, dict):
                    continue
                instrument = key_map.get(str(row.get("instrument_token", "")))
                if instrument is None:
                    continue
                result[instrument.instrument_id] = self._quote_from_row(instrument, row)
        return result

    def get_ohlc(
        self: UptoxFacade, instruments: Sequence[Instrument], interval: str = "1d"
    ) -> dict[InstrumentId, OHLC]:
        """OHLC snapshot via GET /market-quote/ohlc (interval: 1d, I1, I30)."""
        keys, key_map = self._resolve_batch_keys(instruments)
        if len(keys) > _UPSTOX_MAX_BATCH_SIZE:
            raise ValueError(f"up to {_UPSTOX_MAX_BATCH_SIZE} instruments per OHLC request")
        body = self._request(
            "GET", "/market-quote/ohlc", cache_read=True,
            params={"instrument_key": ",".join(keys), "interval": interval})
        raw = unwrap_data(body)
        result: dict[InstrumentId, OHLC] = {}
        if not isinstance(raw, dict):
            return result
        for row in raw.values():
            if not isinstance(row, dict):
                continue
            instrument = key_map.get(str(row.get("instrument_token", "")))
            ohlc = row.get("ohlc")
            if instrument is None or not isinstance(ohlc, dict):
                continue
            result[instrument.instrument_id] = OHLC(
                open=as_price(ohlc.get("open")),
                high=as_price(ohlc.get("high")),
                low=as_price(ohlc.get("low")),
                close=as_price(ohlc.get("close")))
        return result

    def get_quote(self: UptoxFacade, instrument: Instrument) -> Quote:
        """Single quote via GET /market-quote/quotes."""
        key = provider_key(self._registry, instrument.instrument_id)
        body = self._request(
            "GET", "/market-quote/quotes", cache_read=True, params={"instrument_key": key})
        row = self._market_row(unwrap_data(body), key)
        return self._quote_from_row(instrument, row)

    def depth(self: UptoxFacade, instrument: Instrument) -> Depth:
        """Market depth via GET /market-quote/quotes."""
        key = provider_key(self._registry, instrument.instrument_id)
        body = self._request(
            "GET", "/market-quote/quotes", cache_read=True, params={"instrument_key": key})
        row = self._market_row(unwrap_data(body), key)
        depth_data = (
            row.get("depth", {})
            if isinstance(row, dict) and isinstance(row.get("depth"), dict)
            else {}
        )
        # Sort the book (bids price-descending, asks ascending) so the depth
        # invariant ``Depth.best_bid/best_ask == [0]`` holds even if the
        # provider returns levels out of order.
        def _level(raw: dict[str, Any]) -> tuple[Price, Quantity]:
            return (
                as_price(raw.get("price")),
                Quantity(value=as_decimal(str(raw.get("quantity")))))

        bids = tuple(
            sorted(
                (_level(i) for i in depth_data.get("buy", []) if isinstance(i, dict)),
                key=lambda level: level[0].value,
                reverse=True)
        )
        asks = tuple(
            sorted(
                (_level(i) for i in depth_data.get("sell", []) if isinstance(i, dict)),
                key=lambda level: level[0].value)
        )
        return Depth(instrument=instrument, bids=bids, asks=asks, timestamp=datetime.now(UTC))

    def _fetch_candles(
        self: UptoxFacade,
        instrument: Instrument,
        timeframe: Timeframe | str,
        path: str,
        label: str,
        *,
        start: datetime,
        end: datetime) -> HistoricalSeries:
        """Fetch and parse v3 candle rows into a ``HistoricalSeries``."""
        body = self._request("GET", path, host="v3", cache_read=True)
        if isinstance(body, dict) and body.get("status") == "error":
            errors = body.get("errors", [])
            msg = errors[0].get("message", "unknown") if errors else "unknown"
            raise SDKError(f"{label} failed: {msg}")
        raw = unwrap_data(body)
        rows = raw.get("candles", []) if isinstance(raw, dict) else []
        value = timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)
        target = _target_timeframe(value)
        candles = _candles_from_rows(rows, instrument, target, start, end)
        return HistoricalSeries(
            instrument=instrument, timeframe=target, candles=candles, start=start, end=end
        )

    def history(
        self: UptoxFacade,
        instrument: Instrument,
        timeframe: Timeframe | str,
        start: datetime,
        end: datetime) -> HistoricalSeries:
        """Historical candles via V3 GET /historical-candle/:key/:unit/:interval/:to/:from."""
        key = provider_key(self._registry, instrument.instrument_id)
        value = timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)
        unit, interval = _unit_interval(value)
        path = f"/historical-candle/{key}/{unit}/{interval}/{end.date()}/{start.date()}"
        return self._fetch_candles(
            instrument, timeframe, path, "Upstox history", start=start, end=end
        )

    def intraday_candles(
        self: UptoxFacade,
        instrument: Instrument,
        timeframe: Timeframe | str) -> HistoricalSeries:
        """Current-day candles via V3 GET /historical-candle/intraday/:key/:unit/:interval."""
        key = provider_key(self._registry, instrument.instrument_id)
        value = timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)
        unit, interval = _unit_interval(value)
        if unit not in ("minutes", "hours", "days"):
            raise ValueError(f"unsupported intraday timeframe: {value!r}")
        now = datetime.now(UTC)
        path = f"/historical-candle/intraday/{key}/{unit}/{interval}"
        return self._fetch_candles(
            instrument, timeframe, path, "Upstox intraday", start=now, end=now
        )

    def get_option_chain(
        self: UptoxFacade,
        underlying: Instrument,
        expiry: date | str | None = None) -> OptionChain:
        """Option chain via GET /option/chain."""
        if expiry is None:
            raise CapabilityNotSupportedError(
                "Upstox option-chain requests require an explicit expiry"
            )
        key = provider_key(self._registry, underlying.instrument_id)
        params: dict[str, object] = {
            "instrument_key": key,
            "expiry_date": expiry.isoformat() if isinstance(expiry, date) else str(expiry),
        }
        body = self._request(
            "GET", "/option/chain", cache_read=True, params=params)
        data = unwrap_data(body)
        envelope_expiry = (
            parse_date(data.get("expiry", data.get("expiry_date")))
            if isinstance(data, dict)
            else None
        )
        envelope_spot = (
            data.get("underlying_spot_price", data.get("underlying_value"))
            if isinstance(data, dict)
            else None
        )
        rows = data.get("chain", data) if isinstance(data, dict) else data
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return OptionChain(underlying=underlying, _expiries=())
        option_exchange = "BFO" if underlying.exchange.value == "BSE" else "NFO"
        grouped: dict[date, list[OptionPair]] = {}
        references: dict[date, Price | None] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            expiry_date = parse_date(row.get("expiry", row.get("expiry_date"))) or envelope_expiry
            requested_expiry = parse_date(expiry)
            if expiry_date is None or requested_expiry is None or expiry_date != requested_expiry:
                continue
            strike_raw = row.get("strike_price", row.get("strike"))
            if expiry_date is None or strike_raw is None:
                continue
            try:
                strike = Decimal(str(strike_raw))
                call = Option.of(option_exchange, underlying.symbol, expiry_date, strike, "CE")
                put = Option.of(option_exchange, underlying.symbol, expiry_date, strike, "PE")
            except (TypeError, ValueError):
                continue
            call_data = row.get("call_options")
            put_data = row.get("put_options")
            call_key = (
                call_data.get("instrument_key", call_data.get("instrument_token"))
                if isinstance(call_data, dict) else None
            )
            put_key = (
                put_data.get("instrument_key", put_data.get("instrument_token"))
                if isinstance(put_data, dict) else None
            )
            # Only pair legs whose native key is truthy: a zero/empty
            # placeholder key would collapse to ``"0"``/``"None"`` and
            # collide across strikes in the registry, and a pair with an
            # unmapped leg would break quote/order lookups (Dhan parity fix).
            # A strike with only one real leg (single-sided row) is dropped
            # too — mapping it would leave the other leg unusable.
            if call_key and put_key:
                self._registry.register(call.instrument_id, {"key": str(call_key)})
                self._registry.register(put.instrument_id, {"key": str(put_key)})
                grouped.setdefault(expiry_date, []).append(
                    OptionPair(call=call, put=put, strike=Price(value=strike))
                )
            spot = row.get("underlying_spot_price", row.get("underlying_value", envelope_spot))
            references[expiry_date] = (
                as_price(spot) if spot is not None else references.get(expiry_date)
            )
        expiries = tuple(
            Expiry(
                underlying=underlying,
                expiry_date=expiry_date,
                pairs=tuple(sorted(pairs, key=lambda pair: pair.strike.value)),
                reference_price=references.get(expiry_date))
            for expiry_date, pairs in sorted(grouped.items())
        )
        return OptionChain(underlying=underlying, _expiries=expiries)

    def get_news(
        self: UptoxFacade,
        category: str,
        instrument_keys: list[str] | None = None,
        page_number: int | None = None,
        page_size: int | None = None) -> list[dict[str, object]]:
        """News via GET /news (Upstox v2 news feed).

        Real v2 contract (verified against the official Upstox docs):
        ``category`` is required — one of ``instrument_keys`` (pass the keys
        via ``instrument_keys``, max 30), ``positions``, or ``holdings`` —
        and the key parameter is the *plural* ``instrument_keys``. The
        response ``data`` maps each instrument key to an array of news items;
        those arrays are flattened into a single list here.
        """
        if category not in ("instrument_keys", "positions", "holdings"):
            raise ValueError(
                f"invalid Upstox news category: {category!r} "
                "(expected 'instrument_keys', 'positions', or 'holdings')"
            )
        if category == "instrument_keys" and not instrument_keys:
            raise ValueError(
                "instrument_keys is required when category='instrument_keys'"
            )
        if instrument_keys and len(instrument_keys) > 30:
            raise ValueError(
                "Upstox news supports at most 30 instrument keys per request"
            )
        params: dict[str, object] = {"category": category}
        # ``instrument_keys`` only applies to the ``instrument_keys`` category
        # (the API ignores it otherwise); always sent as the plural param.
        if category == "instrument_keys" and instrument_keys:
            params["instrument_keys"] = ",".join(instrument_keys)
        if page_number is not None:
            params["page_number"] = page_number
        if page_size is not None:
            params["page_size"] = page_size
        body = self._request("GET", "/news", cache_read=True, params=params)
        rows = unwrap_data(body)
        # Real v2 shape: ``data = {instrument_key: [news_item, ...]}``.
        # The unwrapped payload is therefore a dict of key -> item arrays;
        # flatten the arrays into one list. A bare list is accepted too.
        if isinstance(rows, dict):
            return [
                item
                for group in rows.values()
                if isinstance(group, list)
                for item in group
                if isinstance(item, dict)
            ]
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
        return []
