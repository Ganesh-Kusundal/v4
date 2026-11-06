"""Helpers shared by the gateway route modules."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from functools import partial
from typing import Any

from tradex_gateway.errors import BadRequest


async def _run(state: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(state.executor, partial(fn, *args, **kwargs))


def _query_moment(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadRequest(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise BadRequest("timestamp must include a UTC offset")
    return parsed


def _now_ms() -> int:
    return int(time.time() * 1000)
