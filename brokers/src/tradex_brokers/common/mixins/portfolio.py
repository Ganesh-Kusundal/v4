"""Shared portfolio mixin — provider-neutral composites (REF-8).

Only the members whose bodies are byte-identical for Dhan and Upstox live
here. Endpoint-specific portfolio calls (account, profile, ledger, fund
limits, token status, positions, holdings, conversion, exit-all, margin,
trade books) differ in wire format and stay in the per-provider
``_portfolio.py`` modules.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from tradex_domain.execution import Account, PortfolioSnapshot
from tradex_domain.value_objects import Money

if TYPE_CHECKING:
    from tradex_brokers.common.mixins import ProviderFacade

__all__ = ["SharedPortfolioMixin"]


class SharedPortfolioMixin(Protocol):
    """Portfolio members shared verbatim by both provider clients."""

    def get_portfolio(self: ProviderFacade) -> PortfolioSnapshot:
        """Portfolio snapshot (positions + account)."""
        return PortfolioSnapshot(
            positions=self.get_positions(), account=self._account_for_portfolio()
        )

    def _account_for_portfolio(self: ProviderFacade) -> Account:
        """Normalize an ``Account`` snapshot so no ``Money`` field is ``None``.

        Mirrors the (identical) implementations in both provider clients; the
        concrete clients keep their own copy until ``dhan/client.py`` /
        ``upstox/client.py`` can adopt this mixin directly (outside REF-8
        file ownership).
        """
        snapshot = self.get_account()
        return Account(
            account_id=snapshot.account_id,
            balance=snapshot.balance or Money(amount=Decimal("0"), currency="INR"),
            margin=snapshot.margin or Money(amount=Decimal("0"), currency="INR"),
            equity=snapshot.equity or Money(amount=Decimal("0"), currency="INR"),
        )
