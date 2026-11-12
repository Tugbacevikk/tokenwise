import pytest

from tokenwise.llm import Reply, Usage
from tokenwise.pricing import format_usd, price_for, reply_cost, usage_cost


def test_known_model_cost_counts_every_token_kind():
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert usage_cost(usage, price_for("claude-sonnet-5-5")) == pytest.approx(2 + 10 + 0.1 + 2.5)


def test_unknown_model_is_free_and_overrides_win():
    assert price_for("qwen2.5-coder:7b") is None
    assert usage_cost(Usage(1000, 1000), None) == 0
    price = price_for("qwen2.5-coder:7b", {"qwen2.5-coder:7b": [1, 1, 1, 1]})
    assert usage_cost(Usage(1_000_000, 0), price) == 1


def test_cached_reply_costs_nothing():
    usage = Usage(1_000_000, 0)
    assert reply_cost(Reply("x", "claude-opus-5-5", usage)) == 4
    assert reply_cost(Reply("x", "claude-opus-5-5", usage, cached=True)) == 0


def test_format_usd():
    assert format_usd(0.00123) == "$0.0012"
    assert format_usd(12.5) == "$12.50"
