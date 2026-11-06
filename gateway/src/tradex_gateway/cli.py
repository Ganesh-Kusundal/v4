"""Console entry point for the broker data gateway."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from tradex_gateway.brokers import build_runtimes
from tradex_gateway.config import GatewayConfig, load_local_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradex-gateway")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the gateway")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--workers", type=int, default=1)
    sub.add_parser("check", help="build brokers and print their status")
    parser.set_defaults(command="serve", host=None, port=None, workers=1)
    args = parser.parse_args(argv)

    load_local_env()
    config = GatewayConfig.from_env().validate()

    if args.command == "check":
        runtimes = build_runtimes(config)
        payload = {
            name: {
                "available": runtime.available,
                "error": runtime.error,
                "capabilities": (
                    asdict(runtime.capabilities) if runtime.capabilities is not None else None
                ),
            }
            for name, runtime in runtimes.items()
        }
        print(json.dumps(payload, indent=2, default=str))
        for runtime in runtimes.values():
            runtime.close()
        return 0 if any(runtime.available for runtime in runtimes.values()) else 1

    if args.workers != 1:
        print("tradex-gateway is single-process: per-process broker rate limiters", file=sys.stderr)
        return 2
    import uvicorn

    uvicorn.run(
        "tradex_gateway.app:create_app",
        factory=True,
        host=args.host or config.host,
        port=args.port or config.port,
        workers=1,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
