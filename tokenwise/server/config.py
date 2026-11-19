"""Server configuration (environment variables) and the settings used for a customer repository.

A repository's own `tokenwise.toml` is untrusted input on the server: it may choose which files
count as source and what its check command is, nothing else. The model provider, its address,
the sandbox, its image and the network are decided here, and the check only ever runs in a
container."""

from __future__ import annotations

import base64
import hashlib
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..config import ANTHROPIC_MODELS, CONFIG_FILE, OPENROUTER_MODELS, Settings

# Keys a repository may set in its tokenwise.toml when the server runs it.
REPO_KEYS = ("check", "extensions", "ignore_dirs", "focus_lines", "max_files", "check_timeout")
MAX_CHECK_TIMEOUT = 900


@dataclass
class ServerConfig:
    database_url: str
    secret_key: str
    public_url: str = "http://localhost:8000"
    sandbox_image: str = "python:3.12-slim"
    sandbox_runtime: str = ""  # "runsc" to run checks under gVisor
    metrics_token: str = ""  # bearer token for /metrics; empty = no metrics endpoint
    scan_tools: tuple[str, ...] = ("ruff", "bandit")
    allowed_model_urls: tuple[str, ...] = ()  # OpenAI-compatible servers owners may choose
    work_dir: str = ""  # clones and check copies; must be the same path on the Docker host
    max_usd_per_fix: float = 0.5  # one AI fix's ceiling on the organization's key; 0 = none
    github: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> ServerConfig:
        key_path = os.getenv("GITHUB_PRIVATE_KEY_PATH", "")
        private_key = os.getenv("GITHUB_PRIVATE_KEY", "")
        if not private_key and key_path:
            private_key = Path(key_path).read_text("utf-8")
        secret = os.getenv("TOKENWISE_SECRET_KEY", "")
        if len(secret) < 32:
            raise ValueError("TOKENWISE_SECRET_KEY must be set (at least 32 characters).")
        return cls(
            database_url=os.environ["DATABASE_URL"],
            secret_key=secret,
            public_url=os.getenv("TOKENWISE_PUBLIC_URL", "http://localhost:8000").rstrip("/"),
            sandbox_image=os.getenv("TOKENWISE_SANDBOX_IMAGE", "python:3.12-slim"),
            sandbox_runtime=os.getenv("TOKENWISE_SANDBOX_RUNTIME", ""),
            metrics_token=os.getenv("TOKENWISE_METRICS_TOKEN", ""),
            scan_tools=_split(os.getenv("TOKENWISE_SCAN_TOOLS", "ruff,bandit")),
            max_usd_per_fix=float(os.getenv("TOKENWISE_MAX_USD_PER_FIX", "0.5")),
            allowed_model_urls=_split(os.getenv("TOKENWISE_ALLOWED_MODEL_URLS", "")),
            work_dir=os.getenv("TOKENWISE_WORK_DIR", ""),
            github={
                "app_id": os.getenv("GITHUB_APP_ID", ""),
                "private_key": private_key,
                "webhook_secret": os.getenv("GITHUB_WEBHOOK_SECRET", ""),
                "client_id": os.getenv("GITHUB_CLIENT_ID", ""),
                "client_secret": os.getenv("GITHUB_CLIENT_SECRET", ""),
            },
        )

    @property
    def fernet(self) -> Fernet:
        key = base64.urlsafe_b64encode(hashlib.sha256(self.secret_key.encode()).digest())
        return Fernet(key)

    def encrypt(self, value: str) -> bytes:
        return self.fernet.encrypt(value.encode())

    def decrypt(self, value: bytes | memoryview | None) -> str:
        if value is None:
            return ""
        try:
            return self.fernet.decrypt(bytes(value)).decode()
        except InvalidToken:
            return ""


def _split(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def repo_settings(root: Path, server: ServerConfig, org: dict | None = None) -> Settings:
    """Settings for a cloned customer repository: its allowed keys, the server's everything else."""
    settings = Settings(root=root)
    path = root / CONFIG_FILE
    if path.exists():
        try:
            data = tomllib.loads(path.read_text("utf-8")).get("tokenwise", {})
        except (OSError, ValueError):
            data = {}
        for key in REPO_KEYS:
            value = data.get(key)
            if value is None:
                continue
            default = getattr(settings, key)
            if isinstance(default, tuple) and isinstance(value, list):
                if all(isinstance(v, str) for v in value):
                    setattr(settings, key, tuple(value))
            elif isinstance(default, int) and isinstance(value, int):
                setattr(settings, key, value)
            elif isinstance(default, str) and isinstance(value, str):
                setattr(settings, key, value)
    settings.check_timeout = max(10, min(settings.check_timeout, MAX_CHECK_TIMEOUT))
    settings.sandbox = "docker"
    settings.sandbox_image = server.sandbox_image
    settings.sandbox_runtime = server.sandbox_runtime
    settings.scan_tools = server.scan_tools
    settings.max_usd_per_request = server.max_usd_per_fix  # a repository cannot raise it
    settings.cache = False
    org = org or {}
    settings.provider = org.get("model_provider") or "anthropic"
    settings.base_url = org.get("model_base_url") or ""
    default = OPENROUTER_MODELS if settings.provider == "openrouter" else ANTHROPIC_MODELS
    settings.models = tuple(org.get("models") or ()) or default
    return settings


def make_model(settings: Settings, api_key: str, server: ServerConfig):
    """The organization's own model, with its own key; never the server's environment."""
    from ..llm import AnthropicLLM, ConfigError, OpenAICompatibleLLM, openrouter_backend

    if not api_key:
        raise ConfigError("Bu organization için model anahtarı girilmemiş.")
    if settings.provider == "anthropic":
        return AnthropicLLM(api_key=api_key)
    if settings.provider == "openrouter":  # its fixed public address: no allow-list needed
        settings.base_url = ""
        return openrouter_backend(settings, api_key, None)
    if settings.provider == "openai" and settings.base_url in server.allowed_model_urls:
        return OpenAICompatibleLLM(settings.base_url, api_key)
    raise ConfigError("Bu model sağlayıcısına ya da adresine bu sunucuda izin verilmiyor.")
