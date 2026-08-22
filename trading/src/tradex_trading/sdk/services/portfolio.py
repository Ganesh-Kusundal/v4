"""PortfolioService — positions, holdings, account, portfolio snapshots."""

from __future__ import annotations

from tradex_domain.execution import Account, PortfolioSnapshot, Position
from tradex_domain.protocols import BrokerAdapter

from tradex_trading.execution.trading_cache import TradingCache


class PortfolioService:
    """Positions, holdings, account, portfolio snapshots (D-13)."""

    def __init__(self, broker: BrokerAdapter, cache: TradingCache) -> None:
        self._broker = broker
        self._cache = cache

    # v4 names (cache-first)
    def positions(self) -> list[Position]:
        """Get all positions from the local cache."""
        return self._cache.all_positions()

    def account(self) -> Account:
        """Get account snapshot from the broker."""
        return self._broker.get_account()

    def portfolio(self) -> PortfolioSnapshot:
        """Get portfolio snapshot from the broker."""
        return self._broker.get_portfolio()

    # broker-delegated names
    def holdings(self) -> list[Position]:
        """Get holdings from the broker."""
        return list(self._broker.get_holdings())

    def funds(self) -> dict[str, object]:
        """Broker fund limits / margin snapshot.

        Delegates to the broker's ``fund_limits`` surface when present
        (Dhan/Upstox); otherwise derives the available cash from the
        account snapshot (paper).
        """
        limits = getattr(self._broker, "fund_limits", None)
        if callable(limits):
            return dict(limits())
        account = self._broker.get_account()
        return {
            "available_cash": (
                str(account.balance) if account.balance is not None else None
            ),
        }


__all__ = ["PortfolioService"]
