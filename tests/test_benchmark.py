import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tokenwise import cli
from tokenwise.benchmark import (
    baseline,
    load_tasks,
    markdown,
    parse_files,
    run_task,
    savings,
    totals,
)
from tokenwise.config import Settings

BENCH = Path(__file__).resolve().parent.parent / "benchmark"
TYPO_EDIT = """cart.py
<<<<<<< SEARCH
            raise ValueError("Quantiy must be positive")
=======
            raise ValueError("Quantity must be positive")
>>>>>>> REPLACE
"""


def typo_fixed_cart() -> str:
    original = (BENCH / "projects/shop/cart.py").read_text()
    return original.replace("Quantiy", "Quantity")


def test_sample_tasks_load():
    tasks = load_tasks(BENCH / "tasks.toml")
    assert [t.name for t in tasks][:2] == ["typo", "discount"]
    assert all(t.project == (BENCH / "projects/shop").resolve() and t.check for t in tasks)


def test_load_tasks_errors(tmp_path):
    bad = tmp_path / "t.toml"
    bad.write_text('[[task]]\nname = "x"\n')
    with pytest.raises(ValueError, match=r"missing project \(or repo and ref\), request"):
        load_tasks(bad)
    bad.write_text('[[task]]\nname = "x"\nproject = "nope"\nrequest = "r"\n')
    with pytest.raises(ValueError, match="does not exist"):
        load_tasks(bad)
    bad.write_text("")
    with pytest.raises(ValueError, match="no \\[\\[task\\]\\]"):
        load_tasks(bad)


def test_parse_files():
    text = (
        "intro\n=== FILE: a.py ===\nx = 1\n=== END FILE ===\n=== FILE: b/c.py ===\n=== END FILE ==="
    )
    assert parse_files(text) == [("a.py", "x = 1\n"), ("b/c.py", "")]


def test_baseline_sends_everything_and_reads_whole_files(tmp_path, fake):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    llm = fake(
        "=== FILE: a.py ===\nx = 2\n=== END FILE ===\n=== FILE: b.py ===\ny = 2\n=== END FILE ==="
    )
    result = baseline("x", Settings(root=tmp_path), llm, "big")
    assert "y = 2" in llm.calls[0]["prompt"]  # unrelated file was sent too
    assert [c.path for c in result.changes] == ["a.py"]  # unchanged b.py is ignored
    assert result.ok and result.model == "big" and result.replies[0].role == "baseline"


def test_baseline_without_blocks_or_with_bad_path(tmp_path, fake):
    settings = Settings(root=tmp_path)
    assert baseline("x", settings, fake("sorry"), "m").error == "no whole-file blocks in the reply"
    escaped = baseline("x", settings, fake("=== FILE: ../e.py ===\n=== END FILE ==="), "m")
    assert "inside the project" in escaped.error


@pytest.mark.parametrize("method", ["baseline", "tokenwise"])
def test_run_task_applies_in_a_copy_and_runs_the_check(method, fake):
    task = load_tasks(BENCH / "tasks.toml")[0]
    replies = {
        "baseline": [f"=== FILE: cart.py ===\n{typo_fixed_cart()}=== END FILE ==="],
        "tokenwise": ['["cart.py"]', TYPO_EDIT],
    }[method]
    outcome = run_task(task, method, Settings(root=BENCH), fake(*replies), BENCH / "checks")
    assert outcome.result.ok and outcome.passed is True
    assert "Quantiy" in (BENCH / "projects/shop/cart.py").read_text()  # original untouched


def test_run_task_failed_edit_fails_the_check(fake):
    task = load_tasks(BENCH / "tasks.toml")[0]
    outcome = run_task(task, "tokenwise", Settings(root=BENCH), fake("[]"), BENCH / "checks")
    assert outcome.passed is False and not outcome.result.ok


def test_wrong_change_fails_the_check(fake):
    task = load_tasks(BENCH / "tasks.toml")[0]
    wrong = TYPO_EDIT.replace('"Quantity must be positive"', '"bad"')
    outcome = run_task(
        task, "tokenwise", Settings(root=BENCH), fake('["cart.py"]', wrong), BENCH / "checks"
    )
    assert outcome.result.ok and outcome.passed is False
    assert "Quantity must be positive" in outcome.check_output  # why it failed is kept
    md = markdown([outcome])
    assert (
        "## Failures" in md
        and "typo · tokenwise: check failed" in md
        and '+            raise ValueError("bad")' in md
    )


def test_savings_and_markdown(fake):
    assert savings(100, 40) == "-60%" and savings(0, 5) == "–" and savings(10, 15) == "+50%"
    task = load_tasks(BENCH / "tasks.toml")[0]
    settings = Settings(root=BENCH)
    outcomes = [
        run_task(
            task,
            "baseline",
            settings,
            fake(f"=== FILE: cart.py ===\n{typo_fixed_cart()}=== END FILE ==="),
            BENCH / "checks",
        ),
        run_task(task, "tokenwise", settings, fake('["cart.py"]', TYPO_EDIT), BENCH / "checks"),
    ]
    b, t = totals(outcomes, "baseline"), totals(outcomes, "tokenwise")
    assert b["passed"] == t["passed"] == 1
    assert t["output"] < b["output"]
    md = markdown(outcomes)
    assert "| typo | baseline |" in md and "| Checks passed | 1/1 | 1/1 | |" in md


def test_benchmark_command(tmp_path, monkeypatch, fake):
    replies = [
        f"=== FILE: cart.py ===\n{typo_fixed_cart()}=== END FILE ===",
        '["cart.py"]',
        TYPO_EDIT,
    ]
    llm = fake(*replies)
    seen = {}

    def make(settings):
        seen["cache"] = settings.cache
        return llm

    monkeypatch.setattr(cli, "make_llm", make)
    out = tmp_path / "results.md"
    args = ["benchmark", str(BENCH / "tasks.toml"), "--only", "typo", "-o", str(out)]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert seen["cache"] is False
    assert "checks passed" in result.output and "| Cost ($) |" in out.read_text()
    ledger = BENCH / ".tokenwise" / "ledger.db"
    assert ledger.exists()
    report = CliRunner().invoke(cli.app, ["report", "-r", str(BENCH), "--json"])
    assert json.loads(report.output)["all_time"]["runs"] == 1  # tokenwise runs only
    ledger.unlink()
    (BENCH / ".tokenwise").rmdir()


def test_benchmark_rejects_unknown_method():
    result = CliRunner().invoke(cli.app, ["benchmark", str(BENCH / "tasks.toml"), "--methods", "x"])
    assert result.exit_code == 1 and "--methods" in result.output


def test_repo_tasks_validate_before_fetching(tmp_path):
    bad = tmp_path / "t.toml"
    bad.write_text('[[task]]\nname = "x"\nrepo = "not a repo"\nref = "abc1234"\nrequest = "r"\n')
    with pytest.raises(ValueError, match="owner/name"):
        load_tasks(bad)
    bad.write_text('[[task]]\nname = "x"\nrepo = "a/b"\nref = "main"\nrequest = "r"\n')
    with pytest.raises(ValueError, match="commit hash"):
        load_tasks(bad)
    bad.write_text('[[task]]\nname = "x"\nrepo = "a/b"\nrequest = "r"\n')
    with pytest.raises(ValueError, match="repo and ref"):
        load_tasks(bad)


def test_repo_task_uses_an_existing_clone(tmp_path):
    from tokenwise.benchmark import REPOS_DIR

    clone = tmp_path / REPOS_DIR / "owner__name__0123456789ab"
    (clone / ".git").mkdir(parents=True)
    (clone / "pkg").mkdir()
    tasks_file = tmp_path / "t.toml"
    tasks_file.write_text(
        '[[task]]\nname = "x"\nrepo = "owner/name"\nref = "0123456789abcdef"\nsubdir = "pkg"\nrequest = "r"\n'
    )
    (task,) = load_tasks(tasks_file)
    assert task.project == (clone / "pkg").resolve()


def test_benchmark_continues_after_a_too_long_prompt(tmp_path, monkeypatch, fake):
    from tokenwise.llm import ContextTooLongError

    class SmallModel:
        def __init__(self):
            self.inner = fake('["cart.py"]', TYPO_EDIT)
            self.calls = 0

        def complete(self, **kw):
            self.calls += 1
            if self.calls == 1:
                raise ContextTooLongError("needs 40,000 tokens; max_context is 32,768")
            return self.inner.complete(**kw)

    monkeypatch.setattr(cli, "make_llm", lambda s: SmallModel())
    tasks = tmp_path / "tasks.toml"
    project = (BENCH / "projects/shop").as_posix()
    check = (
        "{python} -m pytest -q -p no:cacheprovider " + (BENCH / "checks/test_typo.py").as_posix()
    )
    tasks.write_text(
        f'[[task]]\nname = "big"\nproject = "{project}"\nrequest = "x"\ncheck = "{check}"\n'
        f'[[task]]\nname = "small"\nproject = "{project}"\nrequest = "fix the typo"\ncheck = "{check}"\n'
    )
    result = CliRunner().invoke(cli.app, ["benchmark", str(tasks), "--methods", "tokenwise"])
    assert result.exit_code == 0, result.output
    assert "big · tokenwise: context too long" in result.output.replace("\n", " ")
    assert "small · tokenwise: ok" in result.output
    md = (tmp_path / "results.md").read_text()  # default: next to the tasks file
    assert "| Checks passed |" not in md and "big · tokenwise: context too long" in md
    shutil_dir = BENCH / "projects" / ".tokenwise"
    assert not shutil_dir.exists()


def test_failure_details_show_the_last_reply(fake):
    task = load_tasks(BENCH / "tasks.toml")[0]
    bad = "cart.py\n<<<<<<< SEARCH\nnot in the file\n=======\nx\n>>>>>>> REPLACE"
    outcome = run_task(
        task,
        "tokenwise",
        Settings(root=BENCH),
        fake('["cart.py"]', bad, bad, bad, bad, bad, bad),
        BENCH / "checks",
    )
    assert (
        not outcome.result.ok
        and "last problem: cart.py: SEARCH text not found" in outcome.result.error
    )
    md = markdown([outcome])
    assert "Last reply (editor," in md and "not in the file" in md
