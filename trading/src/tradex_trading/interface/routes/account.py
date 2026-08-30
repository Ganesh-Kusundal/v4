"""``/account`` — broker account snapshot (balance/margin/equity)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from tradex_trading.interface.models import AccountResponse
from tradex_trading.interface.routes.deps import get_session

router = APIRouter()


@router.get("/account", response_model=AccountResponse)
async def get_account(session: Any | None = Depends(get_session)) -> AccountResponse:
    if session is None:
        raise HTTPException(status_code=404, detail="no session bound")
    acct = session.broker.get_account()
    return AccountResponse(
        balance=str(acct.balance),
        margin=str(acct.margin),
        equity=str(acct.equity),
    )
