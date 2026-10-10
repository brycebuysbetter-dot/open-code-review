from dataclasses import dataclass
from typing import Any, Protocol

from bravo.config import get_settings


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class LLMResponse:
    stop_reason: str
    content: list[dict[str, Any]]  # normalized blocks: {"type": "text"|"tool_use", ...}


class LLM(Protocol):
    def create(self, *, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 2048) -> LLMResponse: ...


class AnthropicLLM:
    def __init__(self, client=None, model: str | None = None):
        s = get_settings()
        self.model = model or s.claude_model
        if client is None:
            if not s.anthropic_api_key:
                raise LLMUnavailable("ANTHROPIC_API_KEY is not configured")
            import anthropic

            # The SDK retries 429/5xx/connection errors with exponential backoff.
            client = anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=4, timeout=60)
        self.client = client

    def create(self, *, system, messages, tools, max_tokens=2048) -> LLMResponse:
        resp = self.client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system, messages=messages, tools=tools
        )
        blocks = []
        for b in resp.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        return LLMResponse(resp.stop_reason, blocks)


_llm: LLM | None = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = AnthropicLLM()
    return _llm


def set_llm(llm: LLM | None) -> None:
    global _llm
    _llm = llm
