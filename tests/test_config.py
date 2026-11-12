from tokenwise.config import load_settings


def test_toml_and_env_override_defaults(tmp_path, monkeypatch):
    (tmp_path / "tokenwise.toml").write_text(
        '[tokenwise]\nmax_files = 3\nextensions = [".py"]\nunknown = 1\n', "utf-8"
    )
    monkeypatch.setenv("TOKENWISE_EDITOR_MODEL", "claude-opus-5-5")
    settings = load_settings(tmp_path)
    assert settings.max_files == 3
    assert settings.extensions == (".py",)
    assert settings.editor_model == "claude-opus-5-5"
    assert settings.selector_model == "claude-haiku-5-5"
