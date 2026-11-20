"""MCP JSON-RPC stdio server: initialize, tools/list, tools/call, notifications."""

from __future__ import annotations

import io
import json

from tradex_interfaces.agent.server import handle_request, serve
from tradex_interfaces.agent.tools import TOOL_SPECS


def test_initialize_reports_server_info() -> None:
    body = handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert body["serverInfo"]["name"] == "tradex-agent"
    assert "protocolVersion" in body


def test_tools_list_advertises_specs() -> None:
    body = handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in body["tools"]}
    assert names == {spec["name"] for spec in TOOL_SPECS}


def test_tools_call_list_strategies() -> None:
    body = handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "list_strategies", "arguments": {}}}
    )
    assert body["content"][0]["type"] == "text"
    assert isinstance(json.loads(body["content"][0]["text"]), list)


def test_tools_call_unknown_returns_iserror() -> None:
    body = handle_request(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "nope", "arguments": {}}}
    )
    assert body.get("isError") is True


def test_unknown_method_is_error() -> None:
    body = handle_request({"jsonrpc": "2.0", "id": 5, "method": "bogus"})
    assert "error" in body


def test_serve_writes_response_for_request_not_notification() -> None:
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    )
    stdout = io.StringIO()
    serve(stdin, stdout)
    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1  # only the request got a response
    response = json.loads(lines[0])
    assert response["id"] == 1
    assert response["jsonrpc"] == "2.0"
