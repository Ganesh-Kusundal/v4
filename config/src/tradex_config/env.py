"""Environment variable loader for AppConfig.

Loads configuration from environment variables with sensible defaults.
"""

from __future__ import annotations

import os
import re
import shlex
from decimal import Decimal
from pathlib import Path

from tradex_domain import BrokerId
from tradex_domain.errors import SDKError

from tradex_config.schema import (
    AppConfig,
    BrokerConfig,
    ExecutionConfig,
    LoggingConfig,
    RiskConfig,
    default_feed_instruments,
    default_runtime_dir_str,
)


def _parse_bool(value: str) -> bool:
    """Parse a boolean string (1/true/yes/on → True, anything else → False)."""
    return value.strip().lower() in {"1", "true", "yes", "on"}


def from_env() -> AppConfig:
    """Load config from environment variables with sensible defaults.

    Environment variables:
    - TRADEX_BROKER: BrokerId (PAPER, DHAN, UPSTOX, REPLAY)
    - TRADEX_MODE: Execution mode (paper, backtest, replay, live)
    - TRADEX_RUNTIME_DIR: Runtime directory (defaults to the repo-anchored
      ``<repo>/runtime`` shared with the broker layer; see
      ``tradex_domain.paths``)
    - TRADEX_KILL_SWITCH: Kill switch default (true/false)
    - TRADEX_RISK_MAX_ORDER_VALUE: Max order value
    - TRADEX_RISK_MAX_POSITION_VALUE: Max position value
    - TRADEX_RISK_MAX_ORDERS_PER_MINUTE: Max orders per minute
    - TRADEX_FEES_ENABLED: Deduct brokerage/STT/etc from fills (true/false)
    - TRADEX_LOG_LEVEL: Root log level name (DEBUG/INFO/WARNING/ERROR/
      CRITICAL; default INFO). An unrecognised name raises rather than
      falling back, so a typo cannot quietly silence a trading process.
    - TRADEX_LOG_JSON: Emit JSON lines for log aggregation instead of the
      human-readable format (true/false; default false)
    - TRADEX_SLIPPAGE_BPS: Basis-points slippage on fill prices
    - TRADEX_FILL_REFERENCE: Strategy order timing (next_open | signal_close)
    - TRADEX_FEED_INSTRUMENTS: The tape a live session streams, as
      ``EXCHANGE:SYMBOL`` entries separated by commas
      (``NSE:RELIANCE,NSE:TCS``). **Unset means no tape**, which is what paper,
      backtest and replay want and what a live session refuses to start
      without — the platform is not asked to guess what to trade, so nothing
      here is defaulted to an index or a universe. Parsing, trimming and
      de-duplication live in ``schema.parse_feed_instruments``; a malformed
      entry raises rather than being dropped.
    """
    broker_id_str = os.environ.get("TRADEX_BROKER", "PAPER")
    try:
        broker_id = BrokerId(broker_id_str)
    except ValueError:
        broker_id = BrokerId.PAPER

    mode = os.environ.get("TRADEX_MODE", "paper")
    runtime_dir = default_runtime_dir_str()
    kill_switch_str = os.environ.get("TRADEX_KILL_SWITCH", "false")
    kill_switch = _parse_bool(kill_switch_str)

    # Risk config
    max_order_value_str = os.environ.get("TRADEX_RISK_MAX_ORDER_VALUE")
    max_order_value = Decimal(max_order_value_str) if max_order_value_str else None

    max_position_value_str = os.environ.get("TRADEX_RISK_MAX_POSITION_VALUE")
    max_position_value = Decimal(max_position_value_str) if max_position_value_str else None

    max_orders_per_minute_str = os.environ.get("TRADEX_RISK_MAX_ORDERS_PER_MINUTE")
    max_orders_per_minute = int(max_orders_per_minute_str) if max_orders_per_minute_str else None

    reject_unknown_market_value = _parse_bool(
        os.environ.get("TRADEX_RISK_REJECT_UNKNOWN_MARKET_VALUE", "false")
    )

    risk = RiskConfig(
        max_order_value=max_order_value,
        max_position_value=max_position_value,
        max_orders_per_minute=max_orders_per_minute,
        reject_unknown_market_value=reject_unknown_market_value,
        max_daily_loss_amt=(
            Decimal(v) if (v := os.environ.get("TRADEX_RISK_MAX_DAILY_LOSS")) else None
        ),
        max_drawdown_pct=(
            Decimal(v) if (v := os.environ.get("TRADEX_RISK_MAX_DRAWDOWN_PCT")) else None
        ),
    )

    slippage_bps_str = os.environ.get("TRADEX_SLIPPAGE_BPS")
    slippage_bps = Decimal(slippage_bps_str) if slippage_bps_str else None
    execution = ExecutionConfig(
        fees_enabled=_parse_bool(os.environ.get("TRADEX_FEES_ENABLED", "true")),
        slippage_bps=slippage_bps,
        fill_reference=os.environ.get("TRADEX_FILL_REFERENCE", "next_open"),
    )

    return AppConfig(
        broker_id=broker_id,
        mode=mode,
        risk=risk,
        runtime_dir=runtime_dir,
        kill_switch_default=kill_switch,
        broker=BrokerConfig(
            name=os.environ.get("TRADEX_BROKER_NAME", "paper"),
            environment=os.environ.get("TRADEX_BROKER_ENVIRONMENT", "PAPER"),
        ),
        execution=execution,
        # AC-17 / lane 5c: the structured-logging seam existed with no
        # production call site. boot() applies this, so a run's verbosity and
        # format are operator decisions rather than constants in the code.
        logging=LoggingConfig(
            level=os.environ.get("TRADEX_LOG_LEVEL", "INFO"),
            json_format=_parse_bool(os.environ.get("TRADEX_LOG_JSON", "false")),
        ),
        # The same per-call resolution ``runtime_dir`` uses, passed explicitly
        # for the same reason: this function is where an operator looks to see
        # which variable a setting comes from.
        feed_instruments=default_feed_instruments(),
    )


# ---------------------------------------------------------------------------
# Explicit opt-in env-file loader (ported from v3 runtime/live.py)
# ---------------------------------------------------------------------------

def load_env_file(path: str | Path, *, override: bool = False) -> tuple[str, ...]:
    """Load simple ``KEY=VALUE`` entries into this process only.

    This deliberately does not write files, expand variables, or print values.
    It is an explicit opt-in helper for local smoke commands; ``boot`` never
    discovers or loads credential files implicitly. Existing environment values
    win unless ``override=True`` is requested by the caller.
    """
    loaded: list[str] = []
    for number, raw_line in enumerate(Path(path).read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise SDKError(f"invalid environment entry at line {number}")
        name, raw_value = (part.strip() for part in line.split("=", 1))
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
            raise SDKError(f"invalid environment variable at line {number}")
        value = raw_value
        if raw_value[:1] in {"'", '"'}:
            try:
                parts = shlex.split(raw_value, comments=False, posix=True)
            except ValueError as exc:
                raise SDKError(f"invalid quoted environment value at line {number}") from exc
            if len(parts) != 1:
                raise SDKError(f"invalid environment value at line {number}")
            value = parts[0]
        if override or name not in os.environ:
            os.environ[name] = value
        loaded.append(name)
    return tuple(loaded)


__all__ = ["_parse_bool", "from_env", "load_env_file"]
