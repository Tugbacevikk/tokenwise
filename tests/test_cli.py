from typer.testing import CliRunner

from tokenwise import cli

from .test_editor import LENGTH_CHECK

runner = CliRunner()


def test_map_command(project):
    result = runner.invoke(cli.app, ["map", "--root", str(project)])
    assert result.exit_code == 0
    assert "app/auth.py" in result.output
    assert "3 files" in result.output


def test_init_creates_config_env_and_gitignore(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(cli.app, ["init", "--root", str(tmp_path)], input="sk-test\n")
    assert result.exit_code == 0
    assert (tmp_path / "tokenwise.toml").exists()
    assert (tmp_path / ".env").read_text() == "ANTHROPIC_API_KEY=sk-test\n"
    assert (tmp_path / ".gitignore").read_text() == ".env\n.tokenwise/\n"
    runner.invoke(cli.app, ["init", "--root", str(tmp_path)])
    assert (tmp_path / ".gitignore").read_text() == ".env\n.tokenwise/\n"


def test_ask_end_to_end_then_undo(project, monkeypatch, fake):
    llm = fake('["app/auth.py"]', LENGTH_CHECK)
    monkeypatch.setattr(cli, "make_llm", lambda s: llm)
    original = (project / "app/auth.py").read_text()

    result = runner.invoke(cli.app, ["ask", "check password length", "-r", str(project), "-y"])
    assert result.exit_code == 0, result.output
    assert "Applied" in result.output
    assert "+2" in result.output
    assert "too short" in (project / "app/auth.py").read_text()
    # "check" is not an easy word and the file is small → medium → second rung
    assert [c["model"] for c in llm.calls] == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert "Difficulty: medium" in result.output

    result = runner.invoke(cli.app, ["undo", "-r", str(project)])
    assert "Restored" in result.output
    assert (project / "app/auth.py").read_text() == original


def test_ask_dry_run_changes_nothing(project, monkeypatch, fake):
    monkeypatch.setattr(cli, "make_llm", lambda s: fake('["app/auth.py"]', LENGTH_CHECK))
    original = (project / "app/auth.py").read_text()
    result = runner.invoke(cli.app, ["ask", "x", "-r", str(project), "--dry-run"])
    assert result.exit_code == 0
    assert (project / "app/auth.py").read_text() == original


def test_ask_without_files_fails(project, monkeypatch, fake):
    monkeypatch.setattr(cli, "make_llm", lambda s: fake("[]"))
    result = runner.invoke(cli.app, ["ask", "x", "-r", str(project)])
    assert result.exit_code == 1
    assert "no relevant files" in result.output


def test_missing_api_key(project, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(cli.app, ["ask", "x", "-r", str(project)])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


def test_ask_forced_model_and_no_cache(project, monkeypatch, fake):
    llm = fake('["app/auth.py"]', LENGTH_CHECK)
    seen = {}

    def make(settings):
        seen["cache"] = settings.cache
        return llm

    monkeypatch.setattr(cli, "make_llm", make)
    args = ["ask", "x", "-r", str(project), "--dry-run", "-m", "my-model", "--no-cache"]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert seen["cache"] is False
    assert llm.calls[1]["model"] == "my-model"
    assert "by my-model" in result.output


def test_init_local_writes_openai_config(tmp_path):
    tmp_path = tmp_path / "new-project"  # init creates the folder when it is missing
    result = runner.invoke(cli.app, ["init", "--root", str(tmp_path), "--local"])
    assert result.exit_code == 0, result.output
    text = (tmp_path / "tokenwise.toml").read_text()
    assert 'provider = "openai"' in text and "localhost:11434" in text
    assert "ollama pull" in result.output
    assert not (tmp_path / ".env").exists()


def test_cache_command(project, settings, fake):
    from tokenwise.llm import CachedLLM, cache_path

    result = runner.invoke(cli.app, ["cache", "-r", str(project)])
    assert "empty" in result.output
    CachedLLM(fake("hi"), cache_path(settings)).complete(
        model="m", system="s", prompt="p", max_tokens=1
    )
    result = runner.invoke(cli.app, ["cache", "-r", str(project)])
    assert "1 cached replies" in result.output
    result = runner.invoke(cli.app, ["cache", "-r", str(project), "--clear"])
    assert "Removed 1" in result.output
