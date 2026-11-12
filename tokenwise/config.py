"""Settings: defaults, overridden by tokenwise.toml and then by environment variables."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from dotenv import load_dotenv

CONFIG_FILE = "tokenwise.toml"
STATE_DIR = ".tokenwise"

DEFAULT_EXTENSIONS = (".py", ".js", ".jsx", ".ts", ".tsx")
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
OLLAMA_URL = "http://localhost:11434/v1"

CONFIG_TEMPLATE = """[tokenwise]
# "anthropic" (Claude API) or "openai" (any OpenAI-compatible server: Ollama, LM Studio, vLLM)
provider = "{provider}"
{base_url_line}
# Model ladder, cheapest first. The selector always uses the first one; the editor starts
# at the rung the router picks and climbs on failure. One entry = no climbing.
models = {models}
# File types included in the project map
extensions = [".py", ".js", ".jsx", ".ts", ".tsx"]
# Most files sent to the editor in one request
max_files = 8
"""


def config_template(provider: str) -> str:
    local = provider == "openai"
    return CONFIG_TEMPLATE.format(
        provider=provider,
        base_url_line=f'base_url = "{OLLAMA_URL}"' if local else f'# base_url = "{OLLAMA_URL}"',
        models=_toml_list(LOCAL_MODELS if local else ANTHROPIC_MODELS),
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
    max_output_tokens: int = 4096
    cache: bool = True

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
