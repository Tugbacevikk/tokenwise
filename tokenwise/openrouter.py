"""OpenRouter: one API key for models from many companies (DeepSeek, Qwen, Mistral, Llama,
Claude, ...) behind the OpenAI chat API.

Model names change there every few weeks, and so do prices, so neither is hard-coded beyond
a starting ladder: prices are read from OpenRouter's public model list and kept for a day, so
the cost ledger, the weekly budget and `max_usd_per_request` work for every model."""

from __future__ import annotations

import json
import time
from pathlib import Path

BASE_URL = "https://openrouter.ai/api/v1"
# Starting ladder, cheapest first. "~...-latest" names follow DeepSeek's newest release.
MODELS = (
    "~deepseek/deepseek-flash-latest",
    "~deepseek/deepseek-pro-latest",
    "anthropic/claude-sonnet-5.5",
)
PRICE_TTL = 24 * 3600
HEADERS = {  # how OpenRouter shows the app on its dashboards; optional
    "HTTP-Referer": "https://github.com/Tugbacevikk/tokenwise",
    "X-Title": "tokenwise",
}


def parse_prices(models: list[dict]) -> dict[str, list[float]]:
    """OpenRouter's per-token USD strings → tokenwise's price rows (USD per million):
    input, cache write, cache read, output."""
    prices = {}
    for m in models:
        p = m.get("pricing") or {}
        try:
            prompt, completion = float(p["prompt"]), float(p["completion"])
        except (KeyError, TypeError, ValueError):
            continue
        if prompt < 0 or completion < 0:  # "-1" marks a router whose price is not fixed
            continue
        read = float(p.get("input_cache_read") or prompt)
        write = float(p.get("input_cache_write") or prompt)
        prices[m["id"]] = [round(v * 1_000_000, 6) for v in (prompt, write, read, completion)]
    return prices


_memory: dict[str, tuple[float, dict]] = {}  # the server's workers keep prices in memory


def load_prices(client, cache: Path | None) -> dict[str, list[float]]:
    """Prices from the cache when fresh, else from OpenRouter (then cached). `cache` None:
    kept in this process. {} on failure: costs then show as 0 and the per-request ceiling
    cannot hold, which `doctor` reports."""
    if cache is None:
        at, prices = _memory.get("prices", (0.0, {}))
        if prices and time.time() - at < PRICE_TTL:
            return prices
    elif cache.exists() and time.time() - cache.stat().st_mtime < PRICE_TTL:
        try:
            return json.loads(cache.read_text("utf-8"))
        except ValueError:
            pass
    try:
        response = client.get("/models")
        response.raise_for_status()
        prices = parse_prices(response.json().get("data", []))
    except Exception:  # noqa: BLE001 - no prices is better than no answer
        return {}
    if prices and cache is None:
        _memory["prices"] = (time.time(), prices)
    elif prices and cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(prices), "utf-8")
    return prices
