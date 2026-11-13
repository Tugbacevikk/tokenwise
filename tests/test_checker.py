import sys

from typer.testing import CliRunner

from tokenwise import cli
from tokenwise.checker import check_in_copy, expand, run_command
from tokenwise.editor import Edit, plan_changes, propose

from .test_editor import ALL_FILES, LENGTH_CHECK

# passes only when login rejects short passwords
CHECK = (
    '{python} -c "import app.auth as a, sys; '
    "sys.exit(0 if 'too short' in open(a.__file__).read() else 1)\""
)
WRONG = LENGTH_CHECK.replace("password too short", "nope")


def test_expand_and_run_command(tmp_path):
    assert expand("{python} x", None).startswith(f'"{sys.executable}"')
    assert expand("{checks}/t.py", tmp_path) == f'"{tmp_path}"/t.py'
    ok = run_command("{python} -c \"print('hi')\"", tmp_path, 30)
    assert ok.passed and ok.output == "hi"
    bad = run_command("{python} -c \"import sys; print('boom'); sys.exit(3)\"", tmp_path, 30)
    assert not bad.passed and "boom" in bad.output
    slow = run_command('{python} -c "import time; time.sleep(5)"', tmp_path, 1)
    assert not slow.passed and "timed out" in slow.output


def test_check_runs_in_a_copy(settings):
    original = (settings.root / "app/auth.py").read_text()
    changes = plan_changes(
        settings.root, [Edit("app/auth.py", "    return True\n", "    return 'too short'\n")]
    )
    assert check_in_copy(settings, changes, CHECK).passed
    assert (settings.root / "app/auth.py").read_text() == original
    assert not check_in_copy(settings, [], CHECK).passed


def test_failed_check_is_fed_back_then_fixed(settings, fake):
    from tokenwise.checker import make_verifier

    llm = fake(WRONG, LENGTH_CHECK)
    proposal = propose(
        "x", ["app/auth.py"], settings, llm, ALL_FILES, ["m"], make_verifier(settings, CHECK)
    )
    assert proposal.error is None and proposal.check_passed is True
    assert "check failed" in llm.calls[1]["prompt"]
    assert "ORIGINAL files" in llm.calls[1]["prompt"]


def test_check_failing_everywhere_keeps_last_change(settings, fake):
    from tokenwise.checker import make_verifier

    llm = fake(WRONG, WRONG, WRONG, WRONG)
    proposal = propose(
        "x", ["app/auth.py"], settings, llm, ALL_FILES, ["a", "b"], make_verifier(settings, CHECK)
    )
    assert proposal.check_passed is False and proposal.changes
    assert proposal.error == "the check still fails after trying a → b"
    assert [c["model"] for c in llm.calls] == ["a", "a", "b", "b"]


def test_cli_check_option(project, monkeypatch, fake):
    runner = CliRunner()
    monkeypatch.setattr(cli, "make_llm", lambda s: fake('["app/auth.py"]', LENGTH_CHECK))
    result = runner.invoke(cli.app, ["ask", "x", "-r", str(project), "-y", "--check", CHECK])
    assert result.exit_code == 0, result.output
    assert "Check passed" in result.output

    runner.invoke(cli.app, ["undo", "-r", str(project)])
    monkeypatch.setattr(
        cli, "make_llm", lambda s: fake('["app/auth.py"]', WRONG, WRONG, WRONG, WRONG)
    )
    result = runner.invoke(cli.app, ["ask", "x", "-r", str(project), "--check", CHECK])
    assert result.exit_code == 1 and "Check failed" in result.output
    assert "too short" not in (project / "app/auth.py").read_text()
    applied = runner.invoke(cli.app, ["apply", "-r", str(project)])  # still possible on purpose
    assert applied.exit_code == 0 and "nope" in (project / "app/auth.py").read_text()


def test_config_check_and_no_check(project, monkeypatch, fake):
    (project / "tokenwise.toml").write_text(f"[tokenwise]\ncheck = '''{CHECK}'''\n")
    seen = {}

    def make(settings):
        seen["check"] = settings.check
        return fake('["app/auth.py"]', LENGTH_CHECK)

    monkeypatch.setattr(cli, "make_llm", make)
    CliRunner().invoke(cli.app, ["ask", "x", "-r", str(project), "--dry-run"])
    assert seen["check"] == CHECK
    CliRunner().invoke(cli.app, ["ask", "x", "-r", str(project), "--dry-run", "--no-check"])
    assert seen["check"] == ""


def test_paths_with_spaces_are_quoted(tmp_path):
    checks = tmp_path / "ADIL CEVIK" / "checks"
    checks.mkdir(parents=True)
    (checks / "t.py").write_text("print('ran')\n")
    result = run_command("{python} {checks}/t.py", tmp_path, 30, checks)
    assert result.passed and result.output == "ran", result.output
