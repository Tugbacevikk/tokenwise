import httpx
import pytest

from tokenwise.config import Settings
from tokenwise.llm import CachedLLM, ConfigError, OpenAICompatibleLLM, make_backend, make_llm


def test_cache_hits_only_on_identical_calls(tmp_path, fake):
    inner = fake("first", "second")
    llm = CachedLLM(inner, tmp_path / ".tokenwise" / "cache.db")
    a = llm.complete(model="m", system="s", prompt="p", max_tokens=10)
    b = llm.complete(model="m", system="s", prompt="p", max_tokens=10)
    c = llm.complete(model="m", system="s", prompt="p changed", max_tokens=10)
    assert (a.text, a.cached) == ("first", False)
    assert (b.text, b.cached, b.usage.input_tokens) == ("first", True, 0)
    assert (c.text, c.cached) == ("second", False)
    assert len(inner.calls) == 2 and llm.count() == 2
    assert llm.clear() == 2 and llm.count() == 0


def test_openai_compatible_backend_parses_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            },
        )

    llm = OpenAICompatibleLLM("http://local/v1/", api_key="k")
    llm.client = httpx.Client(base_url="http://local/v1", transport=httpx.MockTransport(handler))
    llm.client.headers["authorization"] = "Bearer k"
    reply = llm.complete(model="qwen", system="sys", prompt="hi", max_tokens=5)
    assert reply.text == "ok" and reply.model == "qwen"
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (12, 3)
    assert seen["path"] == "/v1/chat/completions" and seen["auth"] == "Bearer k"
    assert b'"temperature":0' in seen["body"]


def test_make_backend_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        make_backend(Settings(root=tmp_path))
    with pytest.raises(ConfigError, match="base_url"):
        make_backend(Settings(root=tmp_path, provider="openai"))
    with pytest.raises(ConfigError, match="unknown provider"):
        make_backend(Settings(root=tmp_path, provider="nope"))


def test_make_llm_wraps_in_cache_unless_disabled(tmp_path):
    local = Settings(root=tmp_path, provider="openai", base_url="http://x/v1")
    assert isinstance(make_llm(local), CachedLLM)
    local.cache = False
    assert isinstance(make_llm(local), OpenAICompatibleLLM)
