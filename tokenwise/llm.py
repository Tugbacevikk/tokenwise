"""Model backends behind one `complete` method, plus a cache that sits in front of them."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Settings


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
    cached: bool = False


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


class OpenAICompatibleLLM:
    """Talks to any server with the OpenAI chat API: Ollama, LM Studio, vLLM, OpenAI itself."""

    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 600) -> None:
        import httpx

        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key or 'none'}"}
        self.client = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)

    def complete(self, *, model: str, system: str, prompt: str, max_tokens: int) -> Reply:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        response = self.client.post("/chat/completions", json=body)
        response.raise_for_status()
        data = response.json()
        text = data["choices"][0]["message"].get("content") or ""
        u = data.get("usage") or {}
        usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        return Reply(text=text, model=model, usage=usage)


class CachedLLM:
    """Returns a stored reply when the exact same call was made before, else asks the model.

    The key covers model, instructions and the full prompt. File contents are inside the
    prompt, so any edit to a sent file is a different key and never gets a stale answer.
    """

    def __init__(self, inner: LLM, db_path: Path) -> None:
        self.inner = inner
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, model TEXT, text TEXT, "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __del__(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    @staticmethod
    def key(model: str, system: str, prompt: str, max_tokens: int) -> str:
        raw = json.dumps([model, system, prompt, max_tokens])
        return hashlib.sha256(raw.encode()).hexdigest()

    def complete(self, *, model: str, system: str, prompt: str, max_tokens: int) -> Reply:
        key = self.key(model, system, prompt, max_tokens)
        row = self.db.execute("SELECT text FROM cache WHERE key = ?", (key,)).fetchone()
        if row:
            return Reply(text=row[0], model=model, usage=Usage(), cached=True)
        reply = self.inner.complete(
            model=model, system=system, prompt=prompt, max_tokens=max_tokens
        )
        self.db.execute(
            "INSERT OR REPLACE INTO cache (key, model, text) VALUES (?, ?, ?)",
            (key, model, reply.text),
        )
        self.db.commit()
        return reply

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0]

    def clear(self) -> int:
        n = self.count()
        self.db.execute("DELETE FROM cache")
        self.db.commit()
        return n


class ConfigError(Exception):
    pass


def make_backend(settings: Settings) -> LLM:
    if settings.provider == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise ConfigError("ANTHROPIC_API_KEY is not set. Run `tokenwise init` first.")
        return AnthropicLLM()
    if settings.provider == "openai":
        if not settings.base_url:
            raise ConfigError('provider = "openai" needs `base_url` in tokenwise.toml.')
        return OpenAICompatibleLLM(settings.base_url, os.getenv("OPENAI_API_KEY"))
    raise ConfigError(f'unknown provider {settings.provider!r}; use "anthropic" or "openai".')


def cache_path(settings: Settings) -> Path:
    return settings.state_dir / "cache.db"


def make_llm(settings: Settings) -> LLM:
    backend = make_backend(settings)
    return CachedLLM(backend, cache_path(settings)) if settings.cache else backend
