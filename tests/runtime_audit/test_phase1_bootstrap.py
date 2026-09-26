"""Phase 1: Composition Root & Bootstrap Runtime Verification.
Tests fail-closed behavior, configuration consistency, and session initialization.
"""

from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from tradex_domain import BrokerId, SessionStateError
from tradex_domain.enums import OrderSide, OrderType
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_runtime.startup import boot, RuntimeContext
from tradex_runtime.session import TradingSession, SessionState


def test_bootstrap_paper_session_success():
    """Verify clean composition root bootstrap in paper mode."""
    ctx = boot(mode="paper")
    try:
        assert isinstance(ctx, RuntimeContext)
        assert ctx.session is not None
        assert ctx.session.state == SessionState.READY
        assert ctx.engine is not None
        assert ctx.bus is not None
        assert ctx.cache is not None

        # Verify initial cash is allocated
        account = ctx.session.account
        assert account is not None
        assert account.balance.amount > Decimal("0")
    finally:
        ctx.close()


def test_bootstrap_live_fails_without_credentials():
    """Verify boot fails closed immediately when live broker credentials are missing."""
    with pytest.raises(Exception) as exc_info:
        boot(mode="live", broker="dhan")
    err_msg = str(exc_info.value).upper()
    assert "DHAN" in err_msg or "CREDENTIAL" in err_msg or "CLIENT_ID" in err_msg or "TOKEN" in err_msg
