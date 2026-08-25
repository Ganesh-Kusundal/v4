"""``/orders`` — submit, list, modify, cancel orders against the bound session."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from tradex_trading.interface.models import OrderResponse
from tradex_trading.interface.routes.deps import get_session

log = logging.getLogger(__name__)

router = APIRouter()


def verify_api_key(request: Request) -> None:
    """FastAPI dep: check ``X-API-Key`` against ``app.state.api_key``.

    Read endpoints (GET) do not require auth. Write endpoints
    (POST/PUT/DELETE) declare this as a ``Depends``.
    """
    expected = request.app.state.api_key
    if expected is None:
        return
    if request.headers.get("X-API-Key") != expected:
        raise HTTPException(status_code=403, detail="Invalid API key")



def _build_order_request(body: dict, session: Any) -> Any:
    """Parse an HTTP order body into a domain OrderRequest.

    Accepts either ``"instrument_id": "EXCHANGE:SYMBOL"`` or the pair
    ``"exchange"`` + ``"symbol"``. Raises ``HTTPException(422)`` on
    missing or malformed fields.
    """
    from tradex_domain.enums import OrderSide, OrderType, TimeInForce
    from tradex_domain.execution import OrderRequest
    from tradex_domain.instruments import Equity
    from tradex_domain.value_objects import Price, Quantity

    instrument_id = body.get("instrument_id")
    if instrument_id:
        parts = instrument_id.split(":", 1)
        if len(parts) != 2:
            raise HTTPException(
                status_code=422,
                detail="instrument_id must be in 'EXCHANGE:SYMBOL' format",
            )
        instrument = Equity.of(parts[0], parts[1])
    else:
        exchange = body.get("exchange")
        symbol = body.get("symbol")
        if not exchange or not symbol:
            raise HTTPException(
                status_code=422,
                detail="provide either 'instrument_id' or both 'exchange' and 'symbol'",
            )
        instrument = Equity.of(exchange, symbol)

    side = OrderSide(body["side"])
    order_type = OrderType(body.get("order_type", "MARKET"))
    quantity = Quantity(Decimal(str(body["quantity"])))
    price = Price(Decimal(str(body["price"]))) if body.get("price") is not None else None
    time_in_force = (
        TimeInForce(body["time_in_force"])
        if body.get("time_in_force")
        else TimeInForce.DAY
    )
    return OrderRequest(
        instrument=instrument,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
        time_in_force=time_in_force,
    )


@router.get("/orders", response_model=list[OrderResponse])
async def list_orders(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    session: Any | None = Depends(get_session),
) -> list[OrderResponse]:
    if session is None:
        return []
    orders = session.trade.get_orderbook() if hasattr(session.trade, "get_orderbook") else []
    result = [
        OrderResponse(order_id=str(o.order_id), status=str(o.status))
        for o in orders
    ]
    if status is not None:
        result = [o for o in result if o.status.lower() == status.lower()]
    return result[offset : offset + limit]


@router.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    session: Any | None = Depends(get_session),
) -> OrderResponse:
    if session is None:
        raise HTTPException(status_code=404, detail="No session bound")
    try:
        order = session.trade.get_order(order_id)
        return OrderResponse(order_id=str(order.order_id), status=str(order.status))
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/orders", response_model=OrderResponse, dependencies=[Depends(verify_api_key)])
async def place_order(
    body: dict,
    session: Any | None = Depends(get_session),
) -> OrderResponse:
    if session is None:
        raise HTTPException(status_code=503, detail="no session bound")
    try:
        request = _build_order_request(body, session)
        receipt = session.trade.submit(request)
        return OrderResponse(
            order_id=receipt.order_id.value,
            status=str(receipt.status),
            message=receipt.message,
        )
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"missing required field: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("order submission failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put("/orders/{order_id}", response_model=OrderResponse, dependencies=[Depends(verify_api_key)])
async def modify_order(
    order_id: str,
    body: dict,
    session: Any | None = Depends(get_session),
) -> OrderResponse:
    if session is None:
        raise HTTPException(status_code=400, detail="no session bound")
    try:
        from tradex_domain.enums import OrderSide, OrderType, TimeInForce
        from tradex_domain.execution import OrderRequest
        from tradex_domain.value_objects import Price, Quantity

        instrument = session.broker.search(body["symbol"])[0]
        side = OrderSide(body["side"])
        order_type = OrderType(body.get("order_type", "LIMIT"))
        quantity = Quantity(Decimal(str(body["quantity"])))
        price = Price(Decimal(str(body["price"]))) if body.get("price") is not None else None
        time_in_force = (
            TimeInForce(body["time_in_force"])
            if body.get("time_in_force")
            else TimeInForce.DAY
        )
        request = OrderRequest(
            instrument=instrument,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            time_in_force=time_in_force,
        )
        order = session.trade.modify_order(order_id, request)
        return OrderResponse(order_id=str(order.order_id), status="modified")
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"missing required field: {exc}") from exc
    except IndexError as exc:
        raise HTTPException(status_code=422, detail="unknown symbol") from exc
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/orders/{order_id}", response_model=OrderResponse, dependencies=[Depends(verify_api_key)])
async def cancel_order(
    order_id: str,
    session: Any | None = Depends(get_session),
) -> OrderResponse:
    if session is None:
        raise HTTPException(status_code=400, detail="no session bound")
    try:
        order = session.trade.cancel(order_id)
        return OrderResponse(order_id=str(order.order_id), status="cancelled")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
