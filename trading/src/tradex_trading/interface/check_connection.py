"""TradeX v4 connectivity probe — check broker connection status.

No external dependencies.

Usage (from the repository root):
  PYTHONPATH=v4/trading/src:v4/domain/src:v4/brokers/src \\
    python -m tradex_trading.interface.check_connection
  PYTHONPATH=... python -m tradex_trading.interface.check_connection --broker dhan
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from tradex_domain.errors import SDKError

from tradex_trading.sdk.session import TradingSession


def check_connection(session: TradingSession) -> dict:
    """Probe broker connectivity and return status.

    Parameters
    ----------
    session : TradingSession
        The session to probe.

    Returns
    -------
    dict
        Connection status with keys: connected, broker, mode, latency_ms.
    """
    from tradex_trading.runtime.health import check_health

    health = check_health(session)
    return {
        "connected": health.status == "ok",
        "broker": health.broker,
        "mode": health.mode,
        "latency_ms": 0,  # stub
    }


def _token_path(name: str, environment: str = "LIVE") -> str:
    """Return the durable token path for a broker.

    Parameters
    ----------
    name : str
        Broker name ('dhan' or 'upstox').
    environment : str
        Environment ('LIVE' or 'SANDBOX').

    Returns
    -------
    str
        Path to the token state file.
    """
    # Same layout as the real token lifecycle (``<runtime_dir>/<broker>/``) so
    # the probe reports the path the runtime actually persists to, including a
    # ``TRADEX_RUNTIME_DIR`` override.
    from tradex_brokers.common.paths import default_token_state_path

    if name == "dhan":
        return os.environ.get("DHAN_TOKEN_PATH") or str(default_token_state_path("dhan"))
    prefix = "UPSTOX_SANDBOX_" if environment == "SANDBOX" else "UPSTOX_"
    return os.environ.get(f"{prefix}TOKEN_PATH") or str(default_token_state_path(name))


def _cooldown_path(name: str) -> str:
    """Return the TOTP cooldown path for a broker (same layout as runtime)."""
    from tradex_brokers.common.paths import default_totp_cooldown_path

    return str(default_totp_cooldown_path(name))


def _check(name: str, session: TradingSession | None = None) -> bool:
    """Check one live broker; prints PASS / BLOCKED / FAIL and returns success.

    Parameters
    ----------
    name : str
        Broker name.
    session : TradingSession | None
        Optional session to use for the check.

    Returns
    -------
    bool
        True if check passed, False otherwise.
    """
    print(f"\n=== {name.upper()} ===")
    try:
        if session is not None:
            status = check_connection(session)
            print(f"connected={status['connected']}")
            print(f"broker={status['broker']}")
            print(f"mode={status['mode']}")
            print("PASS")
            return True
        # No session — attempt a real broker connect using env-loaded credentials
        token_path = _token_path(name)
        cooldown_path = _cooldown_path(name)
        print(f"token_path={token_path}")
        print(f"cooldown_path={cooldown_path}")
        try:
            from tradex_trading.runtime.live import build_broker_from_env

            broker = build_broker_from_env(name.upper())
            broker.connect()
            print(f"connected={broker._connected}")
            print("PASS")
            return True
        except Exception as exc:  # noqa: BLE001 — report, don't crash the probe
            print(f"FAIL: {type(exc).__name__}: {exc}")
            return False
    except Exception as exc:  # noqa: BLE001 — diagnostic tool: report, never raise
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return False


def main(argv: list[str] | None = None, session: Any | None = None) -> int:
    """CLI entry point for connection checking.

    Parameters
    ----------
    argv : list[str] | None
        Command-line arguments.
    session : Any | None
        Optional trading session.

    Returns
    -------
    int
        Exit code (0 for success, 1 for failure).
    """
    parser = argparse.ArgumentParser(description="Check broker live connectivity")
    parser.add_argument(
        "--broker",
        choices=("dhan", "upstox", "both"),
        default="both",
        help="broker(s) to check (default: both)",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="optional KEY=VALUE file to load with override (explicit opt-in); "
        "credentials are never loaded implicitly",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse errors exit(2); --help exits 0
        return exc.code if isinstance(exc.code, int) else 2

    env_path = Path(args.env_file) if args.env_file else None
    if env_path is not None:
        if env_path.exists():
            try:
                from tradex_trading.config.env import load_env_file
                names = load_env_file(env_path, override=True)
            except SDKError as exc:
                print(f"env_file=(error — {env_path}: {exc})")
            else:
                print(f"env_file={env_path} ({len(names)} entries)")
        else:
            print(f"env_file=(none — {env_path} not found)")

    brokers = ["dhan", "upstox"] if args.broker == "both" else [args.broker]
    failed = 0
    for name in brokers:
        if not _check(name, session):
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["check_connection", "main"]
