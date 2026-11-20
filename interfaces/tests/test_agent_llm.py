"""Pluggable chat model: deterministic echo by default, provider-agnostic seam."""

from __future__ import annotations

from tradex_interfaces.agent.llm import (
    ChatMessage,
    ChatModel,
    EchoChatModel,
    get_chat_model,
)


def test_echo_returns_last_user_message() -> None:
    model = EchoChatModel()
    out = model.complete(
        [ChatMessage("user", "backtest RELIANCE"), ChatMessage("assistant", "ok"),
         ChatMessage("user", "now run it")],
        tools=[],
    )
    assert out.role == "assistant"
    assert out.content == "now run it"


def test_echo_with_no_user_message_is_empty() -> None:
    assert EchoChatModel().complete([], tools=[]).content == ""


def test_default_model_is_echo_without_provider() -> None:
    assert isinstance(get_chat_model(), EchoChatModel)


def test_protocol_is_satisfied() -> None:
    assert isinstance(EchoChatModel(), ChatModel)


def test_provider_without_sdk_falls_back_to_echo(monkeypatch) -> None:
    # anthropic/openai SDKs are not installed: selecting a provider must degrade
    # gracefully to the deterministic echo, never crash the agent.
    monkeypatch.setenv("TRADEX_AGENT_PROVIDER", "anthropic")
    assert isinstance(get_chat_model(), EchoChatModel)
