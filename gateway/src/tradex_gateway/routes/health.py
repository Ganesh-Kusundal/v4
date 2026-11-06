"""``/v1/health`` and ``/v1/brokers`` — liveness and broker inventory."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request

from tradex_gateway.brokers import BrokerRuntime

router = APIRouter()


@router.get("/v1/health")
async def health(request: Request) -> dict[str, Any]:
    runtimes_now: dict[str, BrokerRuntime] = request.app.state.runtimes or {}
    ready = any(runtime.available for runtime in runtimes_now.values())
    return {
        "status": "ok" if ready else "degraded",
        "ready": ready,
        "brokers": [
            {
                "name": name,
                "available": runtime.available,
                "error": runtime.error,
                "health": runtime.health.snapshot(),
            }
            for name, runtime in runtimes_now.items()
        ],
        "feed": request.app.state.hub.status(),
    }


@router.get("/v1/brokers")
async def brokers(request: Request) -> dict[str, Any]:
    runtimes_now: dict[str, BrokerRuntime] = request.app.state.runtimes or {}
    cfg = request.app.state.config
    return {
        "brokers": [
            {
                "name": name,
                "available": runtime.available,
                "error": runtime.error,
                "capabilities": _caps(runtime),
                "health": runtime.health.snapshot(),
            }
            for name, runtime in runtimes_now.items()
        ],
        "primaries": {
            "live": cfg.primary_live,
            "quotes": cfg.primary_quotes,
            "history": cfg.primary_history,
            "depth": cfg.primary_depth,
            "chain": cfg.primary_chain,
        },
    }


def _caps(runtime: BrokerRuntime) -> dict[str, Any] | None:
    capabilities = runtime.capabilities
    if capabilities is None:
        return None
    try:
        data = asdict(capabilities)
    except TypeError:
        return {
            key: getattr(capabilities, key) for key in dir(capabilities) if not key.startswith("_")
        }
    return {key: list(value) if isinstance(value, tuple) else value for key, value in data.items()}
