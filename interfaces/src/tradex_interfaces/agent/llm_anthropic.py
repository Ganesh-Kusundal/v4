"""Anthropic (Claude) chat model adapter.

The ``anthropic`` SDK is an optional dependency, imported lazily so the agent
core stays importable and testable without it. ``get_chat_model`` falls back to
the deterministic echo if this import fails.
"""

from __future__ import annotations

import os

from tradex_interfaces.agent.llm import ChatMessage


class AnthropicChatModel:
    def __init__(self) -> None:
        import anthropic  # optional dependency

        self._client = anthropic.Anthropic()
        self._model = os.getenv("TRADEX_AGENT_MODEL", "claude-3-5-sonnet-latest")

    def complete(
        self, messages: list[ChatMessage], tools: list[dict]
    ) -> ChatMessage:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            messages=[
                {"role": m.role, "content": m.content}
                for m in messages
                if m.role in ("user", "assistant")
            ],
        )
        text = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", "") == "text"
        )
        return ChatMessage(role="assistant", content=text)
