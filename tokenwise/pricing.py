"""Turns token usage into dollars.

Prices are USD per million tokens, from https://platform.claude.com/docs/en/about-claude/pricing
(checked 2026-11-12). Override or add models in tokenwise.toml:

    [tokenwise.prices]
    "my-model" = [input, cache_write, cache_read, output]

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


PRICES: dict[str, Price] = {
    # Haiku 5.5 doubles in price above 100K prompt tokens; tokenwise prompts stay far below.
    "claude-haiku-5-5": Price(0.10, 0.125, 0.01, 0.50),
    "claude-sonnet-5-5": Price(2.00, 2.50, 0.10, 10.00),
    "claude-opus-5-5": Price(4.00, 5.00, 0.20, 20.00),
    "claude-fable-5-2": Price(8.00, 10.00, 0.20, 40.00),
}


def price_for(model: str, overrides: dict | None = None) -> Price | None:
    if overrides and model in overrides:
        return Price(*(float(v) for v in overrides[model]))
    return PRICES.get(model)


def usage_cost(usage: Usage, price: Price | None) -> float:
    if price is None:
        return 0.0
    return (
        usage.input_tokens * price.input
        + usage.cache_write_tokens * price.cache_write
        + usage.cache_read_tokens * price.cache_read
        + usage.output_tokens * price.output
    ) / 1_000_000


def reply_cost(reply: Reply, overrides: dict | None = None) -> float:
    return 0.0 if reply.cached else usage_cost(reply.usage, price_for(reply.model, overrides))


def format_usd(value: float) -> str:
    return f"${value:.4f}" if value < 1 else f"${value:,.2f}"
