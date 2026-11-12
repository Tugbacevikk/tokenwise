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

CONFIG_TEMPLATE = """[tokenwise]
# Cheap model that picks which files are needed
selector_model = "claude-haiku-5-5"
# Model that writes the change
editor_model = "claude-sonnet-5-5"
# File types included in the project map
extensions = [".py", ".js", ".jsx", ".ts", ".tsx"]
# Most files sent to the editor in one request
max_files = 8
"""


@dataclass
class Settings:
    root: Path
    selector_model: str = "claude-haiku-5-5"
    editor_model: str = "claude-sonnet-5-5"
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    ignore_dirs: tuple[str, ...] = DEFAULT_IGNORE_DIRS
    max_file_bytes: int = 200_000
    max_files: int = 8
    max_output_tokens: int = 4096

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR


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

    settings.selector_model = os.getenv("TOKENWISE_SELECTOR_MODEL", settings.selector_model)
    settings.editor_model = os.getenv("TOKENWISE_EDITOR_MODEL", settings.editor_model)
    return settings
