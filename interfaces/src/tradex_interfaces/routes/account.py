"""``/account`` — broker account snapshot (balance/margin/equity)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from tradex_domain.constants import NO_SESSION_BOUND

from tradex_interfaces.auth.deps import require_auth
from tradex_interfaces.models import AccountResponse
from tradex_interfaces.routes.deps import get_session

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/account", response_model=AccountResponse)
async def get_account(session: Any | None = Depends(get_session)) -> AccountResponse:
    if session is None:
        raise HTTPException(status_code=404, detail=NO_SESSION_BOUND)
    acct = session.broker.get_account()
    return AccountResponse(
        balance=str(acct.balance),
        margin=str(acct.margin),
        equity=str(acct.equity),
    )
