"""MP-2: strategy attribution survives mark/fee rebuilds.

A fill opens a position attributed to a strategy. The position must keep that
attribution through fee deduction, quote-driven re-mark (accountant path),
and the live mark-to-market service update. Realized PnL must equal the fold
of domain math: opening fill contributes zero realized; the fee reduces
realized PnL by its amount.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import Money, OrderId, Price, Quantity
from tradex_execution.mark_to_market import MarkToMarketService
from tradex_execution.position_accountant import PositionAccountant
from tradex_execution.trading_cache import TradingCache

_INSTRUMENT = Equity.of("NSE", "ATTR")
_STRATEGY = "strat-attribution"
_TS = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def _fill(qty: str = "10", price: str = "100") -> Fill:
    return Fill(
        order_id=OrderId(value="oid-attr"),
        instrument=_INSTRUMENT,
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        timestamp=_TS,
        strategy_id=_STRATEGY,
    )


def _quote(ltp: str = "110", bid: str = "109") -> Quote:
    return Quote(
        instrument=_INSTRUMENT,
        bid=Price(value=Decimal(bid)),
        ask=Price(value=Decimal("111")),
        ltp=Price(value=Decimal(ltp)),
        timestamp=_TS,
    )


def test_strategy_attribution_stable_across_fill_fee_remark_mtm() -> None:
    cache = TradingCache()
    accountant = PositionAccountant(cache)

    # 1. Opening fill attributes the position to the strategy.
    pos = accountant.on_fill(_fill())
    assert pos.strategy_id == _STRATEGY

    # 2. Fee deduction rebuilds the position; attribution must survive.
    fee = Money(amount=Decimal("5"))
    pos = accountant.on_fee(_fill(), fee)
    assert pos is not None
    assert pos.strategy_id == _STRATEGY

    # 3. Quote-driven re-mark (accountant path) rebuilds; attribution survives.
    pos = accountant.remark(_quote())
    assert pos is not None
    assert pos.strategy_id == _STRATEGY

    # 4. Live mark-to-market service update rebuilds; attribution survives.
    service = MarkToMarketService(cache)
    try:
        updated = service.on_quote(_quote())
        assert updated is not None
        final = cache.get_position(_INSTRUMENT)
        assert final is not None
        assert final.strategy_id == _STRATEGY
        # Realized PnL fold: opening fill 0 - fee 5 = -5.
        assert final.realized_pnl.amount == Decimal("-5")
    finally:
        service.close()
