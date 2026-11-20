"""``/account`` — broker account snapshot (balance/margin/equity)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from tradex_interfaces.models import AccountResponse
from tradex_interfaces.routes.deps import require_session

router = APIRouter()


@router.get("/account", response_model=AccountResponse)
async def get_account(session: Any = Depends(require_session(404))) -> AccountResponse:
    acct = session.broker.get_account()
    return AccountResponse(
        balance=str(acct.balance),
        margin=str(acct.margin),
        equity=str(acct.equity),
    )
