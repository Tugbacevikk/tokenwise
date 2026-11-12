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
    monkeypatch.setattr(cli, "make_llm", lambda: llm)
    original = (project / "app/auth.py").read_text()

    result = runner.invoke(cli.app, ["ask", "check password length", "-r", str(project), "-y"])
    assert result.exit_code == 0, result.output
    assert "Applied" in result.output
    assert "+2" in result.output
    assert "too short" in (project / "app/auth.py").read_text()
    assert [c["model"] for c in llm.calls] == ["claude-haiku-5-5", "claude-sonnet-5-5"]

    result = runner.invoke(cli.app, ["undo", "-r", str(project)])
    assert "Restored" in result.output
    assert (project / "app/auth.py").read_text() == original


def test_ask_dry_run_changes_nothing(project, monkeypatch, fake):
    monkeypatch.setattr(cli, "make_llm", lambda: fake('["app/auth.py"]', LENGTH_CHECK))
    original = (project / "app/auth.py").read_text()
    result = runner.invoke(cli.app, ["ask", "x", "-r", str(project), "--dry-run"])
    assert result.exit_code == 0
    assert (project / "app/auth.py").read_text() == original


def test_ask_without_files_fails(project, monkeypatch, fake):
    monkeypatch.setattr(cli, "make_llm", lambda: fake("[]"))
    result = runner.invoke(cli.app, ["ask", "x", "-r", str(project)])
    assert result.exit_code == 1
    assert "no relevant files" in result.output


def test_missing_api_key(project, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(cli.app, ["ask", "x", "-r", str(project)])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output
