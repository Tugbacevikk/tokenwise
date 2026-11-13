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


def _ollama(handler, max_context=32768):
    from tokenwise.llm import OllamaLLM

    llm = OllamaLLM("http://localhost:11434/v1", max_context=max_context)
    llm.client = httpx.Client(base_url=llm.base_url, transport=httpx.MockTransport(handler))
    return llm


def test_ollama_sets_context_window_and_reads_usage():
    import json

    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.read())
        return httpx.Response(
            200, json={"message": {"content": "hi"}, "prompt_eval_count": 900, "eval_count": 7}
        )

    llm = _ollama(handler)
    assert llm.base_url == "http://localhost:11434"  # a /v1 suffix from an old config is dropped
    reply = llm.complete(model="qwen", system="s" * 30_000, prompt="p", max_tokens=400)
    assert seen["path"] == "/api/chat" and seen["body"]["stream"] is False
    opts = seen["body"]["options"]
    assert opts["num_predict"] == 400 and opts["temperature"] == 0
    assert opts["num_ctx"] >= 10_000 + 400 and opts["num_ctx"] % 2048 == 0
    assert (reply.text, reply.usage.input_tokens, reply.usage.output_tokens) == ("hi", 900, 7)
    assert _ollama(handler).context_for("s", "p", 100) == 4096


def test_ollama_errors_are_readable():
    from tokenwise.llm import LLMError

    missing = _ollama(
        lambda r: httpx.Response(404, json={"error": "model 'qwen2.5-coder:7b' not found"})
    )
    with pytest.raises(LLMError, match="ollama pull qwen2.5-coder:7b"):
        missing.complete(model="qwen2.5-coder:7b", system="s", prompt="p", max_tokens=10)

    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="ollama serve"):
        _ollama(down).complete(model="m", system="s", prompt="p", max_tokens=10)
    with pytest.raises(LLMError, match="max_context"):
        _ollama(down, max_context=4096).complete(
            model="m", system="s" * 40_000, prompt="", max_tokens=10
        )
    broken = _ollama(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(LLMError, match="500"):
        broken.complete(model="m", system="s", prompt="p", max_tokens=10)


def test_list_models():
    from tokenwise.llm import OpenAICompatibleLLM

    ollama = _ollama(lambda r: httpx.Response(200, json={"models": [{"name": "qwen2.5-coder:3b"}]}))
    assert ollama.list_models() == ["qwen2.5-coder:3b"]
    openai = OpenAICompatibleLLM("http://x/v1")
    openai.client = httpx.Client(
        base_url="http://x/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"id": "a"}]})),
    )
    assert openai.list_models() == ["a"]


def test_make_backend_ollama(tmp_path):
    from tokenwise.llm import OllamaLLM

    backend = make_backend(Settings(root=tmp_path, provider="ollama", max_context=8192))
    assert isinstance(backend, OllamaLLM) and backend.max_context == 8192
    assert backend.base_url == "http://localhost:11434"


def test_strip_thinking():
    from tokenwise.llm import strip_thinking

    assert strip_thinking("<think>\nSEARCH maybe\n</think>\n\nanswer") == "answer"
    assert strip_thinking("reasoning without opening tag</think>answer") == "answer"
    assert strip_thinking("plain <think> mention") == "plain <think> mention"
