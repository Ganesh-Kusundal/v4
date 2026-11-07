"""Trading environment factory — the zero-discrepancy backbone.

Every environment (live, paper, backtest, replay) is constructed by this
factory using the same component interfaces. Only the *implementations*
differ; the strategy sees an identical surface in all cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from tradex_domain.clocks import LiveClock, ReplayClock, SimClock, TradingClock


class EnvironmentType(Enum):
    LIVE = "live"
    PAPER = "paper"
    BACKTEST = "backtest"
    REPLAY = "replay"


@dataclass
class TradingEnvironment:
    """A fully wired trading environment.

    Contains every dependency a strategy needs, selected by environment type.
    Strategies are constructed once and injected into any environment.
    """

    env_type: EnvironmentType
    clock: TradingClock
    market_data: Any  # MarketDataProvider
    order_gateway: Any  # OrderGateway
    portfolio: Any  # Portfolio
    event_bus: Any  # ReactiveBus
    risk_manager: Any  # RiskManager


class EnvironmentFactory:
    """Factory for creating trading environments.

    Ensures consistent wiring across all environment types. The strategy
    code is identical regardless of which environment runs it.
    """

    def __init__(self, config: Any, event_bus: Any) -> None:
        self._config = config
        self._bus = event_bus

    def create_with_strategy(
        self, env_type: EnvironmentType, strategy_name: str, strategy_params: dict | None = None, **kwargs: Any
    ) -> tuple[TradingEnvironment, Any]:
        """Create environment and instantiate a registered strategy."""
        from tradex_strategy.registry import create as create_strategy
        env = self.create(env_type, **kwargs)
        strategy = create_strategy(strategy_name, strategy_params)
        return env, strategy

    def create(self, env_type: EnvironmentType, **kwargs: Any) -> TradingEnvironment:
        if env_type == EnvironmentType.LIVE:
            return self._create_live(**kwargs)
        if env_type == EnvironmentType.PAPER:
            return self._create_paper(**kwargs)
        if env_type == EnvironmentType.BACKTEST:
            return self._create_backtest(**kwargs)
        if env_type == EnvironmentType.REPLAY:
            return self._create_replay(**kwargs)
        raise ValueError(f"Unknown environment type: {env_type}")

    def _create_live(self, **kwargs: Any) -> TradingEnvironment:
        from tradex_domain.clocks import LiveClock

        return TradingEnvironment(
            env_type=EnvironmentType.LIVE,
            clock=LiveClock(),
            market_data=kwargs.get("market_data"),
            order_gateway=kwargs.get("order_gateway"),
            portfolio=kwargs.get("portfolio"),
            event_bus=self._bus,
            risk_manager=kwargs.get("risk_manager"),
        )

    def _create_paper(self, **kwargs: Any) -> TradingEnvironment:
        from tradex_domain.clocks import LiveClock

        return TradingEnvironment(
            env_type=EnvironmentType.PAPER,
            clock=LiveClock(),
            market_data=kwargs.get("market_data"),
            order_gateway=kwargs.get("order_gateway"),
            portfolio=kwargs.get("portfolio"),
            event_bus=self._bus,
            risk_manager=kwargs.get("risk_manager"),
        )

    def _create_backtest(self, **kwargs: Any) -> TradingEnvironment:
        start = kwargs.get("start")
        return TradingEnvironment(
            env_type=EnvironmentType.BACKTEST,
            clock=SimClock(start),
            market_data=kwargs.get("market_data"),
            order_gateway=kwargs.get("order_gateway"),
            portfolio=kwargs.get("portfolio"),
            event_bus=self._bus,
            risk_manager=kwargs.get("risk_manager"),
        )

    def _create_replay(self, **kwargs: Any) -> TradingEnvironment:
        timestamps = kwargs.get("timestamps", [])
        return TradingEnvironment(
            env_type=EnvironmentType.REPLAY,
            clock=ReplayClock(timestamps),
            market_data=kwargs.get("market_data"),
            order_gateway=kwargs.get("order_gateway"),
            portfolio=kwargs.get("portfolio"),
            event_bus=self._bus,
            risk_manager=kwargs.get("risk_manager"),
        )
