"""A spending ceiling for one request (`max_usd_per_request`).

The weekly budget stops new requests once a week's spending is reached; it cannot stop one
request that escalates through stronger models from costing more than expected. This wrapper
does: before each model call it estimates the input's cost, caps the answer's length
(`max_tokens`) to what the rest of the ceiling can pay for, and refuses the call when not even
a short answer fits.

The input estimate is deliberately pessimistic (about 4 characters per token, plus 20%, all
at the uncached price), so the ceiling holds even though no tokenizer is used. Models without a
known price (local models, unlisted ones) cost nothing here and are not limited."""

from __future__ import annotations

from .llm import LLM, Reply
from .pricing import price_for, reply_cost
from .repo_map import estimate_tokens

MIN_OUTPUT_TOKENS = 256  # below this an edit cannot be written, so the call is not made
INPUT_MARGIN = 1.2


class RequestBudgetExceeded(Exception):
    def __init__(self, spent: float, limit: float, model: str) -> None:
        self.spent, self.limit, self.model = spent, limit, model
        super().__init__(
            f"the request budget is used up: ${spent:.4f} of ${limit:.4f} spent, "
            f"not enough left for {model}"
        )


class BudgetedLLM:
    """Wraps a model for one request. `replies` keeps every answer, also when the ceiling
    stops the request half-way, so the ledger still records what was spent."""

    def __init__(self, inner: LLM, limit_usd: float, prices: dict | None = None) -> None:
        self.inner, self.limit, self.prices = inner, limit_usd, prices or {}
        self.spent = 0.0
        self.replies: list[Reply] = []

    def output_cap(self, model: str, system: str, prompt: str) -> int | None:
        """The most output tokens the rest of the ceiling pays for; None: no known price."""
        price = price_for(model, self.prices)
        if price is None or price.output <= 0:
            return None
        tokens_in = int(estimate_tokens(system + prompt) * INPUT_MARGIN)
        multiplier = price.long_multiplier if price.long_threshold and tokens_in > \
            price.long_threshold else 1.0  # fmt: skip
        left = self.limit - self.spent - tokens_in * price.input * multiplier / 1_000_000
        return max(0, int(left / (price.output * multiplier / 1_000_000)))

    def complete(self, *, model: str, system: str, prompt: str, max_tokens: int) -> Reply:
        cap = self.output_cap(model, system, prompt)
        if cap is not None:
            if cap < min(MIN_OUTPUT_TOKENS, max_tokens):
                raise RequestBudgetExceeded(self.spent, self.limit, model)
            max_tokens = min(max_tokens, cap)
        reply = self.inner.complete(model=model, system=system, prompt=prompt,
                                    max_tokens=max_tokens)  # fmt: skip
        self.spent += reply_cost(reply, self.prices)
        self.replies.append(reply)
        return reply

    def __getattr__(self, name: str):  # anything else (e.g. a cache's helpers) goes through
        return getattr(self.inner, name)
