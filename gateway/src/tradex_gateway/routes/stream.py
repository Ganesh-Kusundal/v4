"""``/v1/stream`` — the live-data WebSocket."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from tradex_gateway.auth import check_api_key
from tradex_gateway.errors import Unauthorized
from tradex_gateway.live import LiveHub
from tradex_gateway.models import (
    ClientMessage,
    PingMessage,
    StatusMessage,
    SubscribeMessage,
    UnsubscribeMessage,
)
from tradex_gateway.routes.deps import _now_ms

router = APIRouter()

_CLIENT_MESSAGE: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


@router.websocket("/v1/stream")
async def stream(ws: WebSocket) -> None:
    provided = ws.query_params.get("api_key") or ws.headers.get("x-api-key")
    try:
        check_api_key(provided, ws.app.state.config)
    except Unauthorized:
        await ws.close(code=1008)
        return
    await ws.accept()
    hub: LiveHub = ws.app.state.hub
    client_id = uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
    control: asyncio.Queue = asyncio.Queue(maxsize=256)
    loop = asyncio.get_running_loop()
    hub.add_client(client_id, loop, queue, control)
    await queue.put({"op": "status", **hub.status()})

    async def writer() -> None:
        while True:
            if hub.close_requested(client_id):
                await ws.close(code=1013)
                return
            control_task = asyncio.create_task(control.get())
            tick_task = asyncio.create_task(queue.get())
            done, pending = await asyncio.wait(
                {control_task, tick_task},
                timeout=20,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if not done:
                await ws.send_json({"op": "pong", "ts": _now_ms()})
                continue
            frame = next(iter(done)).result()
            await ws.send_json(frame)

    async def reader() -> None:
        while True:
            try:
                raw = await ws.receive_json()
            except (ValueError, KeyError, TypeError):
                # JSONDecodeError is a ValueError; a binary frame yields no
                # "text" key (KeyError) and an oversized int literal makes
                # json.loads raise plain ValueError. None of them may tear
                # down the connection. WebSocketDisconnect/RuntimeError are
                # deliberately left uncaught so teardown still propagates.
                await control.put(
                    {
                        "op": "error",
                        "code": "invalid_request",
                        "message": "invalid frame",
                    }
                )
                continue
            try:
                message = _CLIENT_MESSAGE.validate_python(raw)
            except ValidationError as exc:
                await control.put(
                    {
                        "op": "error",
                        "code": "invalid_request",
                        "message": exc.errors()[0]["msg"],
                    }
                )
                continue

            match message:
                case SubscribeMessage():
                    ack = await ws.app.state.run(
                        hub.subscribe,
                        client_id,
                        message.instruments,
                        message.mode,
                        message.levels,
                    )
                    await control.put(ack)
                case UnsubscribeMessage():
                    ack = await ws.app.state.run(
                        hub.unsubscribe,
                        client_id,
                        message.instruments,
                        message.subscription_id,
                    )
                    await control.put(ack)
                case PingMessage():
                    await control.put({"op": "pong", "ts": _now_ms()})
                case StatusMessage():
                    await control.put({"op": "status", **hub.status()})

    writer_task = asyncio.create_task(writer())
    reader_task = asyncio.create_task(reader())
    try:
        done, pending = await asyncio.wait(
            {writer_task, reader_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        for task in (writer_task, reader_task):
            task.cancel()
        await ws.app.state.run(hub.remove_client, client_id)
