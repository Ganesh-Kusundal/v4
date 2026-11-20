"""OpenAI chat model adapter.

The ``openai`` SDK is an optional dependency, imported lazily so the agent core
stays importable and testable without it. ``get_chat_model`` falls back to the
deterministic echo if this import fails.
"""

from __future__ import annotations

import os

from tradex_interfaces.agent.llm import ChatMessage


class OpenAIChatModel:
    def __init__(self) -> None:
        import openai  # optional dependency

        self._client = openai.OpenAI()
        self._model = os.getenv("TRADEX_AGENT_MODEL", "gpt-4o-mini")

    def complete(
        self, messages: list[ChatMessage], tools: list[dict]
    ) -> ChatMessage:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
        )
        return ChatMessage(
            role="assistant",
            content=response.choices[0].message.content or "",
        )
