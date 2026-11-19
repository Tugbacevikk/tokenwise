"""max_usd_per_request: one request, escalation included, stays under its ceiling."""

from dataclasses import replace

import pytest

from tokenwise.budget import BudgetedLLM, RequestBudgetExceeded
from tokenwise.editor import propose
from tokenwise.llm import Reply, Usage
from tokenwise.pipeline import run
from tokenwise.services.messages import turkish_error

from .test_editor import LENGTH_CHECK

# $ per million tokens: input, cache write, cache read, output
PRICES = {"cheap": [1, 1, 1, 10], "big": [100, 100, 100, 1000]}
BAD = "app/auth.py\n<<<<<<< SEARCH\nnope\n=======\nx\n>>>>>>> REPLACE\n"


class Recording:
    """Answers in order and remembers the max_tokens each call was allowed."""

    def __init__(self, *texts: str, output_tokens: int = 100) -> None:
        self.texts, self.output_tokens = list(texts), output_tokens
        self.calls: list[dict] = []

    def complete(self, *, model, system, prompt, max_tokens) -> Reply:
        self.calls.append({"model": model, "max_tokens": max_tokens})
        return Reply(self.texts.pop(0), model, Usage(len(system + prompt) // 4, self.output_tokens))


def test_output_is_capped_to_what_the_rest_pays_for():
    llm = BudgetedLLM(Recording("ok", "ok"), 0.01, PRICES)
    # 4000 characters ~ 1000 tokens, +20% margin = 1200 input tokens at $1/M = $0.0012;
    # the remaining $0.0088 buys 880 output tokens at $10/M
    assert llm.output_cap("cheap", "", "x" * 4000) == 880
    llm.complete(model="cheap", system="", prompt="x" * 4000, max_tokens=4096)
    assert llm.inner.calls[0]["max_tokens"] == 880
    assert llm.spent == pytest.approx((1000 * 1 + 100 * 10) / 1_000_000)
    # a smaller request keeps its own limit
    llm.complete(model="cheap", system="", prompt="hi", max_tokens=50)
    assert llm.inner.calls[1]["max_tokens"] == 50


def test_a_call_that_does_not_fit_is_not_made():
    inner = Recording("never")
    llm = BudgetedLLM(inner, 0.001, PRICES)
    with pytest.raises(RequestBudgetExceeded) as exc:
        llm.complete(model="big", system="", prompt="x" * 4000, max_tokens=4096)
    assert inner.calls == [] and exc.value.model == "big"
    assert "used up" in str(exc.value)


def test_models_without_a_price_are_not_limited():
    llm = BudgetedLLM(Recording("ok"), 0.000001, PRICES)
    llm.complete(model="local-qwen", system="", prompt="x" * 40_000, max_tokens=4096)
    assert llm.inner.calls[0]["max_tokens"] == 4096 and llm.spent == 0


def test_escalation_stops_at_the_ceiling(settings):
    """The cheap model fails twice; the expensive one would not fit, so it is never asked."""
    settings = replace(settings, prices=PRICES)
    inner = Recording(BAD, BAD, LENGTH_CHECK)
    llm = BudgetedLLM(inner, 0.05, PRICES)
    proposal = propose("check length", ["app/auth.py"], settings, llm, ["app/auth.py"],
                       models=["cheap", "big"])  # fmt: skip
    assert [c["model"] for c in inner.calls] == ["cheap", "cheap"]
    assert not proposal.changes and "request budget is used up" in proposal.error
    assert len(proposal.replies) == 2


def test_run_records_what_was_spent_before_the_ceiling(settings):
    settings = replace(settings, prices=PRICES, models=("cheap", "big"), max_usd_per_request=0.01)
    inner = Recording('["app/auth.py"]', LENGTH_CHECK)
    result = run("check password length", settings, inner, model="big")
    assert not result.ok and "request budget is used up" in result.error
    assert len(result.replies) == 1 and result.cost > 0  # the selector call is still counted
    assert turkish_error(result.error).startswith("İstek başına bütçe doldu")


def test_no_ceiling_by_default(settings):
    inner = Recording('["app/auth.py"]', LENGTH_CHECK)
    result = run("check password length", settings, inner)
    assert result.ok and inner.calls[1]["max_tokens"] == settings.max_output_tokens


def test_hosted_fixes_have_a_ceiling_a_repository_cannot_raise(tmp_path):
    from tokenwise.server.config import ServerConfig, repo_settings

    (tmp_path / "tokenwise.toml").write_text("[tokenwise]\nmax_usd_per_request = 100\n", "utf-8")
    server = ServerConfig(database_url="", secret_key="x" * 32)
    assert repo_settings(tmp_path, server).max_usd_per_request == 0.5
