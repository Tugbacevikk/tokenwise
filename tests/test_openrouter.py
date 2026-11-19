"""OpenRouter: one key, many companies' models; live prices keep costs and budgets right."""

import json
import os
import time

import httpx
import pytest

from tokenwise import openrouter
from tokenwise.budget import BudgetedLLM, RequestBudgetExceeded
from tokenwise.config import Settings, load_settings
from tokenwise.llm import ConfigError, make_backend, openrouter_backend
from tokenwise.pricing import reply_cost

MODELS = {"data": [
    {"id": "~deepseek/deepseek-flash-latest",
     "pricing": {"prompt": "0.000000035", "completion": "0.00000015"}},
    {"id": "anthropic/claude-sonnet-5.5",
     "pricing": {"prompt": "0.000002", "completion": "0.00001", "input_cache_read": "0.0000002"}},
    {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}},
    {"id": "broken", "pricing": {}},
]}  # fmt: skip


def fake_openrouter(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS)
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "merhaba"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            "model": body["model"],
        })  # fmt: skip

    return httpx.MockTransport(handler)


def test_prices_are_read_per_million_and_unpriced_routers_are_skipped():
    prices = openrouter.parse_prices(MODELS["data"])
    assert prices["~deepseek/deepseek-flash-latest"] == [0.035, 0.035, 0.035, 0.15]
    assert prices["anthropic/claude-sonnet-5.5"] == [2.0, 2.0, 0.2, 10.0]
    assert "openrouter/auto" not in prices and "broken" not in prices


def test_backend_sends_the_key_and_knows_every_price(tmp_path):
    seen: list = []
    settings = Settings(root=tmp_path, provider="openrouter",
                        prices={"anthropic/claude-sonnet-5.5": [1, 1, 1, 1]})  # fmt: skip
    backend = openrouter_backend(settings, "sk-or-x", tmp_path / "p.json", fake_openrouter(seen))
    reply = backend.complete(model="~deepseek/deepseek-flash-latest", system="s", prompt="p",
                             max_tokens=100)  # fmt: skip
    assert reply.text == "merhaba"
    call = seen[-1]
    assert str(call.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert call.headers["authorization"] == "Bearer sk-or-x" and call.headers["x-title"]
    # the live price makes the ledger right; what tokenwise.toml sets wins
    assert reply_cost(reply, settings.prices) == pytest.approx((1000 * 0.035 + 500 * 0.15) / 1e6)
    assert settings.prices["anthropic/claude-sonnet-5.5"] == [1, 1, 1, 1]
    assert backend.priced is True
    # and the request ceiling holds for it too
    budgeted = BudgetedLLM(backend, 0.0000001, settings.prices)
    with pytest.raises(RequestBudgetExceeded):
        budgeted.complete(model="~deepseek/deepseek-flash-latest", system="", prompt="x" * 4000,
                          max_tokens=100)  # fmt: skip


def test_prices_are_cached_for_a_day(tmp_path):
    seen: list = []
    cache = tmp_path / "prices.json"
    first = openrouter.load_prices(httpx.Client(base_url=openrouter.BASE_URL,
                                                transport=fake_openrouter(seen)), cache)  # fmt: skip
    again = openrouter.load_prices(None, cache)  # no client needed: the cache is fresh
    assert first == again and len(seen) == 1
    old = time.time() - openrouter.PRICE_TTL - 10
    os.utime(cache, (old, old))
    openrouter.load_prices(httpx.Client(base_url=openrouter.BASE_URL,
                                        transport=fake_openrouter(seen)), cache)  # fmt: skip
    assert len(seen) == 2


def test_no_prices_when_openrouter_cannot_be_reached(tmp_path):
    down = httpx.MockTransport(lambda r: httpx.Response(503))
    settings = Settings(root=tmp_path, provider="openrouter")
    backend = openrouter_backend(settings, "k", tmp_path / "p.json", down)
    assert backend.priced is False and settings.prices == {}


def test_settings_and_init(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from tokenwise import cli

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        make_backend(Settings(root=tmp_path, provider="openrouter"))
    result = CliRunner().invoke(cli.app, ["init", "--root", str(tmp_path), "--openrouter"],
                                input="sk-or-test\n")  # fmt: skip
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".env").read_text() == "OPENROUTER_API_KEY=sk-or-test\n"
    settings = load_settings(tmp_path)
    assert settings.provider == "openrouter" and settings.models == openrouter.MODELS


def test_hosted_organizations_can_choose_openrouter(tmp_path, monkeypatch):
    from tokenwise.server import config as server_config

    server = server_config.ServerConfig(database_url="", secret_key="x" * 32)
    settings = server_config.repo_settings(tmp_path, server, {"model_provider": "openrouter"})
    assert settings.models == openrouter.MODELS  # not Claude's own names, which OpenRouter lacks
    monkeypatch.setattr(openrouter, "_memory", {})
    import tokenwise.llm as llm

    seen: list = []
    original = llm.openrouter_backend
    monkeypatch.setattr(llm, "openrouter_backend",
                        lambda s, key, cache: original(s, key, cache, fake_openrouter(seen)))  # fmt: skip
    backend = server_config.make_model(settings, "sk-or-org", server)
    assert backend.base_url == openrouter.BASE_URL  # fixed address: no allow-list needed
    assert settings.prices["anthropic/claude-sonnet-5.5"] == [2.0, 2.0, 0.2, 10.0]
    server_config.make_model(settings, "sk-or-org", server)
    assert len(seen) == 1  # the worker keeps prices in memory for a day
