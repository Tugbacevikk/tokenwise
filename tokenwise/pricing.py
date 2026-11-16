"""Turns token usage into dollars.

Prices are USD per million tokens, from https://platform.claude.com/docs/en/about-claude/pricing
(checked 2026-11-12). Override or add models in tokenwise.toml:

    [tokenwise.prices]
    "my-model" = [input, cache_write, cache_read, output]
    "my-model" = [input, cache_write, cache_read, output, long_threshold, long_multiplier]

A model with no known price (for example a local Ollama model) costs 0.
"""

from __future__ import annotations

from dataclasses import dataclass

from .llm import Reply, Usage


@dataclass(frozen=True)
class Price:
    input: float
    cache_write: float  # 5-minute cache write
    cache_read: float
    output: float
    # Some models cost more once the prompt passes a size. When the prompt (input + cache read +
    # cache write tokens) is above `long_threshold`, the whole call is billed at `long_multiplier`
    # times the base price. 0 = no such tier.
    long_threshold: int = 0
    long_multiplier: float = 1.0


PRICES: dict[str, Price] = {
    # Haiku 5.5: prompts above 100K tokens cost 5x (input $0.10 -> $0.50, output $0.50 -> $2.50).
    "claude-haiku-5-5": Price(0.10, 0.125, 0.01, 0.50, long_threshold=100_000, long_multiplier=5.0),
    "claude-sonnet-5-5": Price(2.00, 2.50, 0.10, 10.00),
    "claude-opus-5-5": Price(4.00, 5.00, 0.20, 20.00),
    "claude-fable-5-2": Price(8.00, 10.00, 0.20, 40.00),
}


def price_for(model: str, overrides: dict | None = None) -> Price | None:
    if overrides and model in overrides:
        values = [float(v) for v in overrides[model]]
        if len(values) > 4:  # optional: long_threshold, long_multiplier
            values[4] = int(values[4])
        return Price(*values)
    return PRICES.get(model)


def usage_cost(usage: Usage, price: Price | None) -> float:
    if price is None:
        return 0.0
    cost = (
        usage.input_tokens * price.input
        + usage.cache_write_tokens * price.cache_write
        + usage.cache_read_tokens * price.cache_read
        + usage.output_tokens * price.output
    ) / 1_000_000
    prompt = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    if price.long_threshold and prompt > price.long_threshold:
        cost *= price.long_multiplier
    return cost


def reply_cost(reply: Reply, overrides: dict | None = None) -> float:
    return 0.0 if reply.cached else usage_cost(reply.usage, price_for(reply.model, overrides))


def format_usd(value: float) -> str:
    return f"${value:.4f}" if value < 1 else f"${value:,.2f}"
