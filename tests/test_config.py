import pytest

from tokenwise.config import config_template, load_settings


def test_toml_and_env_override_defaults(tmp_path, monkeypatch):
    (tmp_path / "tokenwise.toml").write_text(
        '[tokenwise]\nmax_files = 3\nextensions = [".py"]\nunknown = 1\n', "utf-8"
    )
    monkeypatch.setenv("TOKENWISE_MODELS", "a, b")
    settings = load_settings(tmp_path)
    assert settings.max_files == 3
    assert settings.extensions == (".py",)
    assert settings.models == ("a", "b")
    assert settings.selector_model == "a"
    assert settings.provider == "anthropic" and settings.cache is True


def test_local_template_parses(tmp_path):
    (tmp_path / "tokenwise.toml").write_text(config_template("openai"), "utf-8")
    settings = load_settings(tmp_path)
    assert settings.provider == "openai"
    assert settings.base_url == "http://localhost:11434/v1"
    assert settings.models == ("qwen2.5-coder:7b",)


def test_empty_models_rejected(tmp_path):
    (tmp_path / "tokenwise.toml").write_text("[tokenwise]\nmodels = []\n", "utf-8")
    with pytest.raises(ValueError):
        load_settings(tmp_path)
