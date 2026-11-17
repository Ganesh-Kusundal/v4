"""``/quotes``, ``/search``, ``/history``, ``/option-chain``, ``/future-chain``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from tradex_domain.constants import NO_SESSION_BOUND

from tradex_interfaces.contracts import (
    FutureChainRequest,
    FutureChainResponse,
    HistoryBar,
    HistoryRequest,
    OptionChainRequest,
    OptionChainResponse,
    QuoteRequest,
    QuoteResponse,
    SearchRequest,
    SearchResultItem,
)
from tradex_interfaces.routes._market_helpers import (
    enrich_chain_live,
    resolve_underlying_instrument,
    serialize_option_chain,
)
from tradex_interfaces.routes.deps import get_session

router = APIRouter()


@router.get(
    "/quotes/{exchange}:{symbol}",
    response_model=QuoteResponse,
)
async def get_quote(
    exchange: str,
    symbol: str,
    session: Any | None = Depends(get_session),
) -> QuoteResponse:
    """Get real-time quote for an instrument."""
    if session is None:
        raise HTTPException(status_code=404, detail=NO_SESSION_BOUND)
    try:
        from tradex_brokers.common.provider_common import instrument_from_id
        from tradex_domain.value_objects import InstrumentId

        iid = InstrumentId.parse(f"{exchange}:{symbol}")
        instrument = instrument_from_id(iid)
        quote = session.broker.get_quote(instrument)

        if isinstance(quote, dict):
            # Dict-form quote (defensive: every broker returns a Quote object).
            # Values may be Price/Quantity objects, to_dict() dicts, or numbers.
            def _num(val):
                if val is None:
                    return None
                if isinstance(val, dict) and "value" in val:
                    val = val["value"]
                elif hasattr(val, "value"):
                    val = val.value
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return None

            return QuoteResponse(
                instrument_id=str(quote.get("instrument_id") or f"{exchange}:{symbol}"),
                symbol=quote.get("symbol") or symbol,
                exchange=quote.get("exchange") or exchange,
                last_price=_num(quote.get("last_price", quote.get("ltp"))),
                bid=_num(quote.get("bid")),
                ask=_num(quote.get("ask")),
                volume=_num(quote.get("volume")),
                timestamp=str(quote["timestamp"]) if quote.get("timestamp") else None,
            )

        # Quote object: explicit field mapping. Never splat to_dict() output
        # into QuoteResponse — Price/Quantity serialize to {'value': ...}
        # dicts, which fail the float fields (the /quotes 502).
        return QuoteResponse(
            instrument_id=str(quote.instrument.instrument_id),
            symbol=quote.instrument.symbol,
            exchange=str(quote.instrument.exchange.value),
            last_price=float(quote.ltp.value),
            bid=float(quote.bid.value) if quote.bid is not None else None,
            ask=float(quote.ask.value) if quote.ask is not None else None,
            volume=float(quote.volume.value) if quote.volume is not None else None,
            timestamp=quote.timestamp.isoformat(),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get(
    "/search",
    response_model=list[SearchResultItem],
)
async def search_instruments(
    q: str,
    session: Any | None = Depends(get_session),
) -> list[SearchResultItem]:
    """Search instruments by query string."""
    if session is None:
        return []
    try:
        results = session.broker.search(q)
        return [
            SearchResultItem(
                instrument_id=str(instrument.instrument_id),
                symbol=instrument.symbol,
                exchange=str(instrument.exchange),
                asset_class=str(instrument.asset_class.value).lower(),
            )
            for instrument in results
        ]
    except Exception:
        return []


@router.get(
    "/history/{instrument_id}",
    response_model=list[HistoryBar],
)
async def get_history(
    instrument_id: str,
    timeframe: str = "1d",
    limit: int = 100,
    session: Any | None = Depends(get_session),
) -> list[HistoryBar]:
    """Get historical bars for an instrument."""
    if session is None:
        raise HTTPException(status_code=400, detail=NO_SESSION_BOUND)
    try:
        from tradex_brokers.common.provider_common import instrument_from_id
        from tradex_domain.enums import Timeframe
        from tradex_domain.value_objects import InstrumentId

        iid = InstrumentId.parse(instrument_id)
        instrument = instrument_from_id(iid)
        tf = Timeframe(timeframe)
        history = session.broker.history(instrument, tf)
        bars = []
        for bar in history:
            bars.append(HistoryBar(
                timestamp=str(bar.timestamp),
                open=float(bar.ohlc.open.value),
                high=float(bar.ohlc.high.value),
                low=float(bar.ohlc.low.value),
                close=float(bar.ohlc.close.value),
                volume=float(bar.volume.value) if bar.volume else 0,
            ))
        return bars[:limit]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/option-chain/{underlying}",
    response_model=OptionChainResponse,
)
async def get_option_chain(
    underlying: str,
    expiry: str | None = None,
    live: bool = False,
    session: Any | None = Depends(get_session),
) -> OptionChainResponse:
    """Option chain for an underlying.

    ``underlying`` accepts ``EXCHANGE:SYMBOL`` (e.g. ``MCX:GOLD``), a
    registry alias/key, or a bare symbol resolved from the loaded master.
    NFO/BFO chains come from the live REST endpoint (OI/volume/greeks);
    MCX and other non-NFO exchanges are derived from the instrument master.
    An optional ``expiry`` (YYYY-MM-DD) filters to a single expiry.
    ``live=true`` enriches the nearest expiry's strikes with real-time
    LTP / OI / volume (best-effort batch quotes for the ATM region).
    """
    if session is None:
        raise HTTPException(status_code=400, detail=NO_SESSION_BOUND)
    try:
        inst = resolve_underlying_instrument(session, underlying)
        chain = session.broker.get_option_chain(inst, expiry)
        if live:
            raw = enrich_chain_live(session, chain)
            return OptionChainResponse(**raw)
        raw = serialize_option_chain(chain)
        return OptionChainResponse(**raw)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/future-chain/{underlying}",
    response_model=FutureChainResponse,
)
async def get_future_chain(
    underlying: str,
    session: Any | None = Depends(get_session),
) -> FutureChainResponse:
    """Future contracts on an underlying, derived from the loaded master."""
    if session is None:
        raise HTTPException(status_code=400, detail=NO_SESSION_BOUND)
    try:
        inst = resolve_underlying_instrument(session, underlying)
        futures = session.broker.future_chain(inst)
        return FutureChainResponse(
            underlying=str(inst.instrument_id),
            futures=[
                {
                    "instrument": str(f.instrument_id),
                    "symbol": f.symbol,
                    "expiry": f.expiry.isoformat() if getattr(f, "expiry", None) else None,
                }
                for f in futures
            ],
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
