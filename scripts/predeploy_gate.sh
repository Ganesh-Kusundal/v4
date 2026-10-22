#!/usr/bin/env bash
# Every deploy-blocking check in one command. Safe to run from any directory.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v uv >/dev/null 2>&1; then
  echo "predeploy gate: uv is not on PATH" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "predeploy gate: docker is required to prove the deploy artifact" >&2
  exit 1
fi

echo "==> uv sync --frozen"
uv sync --frozen

echo "==> import every workspace package"
uv run --no-sync python - <<'PY'
import importlib

packages = [
    "tradex_domain",
    "tradex_brokers",
    "tradex_config",
    "tradex_observability",
    "tradex_analytics",
    "tradex_reactive",
    "tradex_execution",
    "tradex_strategy",
    "tradex_replay",
    "tradex_application",
    "tradex_market_data",
    "tradex_interfaces",
    "tradex_runtime",
    "duck_analytics",
]
for name in packages:
    importlib.import_module(name)
print(f"{len(packages)} packages import")
PY

echo "==> ruff ratchet"
uv run --no-sync python scripts/ruff_ratchet.py

echo "==> mypy ratchet"
uv run --no-sync python scripts/mypy_ratchet.py

echo "==> import-linter"
uv run --no-sync --with import-linter lint-imports

echo "==> full test suite"
uv run --no-sync python -m pytest -c pyproject.toml -q -p no:cacheprovider --timeout=300

echo "==> docker build"
docker build -f deploy/Dockerfile -t tradex-v4:predeploy .

echo "==> container smoke"
test "$(docker run --rm --entrypoint id tradex-v4:predeploy -u)" = "10001"
docker run --rm --entrypoint python tradex-v4:predeploy -c \
  "import tradex_domain, tradex_runtime, tradex_interfaces; print('imports ok')"
docker run --rm --entrypoint tradex tradex-v4:predeploy --help >/dev/null

echo "==> container paper boot"
docker run --rm -i -e TRADEX_RUNTIME_DIR=/tmp/tradex-runtime \
  --entrypoint python tradex-v4:predeploy - <<'PY'
from decimal import Decimal

from tradex_config import AppConfig, RiskConfig
from tradex_runtime import TradingSession

config = AppConfig(
    risk=RiskConfig(
        max_order_value=Decimal("100000"),
        max_position_value=Decimal("500000"),
        max_orders_per_minute=10,
        max_daily_loss_amt=Decimal("5000"),
        max_drawdown_pct=Decimal("5"),
    )
)
session = TradingSession.paper(config=config)
try:
    assert str(session.state) == "READY", session.state
    print("paper boot: READY")
finally:
    session.stop()
PY

echo "predeploy gate: PASS"
