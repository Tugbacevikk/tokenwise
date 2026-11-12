"""Thin wrapper around the model API, so the rest of the code (and the tests) see one method."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass
class Reply:
    text: str
    model: str
    usage: Usage
    role: str = ""


class LLM(Protocol):
    def complete(self, *, model: str, system: str, prompt: str, max_tokens: int) -> Reply: ...


class AnthropicLLM:
    def __init__(self, api_key: str | None = None) -> None:
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key)

    def complete(self, *, model: str, system: str, prompt: str, max_tokens: int) -> Reply:
        # The system part (instructions + project map) is stable between calls, so it is
        # marked for prompt caching: repeated reads cost a fraction of normal input tokens.
        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        u = response.usage
        usage = Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        return Reply(text=text, model=model, usage=usage)
