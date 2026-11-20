"""MCP/agent surface over the deterministic core.

Tools are pure, JSON-safe functions (:mod:`tools`); the chat model is pluggable
(:mod:`llm`); the MCP stdio server (:mod:`server`) exposes the tools to any MCP
client. No LLM or broker is imported by default.
"""

from tradex_interfaces.agent.llm import ChatMessage, ChatModel, EchoChatModel
from tradex_interfaces.agent.server import handle_request, serve
from tradex_interfaces.agent.tools import (
    TOOL_SPECS,
    backtest,
    bars_to_records,
    list_strategies,
    result_to_dict,
    run_backtest,
)

__all__ = [
    "ChatMessage",
    "ChatModel",
    "EchoChatModel",
    "TOOL_SPECS",
    "backtest",
    "bars_to_records",
    "handle_request",
    "list_strategies",
    "result_to_dict",
    "run_backtest",
    "serve",
]
