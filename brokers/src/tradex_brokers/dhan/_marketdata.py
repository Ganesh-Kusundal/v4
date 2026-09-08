"""Dhan REST client mixin — MarketDataMixin.

Mixed into :class:`~tradex_brokers.dhan.client.DhanApiClient`; the
facade owns shared state and internal helpers.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Instrument, Option
from tradex_domain.market import Depth, HistoricalSeries, Quote
from tradex_domain.market_calendar import (
    DHAN_SESSION_CLOSE as _SESSION_CLOSE,
)
from tradex_domain.market_calendar import (
    DHAN_SESSION_OPEN as _SESSION_OPEN,
)
from tradex_domain.market_calendar import (
    MARKET_CLOSE_STR as _DEFAULT_SESSION_CLOSE,
)
from tradex_domain.market_calendar import (
    MARKET_OPEN_STR as _DEFAULT_SESSION_OPEN,
)
from tradex_domain.options import Expiry, OptionChain, OptionPair
from tradex_domain.value_objects import InstrumentId, Price

from tradex_brokers.common.client_shared import parse_timestamp_fallback
from tradex_brokers.common.provider_common import (
    as_price,
    parse_date,
    require_success,
    unwrap_data,
)

if TYPE_CHECKING:
    from tradex_brokers.dhan._facade import DhanClientFacade


class MarketDataMixin(Protocol):
    def _validated(self: DhanClientFacade, body: object) -> object:
        """Reject failure bodies before parsing (never fabricate zeros).

        Dhan serves rate-limit / token failures as ``status: "failed"``
        bodies (often with ``_http_status: 429``) — parsing those into a
        silent ``Price(0)`` / empty series masks the outage. ``require_success``
        raises ``RateLimitError``/``SDKError``/``AuthenticationError`` instead
        (portfolio mixin parity).
        """
        if isinstance(body, dict):
            require_success(body)
        return body

    def ltp(self: DhanClientFacade, instrument: Instrument) -> Price:
        """Last traded price via POST /marketfeed/ltp."""
        payload = {
            self._segment(instrument): [int(self._security_id(instrument.instrument_id))]
        }
        body = self._validated(
            self._request("POST", "/marketfeed/ltp", json=payload, cache_read=True)
        )
        raw = unwrap_data(body)
        segment = raw.get(self._segment(instrument), {}) if isinstance(raw, dict) else {}
        row = (
            segment.get(self._security_id(instrument.instrument_id), {})
            if isinstance(segment, dict)
            else {}
        )
        return (
            as_price(row.get("ltp", row.get("last_price")))
            if isinstance(row, dict)
            else as_price(None)
        )


    def ltp_batch(
        self: DhanClientFacade, instruments: Sequence[Instrument]
    ) -> dict[InstrumentId, Price]:
        """Native batch LTP: one POST /marketfeed/ltp for all symbols."""
        segment_map, id_map = self._batch_segment_map(instruments)
        if not segment_map:
            return {}
        body = self._validated(
            self._request("POST", "/marketfeed/ltp", json=segment_map, cache_read=True)
        )
        raw = unwrap_data(body)
        result: dict[InstrumentId, Price] = {}
        if not isinstance(raw, dict):
            return result
        for rows in raw.values():
            if not isinstance(rows, dict):
                continue
            for security_id, info in rows.items():
                instrument = id_map.get(str(security_id))
                if instrument is None or not isinstance(info, dict):
                    continue
                result[instrument.instrument_id] = as_price(
                    info.get("ltp", info.get("last_price"))
                )
        return result


    def quote_batch(
        self: DhanClientFacade, instruments: Sequence[Instrument]
    ) -> dict[InstrumentId, Quote]:
        """Native batch quote: one POST /marketfeed/quote for all symbols."""
        segment_map, id_map = self._batch_segment_map(instruments)
        if not segment_map:
            return {}
        body = self._validated(
            self._request("POST", "/marketfeed/quote", json=segment_map, cache_read=True)
        )
        raw = unwrap_data(body)
        result: dict[InstrumentId, Quote] = {}
        if not isinstance(raw, dict):
            return result
        for rows in raw.values():
            if not isinstance(rows, dict):
                continue
            for security_id, info in rows.items():
                instrument = id_map.get(str(security_id))
                if instrument is None or not isinstance(info, dict):
                    continue
                result[instrument.instrument_id] = self._quote_from_row(instrument, info)
        return result


    def get_quote(self: DhanClientFacade, instrument: Instrument) -> Quote:
        """Single quote via POST /marketfeed/quote."""
        payload = {
            self._segment(instrument): [int(self._security_id(instrument.instrument_id))]
        }
        body = self._validated(
            self._request("POST", "/marketfeed/quote", json=payload, cache_read=True)
        )
        raw = unwrap_data(body)
        segment = raw.get(self._segment(instrument), {}) if isinstance(raw, dict) else {}
        row = (
            segment.get(self._security_id(instrument.instrument_id), {})
            if isinstance(segment, dict)
            else {}
        )
        if not isinstance(row, dict):
            row = {}
        return self._quote_from_row(instrument, row)


    def depth(self: DhanClientFacade, instrument: Instrument) -> Depth:
        """Market depth via POST /marketfeed/quote."""
        from tradex_brokers.common.market_builders import build_depth

        payload = {
            self._segment(instrument): [int(self._security_id(instrument.instrument_id))]
        }
        body = self._validated(
            self._request("POST", "/marketfeed/quote", json=payload, cache_read=True)
        )
        raw = unwrap_data(body)
        segment = raw.get(self._segment(instrument), {}) if isinstance(raw, dict) else {}
        row = (
            segment.get(self._security_id(instrument.instrument_id), {})
            if isinstance(segment, dict)
            else {}
        )
        depth_data = (
            row.get("depth", {})
            if isinstance(row, dict) and isinstance(row.get("depth"), dict)
            else {}
        )
        return build_depth(instrument, depth_data=depth_data)


    def history(
        self: DhanClientFacade,
        instrument: Instrument,
        timeframe: Timeframe | str,
        start: datetime,
        end: datetime,
    ) -> HistoricalSeries:
        """Historical candles via POST /charts/intraday or /charts/historical."""
        from tradex_domain.timeframe import dhan_interval

        interval = timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)
        # Single-sourced interval map (domain/timeframe.py) — None means unsupported (M30/W1)
        dhan_int = dhan_interval(interval)
        requested_timeframe = {
            "1m": Timeframe.M1,
            "5m": Timeframe.M5,
            "15m": Timeframe.M15,
            "1h": Timeframe.H1,
            "1d": Timeframe.D1,
        }.get(interval)
        if requested_timeframe is None or dhan_int is None and interval != "1d":
            raise ValueError(f"unsupported Dhan timeframe: {interval!r}")
        segment = self._segment(instrument)
        security_id = self._security_id(instrument.instrument_id)
        native_type = self._history_instrument_type(instrument)
        if dhan_int is not None:
            session_open = _SESSION_OPEN.get(segment, _DEFAULT_SESSION_OPEN)
            session_close = _SESSION_CLOSE.get(segment, _DEFAULT_SESSION_CLOSE)
            path = "/charts/intraday"
            # During market hours, use the actual end time so we get bars
            # up to "now" instead of waiting for session close.
            now = datetime.now()
            end_time = (
                min(end.time(), now.time())
                if end.date() == now.date()
                else datetime.strptime(session_close, "%H:%M:%S").time()
            )
            params: dict[str, object] = {
                "securityId": security_id,
                "exchangeSegment": segment,
                "instrument": native_type,
                "interval": dhan_int,
                "fromDate": f"{start.date()} {session_open}",
                "toDate": f"{end.date()} {end_time.strftime('%H:%M:%S')}",
            }
        else:
            path = "/charts/historical"
            params = {
                "securityId": security_id,
                "exchangeSegment": segment,
                "instrument": native_type,
                "expiryCode": 0,
                "fromDate": start.strftime("%Y-%m-%d"),
                "toDate": end.strftime("%Y-%m-%d"),
            }
        body = self._validated(
            self._request("POST", path, json=params, cache_read=True)
        )
        raw = unwrap_data(body)
        if not isinstance(raw, dict):
            raw = {}
        opens = raw.get("open", [])
        if not isinstance(opens, list):
            opens = []
        timestamps = raw.get("timestamp", []) if isinstance(raw.get("timestamp", []), list) else []
        highs = raw.get("high", []) if isinstance(raw.get("high", []), list) else []
        lows = raw.get("low", []) if isinstance(raw.get("low", []), list) else []
        closes = raw.get("close", []) if isinstance(raw.get("close", []), list) else []
        volumes = raw.get("volume", []) if isinstance(raw.get("volume", []), list) else []
        row_count = min(
            len(opens), len(timestamps), len(highs),
            len(lows), len(closes), len(volumes))
        from tradex_brokers.common.market_builders import make_candle

        candles = [
            make_candle(
                instrument,
                requested_timeframe,
                open=opens[i],
                high=highs[i],
                low=lows[i],
                close=closes[i],
                volume=volumes[i],
                timestamp=parse_timestamp_fallback(timestamps[i], start),
            )
            for i in range(row_count)
        ]
        return HistoricalSeries(
            instrument=instrument,
            timeframe=requested_timeframe,
            candles=candles,
            start=start,
            end=end)


    def get_option_chain(
        self: DhanClientFacade,
        underlying: Instrument,
        expiry: object | None = None) -> OptionChain:
        """Option chain via POST /optionchain/expirylist + /optionchain."""
        key = self._security_id(underlying.instrument_id)
        numeric_key = int(key) if str(key).isdigit() else key
        expiry_body = self._validated(
            self._request(
                "POST", "/optionchain/expirylist", json={
                    "UnderlyingScrip": numeric_key,
                    "UnderlyingSeg": self._segment(underlying),
                },
                cache_read=True
            )
        )
        expiry_data = unwrap_data(expiry_body)
        if isinstance(expiry_data, dict):
            expiry_values = expiry_data.get("expiryList", expiry_data.get("expiries", []))
        elif isinstance(expiry_data, list):
            expiry_values = expiry_data
        else:
            expiry_values = []
        requested_expiry = parse_date(expiry) if expiry is not None else None
        if expiry is not None and requested_expiry is None:
            raise ValueError(f"invalid Dhan option-chain expiry: {expiry!r}")
        raw_expiries = expiry_values if isinstance(expiry_values, list) else []
        if requested_expiry is not None:
            raw_expiries = [
                raw_e for raw_e in raw_expiries if parse_date(raw_e) == requested_expiry
            ]
        expiries: list[Expiry] = []
        for raw_expiry in raw_expiries:
            expiry_date = parse_date(raw_expiry)
            if expiry_date is None:
                continue
            chain_body = self._validated(
                self._request(
                    "POST", "/optionchain", json={
                        "UnderlyingScrip": numeric_key,
                        "UnderlyingSeg": self._segment(underlying),
                        "Expiry": str(raw_expiry),
                    },
                    cache_read=True
                )
            )
            data = unwrap_data(chain_body)
            if not isinstance(data, dict):
                data = {}
            spot = as_price(data["last_price"]) if data.get("last_price") is not None else None
            raw_strikes = data.get("oc", {})
            pairs: list[OptionPair] = []
            if isinstance(raw_strikes, dict):
                for raw_strike, legs in sorted(
                    raw_strikes.items(), key=lambda item: Decimal(str(item[0]))
                ):
                    if not isinstance(legs, dict):
                        continue
                    try:
                        strike = Decimal(str(raw_strike))
                        call = Option.of(
                            self._option_exchange(underlying),
                            underlying.symbol,
                            expiry_date,
                            strike,
                            "CE")
                        put = Option.of(
                            self._option_exchange(underlying),
                            underlying.symbol,
                            expiry_date,
                            strike,
                            "PE")
                    except (TypeError, ValueError):
                        continue
                    call_leg = legs.get("ce")
                    put_leg = legs.get("pe")
                    call_key = (
                        call_leg.get("security_id", call_leg.get("securityId"))
                        if isinstance(call_leg, dict)
                        else None
                    )
                    put_key = (
                        put_leg.get("security_id", put_leg.get("securityId"))
                        if isinstance(put_leg, dict)
                        else None
                    )
                    # Dhan returns placeholder legs (``security_id`` == 0) for
                    # deep-ITM strikes on far expiries that have no live
                    # contract. A zero id must not be registered: ``str(0)``
                    # is ``"0"``, and the *next* placeholder leg would then
                    # collide in the registry (SDKError). Only real contracts
                    # (truthy security id) are mapped and paired.
                    if call_key and put_key:
                        self._registry.register(call.instrument_id, {"key": str(call_key)})
                        self._registry.register(put.instrument_id, {"key": str(put_key)})
                        pairs.append(
                            OptionPair(call=call, put=put, strike=Price(value=strike))
                        )
            expiries.append(
                Expiry(
                    underlying=underlying,
                    expiry_date=expiry_date,
                    pairs=tuple(pairs),
                    reference_price=spot)
            )
        return OptionChain(underlying=underlying, _expiries=tuple(expiries))
