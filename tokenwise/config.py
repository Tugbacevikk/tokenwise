"""Settings: defaults, overridden by tokenwise.toml and then by environment variables."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from dotenv import load_dotenv

CONFIG_FILE = "tokenwise.toml"
STATE_DIR = ".tokenwise"

DEFAULT_EXTENSIONS = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".kt",
    ".cs",
    ".go",
    ".rs",
    ".php",
)
DEFAULT_IGNORE_DIRS = (
    ".git",
    ".tokenwise",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    "dist",
    "build",
)
ANTHROPIC_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
LOCAL_MODELS = ("qwen2.5-coder:7b",)
OLLAMA_URL = "http://localhost:11434"

CONFIG_TEMPLATE = """[tokenwise]
# "anthropic" (Claude API), "ollama" (local models) or "openai" (any OpenAI-compatible
# server such as LM Studio or vLLM; set base_url, e.g. "http://localhost:1234/v1")
provider = "{provider}"
{base_url_line}
# Model ladder, cheapest first. The selector always uses the first one; the editor starts
# at the rung the router picks and climbs on failure. One entry = no climbing.
models = {models}
# File types included in the project map
extensions = [".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".cs", ".go", ".rs", ".php"]
# Most files sent to the editor in one request
max_files = 8
# Files longer than this may be sent focused: only the functions the request needs
focus_lines = 300
# Largest project map (tokens). A bigger project gets a shorter map: tests first, then details.
map_max_tokens = {map_max_tokens}
# Stop asking once the last 7 days cost this much (USD). 0 = no limit.
budget_usd_week = 0
# Ollama only: the largest context window tokenwise may ask for. Bigger needs more GPU memory.
max_context = 32768
# Command that must pass before a change is offered, run in a throwaway copy of the project.
# A failing check is sent back to the model to fix. {{python}} = the Python running tokenwise.
# check = "{{python}} -m pytest -q -x"

# Prices in USD per million tokens: [input, cache_write, cache_read, output].
# Claude models are built in; add any other model here (local models cost 0).
# [tokenwise.prices]
# "my-model" = [1.0, 1.25, 0.1, 5.0]
"""


def config_template(provider: str) -> str:
    local = provider == "ollama"
    return CONFIG_TEMPLATE.format(
        provider=provider,
        base_url_line=f'base_url = "{OLLAMA_URL}"' if local else f'# base_url = "{OLLAMA_URL}"',
        models=_toml_list(LOCAL_MODELS if local else ANTHROPIC_MODELS),
        map_max_tokens=8000 if local else 16000,
    )


def _toml_list(items: tuple[str, ...]) -> str:
    return "[" + ", ".join(f'"{i}"' for i in items) + "]"


@dataclass
class Settings:
    root: Path
    provider: str = "anthropic"
    base_url: str = ""
    models: tuple[str, ...] = ANTHROPIC_MODELS
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    ignore_dirs: tuple[str, ...] = DEFAULT_IGNORE_DIRS
    max_file_bytes: int = 200_000
    max_files: int = 8
    focus_lines: int = 300
    budget_usd_week: float = 0.0
    max_context: int = 32768
    map_max_tokens: int = 16000
    max_output_tokens: int = 4096
    cache: bool = True
    check: str = ""
    check_timeout: int = 300
    prices: dict = field(default_factory=dict)

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR

    @property
    def selector_model(self) -> str:
        return self.models[0]


def load_settings(root: Path | str = ".") -> Settings:
    root = Path(root).resolve()
    load_dotenv(root / ".env")
    settings = Settings(root=root)

    config_path = root / CONFIG_FILE
    if config_path.exists():
        data = tomllib.loads(config_path.read_text("utf-8")).get("tokenwise", {})
        allowed = {f.name for f in fields(Settings)} - {"root"}
        for key, value in data.items():
            if key in allowed:
                setattr(settings, key, tuple(value) if isinstance(value, list) else value)

    settings.provider = os.getenv("TOKENWISE_PROVIDER", settings.provider)
    settings.base_url = os.getenv("TOKENWISE_BASE_URL", settings.base_url)
    if models := os.getenv("TOKENWISE_MODELS"):
        settings.models = tuple(m.strip() for m in models.split(",") if m.strip())
    if not settings.models:
        raise ValueError("tokenwise: `models` must list at least one model")
    return settings
