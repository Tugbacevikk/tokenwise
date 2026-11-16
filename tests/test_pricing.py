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


def test_haiku_long_prompt_costs_five_times():
    price = price_for("claude-haiku-5-5")
    short = Usage(input_tokens=100_000, output_tokens=1_000_000)  # exactly at the threshold
    long = Usage(input_tokens=100_001, output_tokens=1_000_000)
    assert usage_cost(short, price) == pytest.approx(0.1 * 100_000 / 1e6 + 0.5)
    assert usage_cost(long, price) == pytest.approx((0.1 * 100_001 / 1e6 + 0.5) * 5)


def test_long_prompt_counts_cache_tokens_and_other_models_have_no_tier():
    haiku = price_for("claude-haiku-5-5")
    assert usage_cost(Usage(10, 0, cache_read_tokens=150_000), haiku) == pytest.approx(
        (10 * 0.10 + 150_000 * 0.01) * 5 / 1e6
    )
    opus = price_for("claude-opus-5-5")
    assert usage_cost(Usage(500_000, 0), opus) == pytest.approx(2.0)


def test_override_can_set_a_long_prompt_tier():
    price = price_for("m", {"m": [1, 1, 1, 1, 1000, 2]})
    assert usage_cost(Usage(2000, 0), price) == pytest.approx(2000 * 1 * 2 / 1e6)


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
