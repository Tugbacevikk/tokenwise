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


class LLMError(Exception):
    """A model call failed in a way the user can fix (server down, unknown model, bad key)."""


def _post(client, url: str, body: dict, model: str, server: str):
    import httpx

    try:
        response = client.post(url, json=body)
    except httpx.ConnectError:
        raise LLMError(
            f"cannot reach {server}. Is the model server running? (for Ollama: `ollama serve`)"
        ) from None
    except httpx.TimeoutException:
        raise LLMError(f"{server} did not answer in time (model {model!r})") from None
    if response.status_code >= 400:
        try:
            detail = response.json().get("error") or response.text
        except ValueError:
            detail = response.text
        if isinstance(detail, dict):
            detail = detail.get("message") or str(detail)
        if response.status_code == 404:
            raise LLMError(
                f"model {model!r} was not found on {server} ({detail}). "
                f"Download it (`ollama pull {model}`) or fix `models` in tokenwise.toml; "
                "`tokenwise doctor` lists what the server has."
            )
        raise LLMError(f"{server} answered {response.status_code}: {detail}")
    return response.json()


class OpenAICompatibleLLM:
    """Talks to any server with the OpenAI chat API: LM Studio, vLLM, OpenAI itself."""

    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 600) -> None:
        import httpx

        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key or 'none'}"}
        self.client = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)

    def list_models(self) -> list[str]:
        return [m["id"] for m in _get(self.client, "/models", self.base_url).get("data", [])]

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
        data = _post(self.client, "/chat/completions", body, model, self.base_url)
        text = data["choices"][0]["message"].get("content") or ""
        u = data.get("usage") or {}
        usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        return Reply(text=text, model=model, usage=usage)


def _get(client, url: str, server: str) -> dict:
    import httpx

    try:
        response = client.get(url)
    except httpx.ConnectError:
        raise LLMError(f"cannot reach {server}. Is the model server running?") from None
    if response.status_code >= 400:
        raise LLMError(f"{server} answered {response.status_code}: {response.text[:200]}")
    return response.json()


class OllamaLLM:
    """Ollama's own chat API. Unlike its OpenAI-compatible endpoint, it lets us set the context
    window per request: Ollama's default (a few thousand tokens) would silently cut long prompts,
    such as the project map of a large repository."""

    def __init__(self, base_url: str, max_context: int = 32768, timeout: float = 1800) -> None:
        import httpx

        self.base_url = base_url.rstrip("/").removesuffix("/v1")
        self.max_context = max_context
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def context_for(self, system: str, prompt: str, max_tokens: int) -> int:
        """Room for the prompt (counted generously) plus the answer, rounded up to 2K steps."""
        need = (len(system) + len(prompt)) // 3 + max_tokens + 512
        size = max(4096, -(-need // 2048) * 2048)
        if size > self.max_context:
            raise LLMError(
                f"the prompt needs a context of about {need:,} tokens but max_context is "
                f"{self.max_context:,}. Raise max_context in tokenwise.toml if the model and "
                "your GPU allow it, or exclude folders from the map (extensions, ignore_dirs)."
            )
        return size

    def list_models(self) -> list[str]:
        return [m["name"] for m in _get(self.client, "/api/tags", self.base_url).get("models", [])]

    def complete(self, *, model: str, system: str, prompt: str, max_tokens: int) -> Reply:
        body = {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "options": {
                "temperature": 0,
                "num_predict": max_tokens,
                "num_ctx": self.context_for(system, prompt, max_tokens),
            },
        }
        data = _post(self.client, "/api/chat", body, model, self.base_url)
        text = (data.get("message") or {}).get("content") or ""
        usage = Usage(data.get("prompt_eval_count", 0), data.get("eval_count", 0))
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


OLLAMA_BASE = "http://localhost:11434"


class ConfigError(Exception):
    pass


def make_backend(settings: Settings) -> LLM:
    if settings.provider == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise ConfigError("ANTHROPIC_API_KEY is not set. Run `tokenwise init` first.")
        return AnthropicLLM()
    if settings.provider == "ollama":
        return OllamaLLM(settings.base_url or OLLAMA_BASE, settings.max_context)
    if settings.provider == "openai":
        if not settings.base_url:
            raise ConfigError('provider = "openai" needs `base_url` in tokenwise.toml.')
        return OpenAICompatibleLLM(settings.base_url, os.getenv("OPENAI_API_KEY"))
    raise ConfigError(
        f'unknown provider {settings.provider!r}; use "anthropic", "ollama" or "openai".'
    )


def cache_path(settings: Settings) -> Path:
    return settings.state_dir / "cache.db"


def make_llm(settings: Settings) -> LLM:
    backend = make_backend(settings)
    return CachedLLM(backend, cache_path(settings)) if settings.cache else backend
