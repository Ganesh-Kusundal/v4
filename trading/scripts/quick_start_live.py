#!/usr/bin/env python3
"""Quick-start smoke test for a live TradeX session.

Usage::

    python trading/scripts/quick_start_live.py --broker dhan

Loads ``.env.local`` (credential presence check), performs a real auth probe
through ``build_broker_from_env`` (TOTP/token refresh included), then boots the
full live session via ``AppConfig`` + ``boot``. Prints a clear success message
or actionable failure guidance. Never prints credential values.

Exit codes:
    0  success (auth probe passed + full session booted)
    1  auth probe or session boot failed (message explains why)
    2  missing/invalid credential env vars (names only, see message)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Ensure project packages are importable
ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

log = logging.getLogger("tradex.scripts.quick_start")

# Credential env-var NAMES per broker. Values are never printed or logged.
# Dhan: client id + a token path (static access token OR TOTP mint pair).
# Upstox: client id + secret + one auth tier (refresh token OR TOTP OR static).
def _credential_problems(broker: str, env_prefix: str, env: dict[str, str]) -> list[str]:
    problems: list[str] = []
    if broker == "dhan":
        if not env.get(f"{env_prefix}CLIENT_ID"):
            problems.append(f"{env_prefix}CLIENT_ID is missing from .env.local")
        has_totp = bool(env.get(f"{env_prefix}PIN") and env.get(f"{env_prefix}TOTP_SECRET"))
        if not has_totp and not env.get(f"{env_prefix}ACCESS_TOKEN"):
            problems.append(
                f"{env_prefix}ACCESS_TOKEN is missing — set it, or set both "
                f"{env_prefix}PIN and {env_prefix}TOTP_SECRET for TOTP minting"
            )
    elif broker == "upstox":
        if not (env.get(f"{env_prefix}API_KEY") or env.get(f"{env_prefix}CLIENT_ID")):
            problems.append(f"{env_prefix}API_KEY (or {env_prefix}CLIENT_ID) is missing")
        if not (env.get(f"{env_prefix}API_SECRET") or env.get(f"{env_prefix}CLIENT_SECRET")):
            problems.append(f"{env_prefix}API_SECRET (or {env_prefix}CLIENT_SECRET) is missing")
        has_refresh = bool(env.get(f"{env_prefix}REFRESH_TOKEN"))
        has_totp = bool(
            env.get("UPSTOX_MOBILE")
            and env.get("UPSTOX_PIN")
            and env.get("UPSTOX_TOTP_SECRET")
        )
        has_static = bool(env.get(f"{env_prefix}ACCESS_TOKEN"))
        if not (has_refresh or has_totp or has_static):
            problems.append(
                f"no auth tier: set {env_prefix}REFRESH_TOKEN, or UPSTOX_MOBILE/"
                f"UPSTOX_PIN/UPSTOX_TOTP_SECRET, or {env_prefix}ACCESS_TOKEN"
            )
    return problems


def _build_and_probe(broker: str) -> tuple[object, str | None]:
    """Real auth probe via the standard interface. Returns (broker, error)."""
    from tradex_trading.runtime.live import build_broker_from_env

    broker_conn = build_broker_from_env(broker)
    try:
        broker_conn.connect()
        if not broker_conn.verify_connection():
            return broker_conn, (
                f"auth probe failed for {broker}: the broker could not verify the "
                "connection (rejected/missing token). Check DHAN_ENVIRONMENT/"
                "UPSTOX_ENVIRONMENT and re-run; tokens are minted on demand."
            )
    except Exception as exc:
        return broker_conn, (
            f"auth probe failed for {broker}: {type(exc).__name__}: {exc}. "
            "Check that .env.local has valid credentials and the network/venue "
            "is reachable."
        )
    return broker_conn, None


def _boot_session(broker: str, environment: str) -> tuple[object, str | None]:
    """Boot the full live session via AppConfig + boot. Returns (session, error)."""
    from tradex_domain import BrokerId

    from tradex_trading.config.schema import AppConfig
    from tradex_trading.runtime.startup import boot

    cfg = AppConfig(
        broker_id=BrokerId[broker.upper()],
        mode="live",
        live_enabled=True,
        environment=environment,
    )
    try:
        session = boot(cfg)
    except Exception as exc:
        return None, (
            f"session boot failed: {type(exc).__name__}: {exc}. The auth probe "
            "passed but full-session composition (engine/strategies/bus wiring) "
            "did not — see the traceback above."
        )
    return session, None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Quick-start smoke test for a live TradeX session"
    )
    p.add_argument("--broker", default="dhan", choices=["dhan", "upstox"],
                   help="Broker adapter (default: dhan)")
    p.add_argument("--env-file", default=".env.local",
                   help="Path to the credential env file (default: .env.local)")
    p.add_argument("--environment", default="LIVE", choices=["LIVE", "SANDBOX"],
                   help="Runtime environment (default: LIVE)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    env_file = Path(args.env_file)
    if not env_file.is_file():
        log.error("env file not found: %s — create it from .env.example", env_file)
        return 2

    from tradex_trading.config.env import load_env_file

    load_env_file(env_file)

    broker_upper = args.broker.upper()
    prefix = f"{broker_upper}_SANDBOX_" if args.environment == "SANDBOX" else f"{broker_upper}_"
    problems = _credential_problems(args.broker, prefix, os.environ)
    if problems:
        log.error("credential check failed for %s (env %s):", args.broker, args.environment)
        for problem in problems:
            log.error("  - %s", problem)
        log.error("values are never shown — fix the vars above in %s and retry", env_file)
        return 2
    log.info("credentials present for %s (%s) — probing auth...", args.broker, args.environment)

    broker, err = _build_and_probe(args.broker)
    if err:
        log.error("%s", err)
        broker.close()
        return 1
    broker.close()
    log.info("auth OK for %s — booting full live session...", args.broker)

    session, err = _boot_session(args.broker, args.environment)
    if err:
        log.error("%s", err)
        return 1

    try:
        print(f"\nLive session ready: broker={args.broker} mode=live env={args.environment}")
        print("session.market.history(...), session.market.ltp(...), session.trade, ...")
    finally:
        session.stop()

    log.info("quick-start complete — live session booted and stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
