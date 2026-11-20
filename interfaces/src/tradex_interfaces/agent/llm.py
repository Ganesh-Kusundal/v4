"""Pluggable chat models for the agent.

Provider-agnostic: :class:`ChatModel` is the seam. The default
:class:`EchoChatModel` is deterministic and needs no network, so the agent loop
is testable offline. A real provider is selected only when an SDK is installed
and ``TRADEX_AGENT_PROVIDER`` names it — the agent core never imports one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ChatMessage:
    role: str
    content: str


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ChatModel(Protocol):
    def complete(
        self, messages: list[ChatMessage], tools: list[dict]
    ) -> ChatMessage: ...


class EchoChatModel:
    """Deterministic stand-in: echoes the last user message. No network."""

    def complete(
        self, messages: list[ChatMessage], tools: list[dict]
    ) -> ChatMessage:
        last = next((m for m in reversed(messages) if m.role == "user"), None)
        return ChatMessage(role="assistant", content=last.content if last else "")


def get_chat_model() -> ChatModel:
    """Return the configured chat model.

    ``TRADEX_AGENT_PROVIDER=anthropic`` / ``openai`` selects a real provider if
    its SDK is importable; anything else (or an uninstalled SDK) falls back to
    the deterministic :class:`EchoChatModel` so the agent always runs offline.
    """
    provider = os.getenv("TRADEX_AGENT_PROVIDER", "").strip().lower()
    try:
        if provider in ("anthropic", "claude"):
            from tradex_interfaces.agent.llm_anthropic import AnthropicChatModel

            return AnthropicChatModel()
        if provider == "openai":
            from tradex_interfaces.agent.llm_openai import OpenAIChatModel

            return OpenAIChatModel()
    except Exception:
        pass
    return EchoChatModel()
