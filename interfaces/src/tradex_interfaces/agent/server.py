"""Dependency-free MCP (JSON-RPC 2.0) stdio server exposing the research tools.

MCP is JSON-RPC over stdio: ``initialize``, ``tools/list``, ``tools/call``. This
implements just enough of the protocol to be a real MCP server without pulling in
the SDK; a client (Claude Desktop, an IDE, or the agent loop) launches it and
calls the tools. All tools are the deterministic core — no LLM, no broker.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from tradex_interfaces.agent.tools import (
    TOOL_SPECS,
    backtest,
    list_strategies,
)

_PROTOCOL_VERSION = "2024-11-05"
_SERVER_INFO = {"name": "tradex-agent", "version": "0.1.0"}


def _dispatch(name: str, arguments: dict[str, Any]) -> Any:
    if name == "backtest":
        return backtest(**arguments)
    if name == "list_strategies":
        return list_strategies()
    raise KeyError(name)


def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """Handle one JSON-RPC request object; return the response *body*.

    The caller stamps ``jsonrpc``/``id`` and decides whether to write it (only
    for requests, not notifications).
    """
    method = request.get("method")
    if method == "initialize":
        return {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": _SERVER_INFO,
        }
    if method == "tools/list":
        return {"tools": TOOL_SPECS}
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            result = _dispatch(str(name), dict(arguments))
        except Exception as exc:  # tool errors are reported, not raised to the client
            return {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}
    return {"error": {"code": -32601, "message": f"unknown method: {method!r}"}}


def serve(stdin: TextIO, stdout: TextIO) -> None:
    """Run the stdio loop: read one JSON-RPC message per line, write responses."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        is_request = request.get("id") is not None
        if not is_request:
            continue  # notification: no response
        body = handle_request(request)
        response = {"jsonrpc": "2.0", "id": request.get("id"), **body}
        stdout.write(json.dumps(response) + "\n")
        stdout.flush()


def main() -> None:
    serve(sys.stdin, sys.stdout)
