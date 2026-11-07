"""``/quotes``, ``/search``, ``/history``, ``/option-chain``, ``/future-chain``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from tradex_domain.constants import NO_SESSION_BOUND

from tradex_interfaces.auth.deps import require_auth
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

router = APIRouter(dependencies=[Depends(require_auth)])


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
        from tradex_domain.serialization import to_dict
        from tradex_domain.value_objects import InstrumentId

        iid = InstrumentId.parse(f"{exchange}:{symbol}")
        instrument = instrument_from_id(iid)
        quote = session.broker.get_quote(instrument)
        raw = to_dict(quote) if not isinstance(quote, dict) else quote
        return QuoteResponse(**raw)
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
