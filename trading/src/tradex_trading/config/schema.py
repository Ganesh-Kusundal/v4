"""Application configuration schema.

Defines the configuration structure for the v4 trading platform.
Frozen dataclass tree with strict shape validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from tradex_domain import BrokerId


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Risk management configuration."""

    max_order_value: Decimal | None = None
    max_position_value: Decimal | None = None
    max_orders_per_minute: int | None = None
    kill_switch_default: bool = False
    max_order_notional: Decimal | float | None = None

    def __post_init__(self) -> None:
        if self.max_order_notional is not None:
            object.__setattr__(
                self,
                "max_order_notional",
                Decimal(str(self.max_order_notional)),
            )
        if self.max_order_value is not None and not isinstance(self.max_order_value, Decimal):
            object.__setattr__(
                self,
                "max_order_value",
                Decimal(str(self.max_order_value)),
            )
        if self.max_position_value is not None and not isinstance(self.max_position_value, Decimal):
            object.__setattr__(
                self,
                "max_position_value",
                Decimal(str(self.max_position_value)),
            )


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    """Broker connection configuration."""

    name: str = "paper"
    environment: str = "PAPER"


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Logging configuration."""

    level: str = "INFO"


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    """Observability (metrics/tracing) configuration."""

    enabled: bool = True


@dataclass(frozen=True, slots=True)
class PersistenceConfig:
    """Optional local SQLite durability for orders and idempotency results.

    ``path`` is deliberately opt-in: the default runtime remains in-memory and
    creates no files. When set, both stores share this SQLite database.
    """

    path: str | None = None


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Application configuration.

    Attributes
    ----------
    broker_id : BrokerId
        The broker to use (default: PAPER).
    mode : str
        Execution mode: "paper", "backtest", "replay", "live".
    risk : RiskConfig
        Risk management configuration.
    runtime_dir : str
        Directory for runtime data (logs, cache, etc.).
    log_level : str
        Logging level.
    kill_switch_default : bool
        Default state of the kill switch.
    live_enabled : bool
        Whether live trading is explicitly enabled.
    environment : str
        Runtime environment (PAPER, SANDBOX, LIVE).
    broker : BrokerConfig
        Broker connection configuration.
    logging : LoggingConfig
        Logging configuration.
    observability : ObservabilityConfig
        Observability configuration.
    persistence : PersistenceConfig
        Persistence configuration.
    """

    broker_id: BrokerId = BrokerId.PAPER
    mode: str = "paper"
    risk: RiskConfig = field(default_factory=RiskConfig)
    runtime_dir: str = ".tradex_v4"
    log_level: str = "INFO"
    kill_switch_default: bool = False
    live_enabled: bool = False
    live_orders_enabled: bool = False
    environment: str = "PAPER"
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    persistence: PersistenceConfig = field(default_factory=PersistenceConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        """Build an AppConfig from a plain dictionary with strict validation."""
        allowed = {
            "broker_id",
            "mode",
            "risk",
            "runtime_dir",
            "log_level",
            "kill_switch_default",
            "live_enabled",
            "live_orders_enabled",
            "environment",
            "broker",
            "logging",
            "observability",
            "persistence",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown config sections: {sorted(unknown)}")

        broker_id_str = str(data.get("broker_id", "PAPER"))
        try:
            broker_id = BrokerId(broker_id_str)
        except ValueError:
            broker_id = BrokerId.PAPER

        broker = _build(BrokerConfig, data.get("broker"))
        risk = _build(RiskConfig, data.get("risk"))
        logging_cfg = _build(LoggingConfig, data.get("logging"))
        obs = _build(ObservabilityConfig, data.get("observability"))
        persistence = _build(PersistenceConfig, data.get("persistence"))

        return cls(
            broker_id=broker_id,
            mode=str(data.get("mode", "paper")),
            risk=risk,
            runtime_dir=str(data.get("runtime_dir", ".tradex_v4")),
            log_level=str(data.get("log_level", "INFO")),
            kill_switch_default=bool(data.get("kill_switch_default", False)),
            live_enabled=bool(data.get("live_enabled", False)),
            live_orders_enabled=bool(data.get("live_orders_enabled", False)),
            environment=str(data.get("environment", "PAPER")),
            broker=broker,
            logging=logging_cfg,
            observability=obs,
            persistence=persistence,
        )


def _build(cls: type, data: object) -> Any:
    """Construct a frozen dataclass from an optional dict with strict keys."""
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ValueError(f"{cls.__name__} config must be an object")
    allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys: {sorted(unknown)}")
    return cls(**data)


__all__ = [
    "AppConfig",
    "BrokerConfig",
    "LoggingConfig",
    "ObservabilityConfig",
    "PersistenceConfig",
    "RiskConfig",
]
