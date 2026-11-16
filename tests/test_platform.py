"""The shared core (graph, history), RepoDoctor (findings, health) and ReleaseGuard (risk),
end to end on small real git repositories. No test calls a real model."""

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tokenwise import cli
from tokenwise.analyzers import Finding, complexity, python_findings, text_findings
from tokenwise.config import Settings
from tokenwise.graph import build_graph
from tokenwise.health import (
    HealthStore,
    health_path,
    scan,
    scores_for,
    task_for,
    weekly_report,
)
from tokenwise.history import load_history, parse_log
from tokenwise.release import GuardError, assess, to_markdown

runner = CliRunner()

COMPLEX = (
    "def decide(a, b, c, d):\n"
    + "".join(f"    if a == {i} and b:\n        return {i}\n" for i in range(12))
    + "    return -1\n"
)


def sh(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", *args],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip


def commit(root: Path, message: str, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
    sh(root, "add", "-A")
    sh(root, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """core.py is used by api.py and jobs.py; fixes keep landing on core.py."""
    sh(tmp_path, "init", "-q", "-b", "main")
    commit(
        tmp_path,
        "start",
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "def total(xs):\n    return sum(xs)\n",
            "pkg/api.py": "from pkg.core import total\n\n\ndef handler(xs):\n    return total(xs)\n",
            "pkg/jobs.py": "from .core import total\n\n\ndef nightly():\n    return total([1])\n",
            "pkg/store.py": "from pkg import core\n",
            "tests/test_api.py": "from pkg.api import handler\n\n\ndef test_h():\n"
            "    assert handler([1]) == 1\n",
        },
    )
    for i in range(3):
        commit(tmp_path, f"fix: total edge case {i}", {"pkg/core.py": f"def total(xs):\n"
               f"    return sum(xs) + {i} * 0\n", "pkg/jobs.py": f"from .core import total\n\n\n"
               f"def nightly():\n    return total([{i}])\n"})  # fmt: skip
    return tmp_path


# --- shared core ------------------------------------------------------------------------


def test_graph_reads_python_and_js_imports(repo):
    (repo / "web").mkdir()
    (repo / "web" / "a.ts").write_text("import { b } from './b';\nexport const a = b;\n")
    (repo / "web" / "b.ts").write_text("export const b = require('../web/c.js');\n")
    (repo / "web" / "c.ts").write_text("import x from 'react';\n")
    graph = build_graph(Settings(root=repo))
    assert graph.deps["pkg/api.py"] == {"pkg/core.py"}
    assert graph.deps["pkg/jobs.py"] == {"pkg/core.py"}  # relative import
    assert "pkg/core.py" in graph.deps["pkg/store.py"]  # from pkg import core
    assert graph.deps["web/a.ts"] == {"web/b.ts"}
    assert graph.deps["web/b.ts"] == {"web/c.ts"}  # "./c.js" written for c.ts
    assert graph.fan_in("pkg/core.py") == 3
    assert graph.fan_in("pkg/api.py") == 0 and graph.fan_in("pkg/api.py", tests=True) == 1
    assert graph.affected(["pkg/core.py"]) == {
        "pkg/api.py": 1,
        "pkg/jobs.py": 1,
        "pkg/store.py": 1,
        "tests/test_api.py": 2,
    }
    assert graph.tested("pkg/api.py") and not graph.tested("pkg/jobs.py")


def test_graph_finds_import_cycles(tmp_path):
    (tmp_path / "a.py").write_text("import b\n")
    (tmp_path / "b.py").write_text("import c\n")
    (tmp_path / "c.py").write_text("import a\n")
    (tmp_path / "d.py").write_text("import a\n")
    assert build_graph(Settings(root=tmp_path)).cycles() == [["a.py", "b.py", "c.py"]]


def test_history_counts_churn_fixes_and_co_changes(repo):
    history = load_history(repo)
    assert len(history.commits) == 4
    assert history.churn["pkg/core.py"] == 4
    assert history.fixes["pkg/core.py"] == 3
    assert history.co_changed(["pkg/core.py"], min_count=3) == [("pkg/jobs.py", "pkg/core.py", 4)]


def test_history_outside_git_is_empty(tmp_path):
    assert load_history(tmp_path).commits == []


def test_parse_log_reads_ai_trailers():
    text = (
        "\x1eabc\x1ffix: crash\x1f\x1fbody\nCo-Authored-By: Claude <noreply@anthropic.com>\x1f\n"
        "a.py\nb.py\n\x1edef\x1fadd feature\x1f\x1f\x1f\nc.py\n"
    )
    first, second = parse_log(text)
    assert first.files == ["a.py", "b.py"] and first.is_fix and first.ai_assisted
    assert second.files == ["c.py"] and not second.is_fix and not second.ai_assisted


# --- analyzers ------------------------------------------------------------------------


def test_complexity_counts_branches():
    import ast

    tree = ast.parse("def f(x):\n    if x and x > 1:\n        return [i for i in x if i]\n")
    assert complexity(tree.body[0]) == 5  # 1 + if + and + for + comprehension if


def test_python_findings_flag_complexity_and_shell():
    source = COMPLEX + "\nimport subprocess\n\ndef run(c):\n    subprocess.run(c, shell=True)\n"
    rules = {f.rule: f for f in python_findings("m.py", source)}
    assert rules["high-complexity"].severity == "high"
    assert rules["high-complexity"].data["function"] == "decide"
    assert rules["shell-command"].category == "security"


def test_fingerprint_survives_moved_lines():
    a = python_findings("m.py", COMPLEX)[0]
    b = python_findings("m.py", "\n\n\n" + COMPLEX)[0]
    assert a.line != b.line and a.fingerprint == b.fingerprint


def test_secrets_are_found_but_placeholders_are_not():
    found = text_findings(
        "conf.py", 'API_KEY = "a8f9d0c7e6b5a4f3d2c1"\nTOKEN = "your-token-here-123"\n'
    )
    assert [f.rule for f in found] == ["secret"]
    assert text_findings("tests/test_conf.py", 'API_KEY = "a8f9d0c7e6b5a4f3d2c1"\n') == []


# --- RepoDoctor -----------------------------------------------------------------------


def test_scores_fall_with_findings_but_stay_in_range():
    clean = scores_for([], 10)
    assert clean["overall"] == 100
    many = [{"category": "security", "severity": "high"}] * 50
    worse = scores_for(many, 10)
    assert 0 <= worse["security"] < 5 and worse["quality"] == 100
    assert worse["overall"] < clean["overall"]


def test_findings_open_resolve_reopen_and_ignore(tmp_path):
    settings = Settings(root=tmp_path)
    one = Finding("long-function", "quality", "low", "a.py", "f uzun", key="f")
    two = Finding("missing-tests", "testing", "low", "b.py", "Test yok")
    with HealthStore(health_path(settings)) as store:
        first = store.record("r", "", [one, two], 2, 0.1)
        assert (first.new, first.open, first.resolved) == (2, 2, 0)
        second = store.record("r", "", [two], 2, 0.1)
        assert (second.new, second.resolved) == (0, 1)
        ids = {f["rule"]: f["id"] for f in store.findings(None)}
        third = store.record("r", "", [one, two], 2, 0.1)
        assert third.reopened == 1
        assert store.get(ids["long-function"])["reopened"] == 1
        store.set_status(ids["missing-tests"], "ignored")
        store.record("r", "", [one], 2, 0.1)
        assert store.get(ids["missing-tests"])["status"] == "ignored"  # people decide
        assert len(store.snapshots()) == 4


def test_scan_and_weekly_report(repo):
    (repo / "pkg" / "rules.py").write_text(COMPLEX)
    settings = Settings(root=repo)
    result = scan(settings)
    assert result.open >= 2 and result.scores["overall"] < 100
    report = weekly_report(settings)
    assert "Genel skor" in report and "tokenwise fix" in report
    with HealthStore(health_path(settings)) as store:
        rules = {f["rule"] for f in store.open_findings()}
    assert {"high-complexity", "missing-tests"} <= rules


def test_task_for_turns_findings_into_requests():
    finding = {"id": 3, "rule": "high-complexity", "file": "m.py", "line": 4, "title": "t",
               "detail": "", "data": {"function": "decide"}}  # fmt: skip
    task = task_for(finding)
    assert "decide" in task["request"] and task["files"] == ["m.py"]
    assert task["intent"] == "change"
    cycle = {**finding, "rule": "import-cycle", "data": {"files": ["a.py", "b.py"]}}
    assert task_for(cycle)["intent"] == "plan" and task_for(cycle)["files"] == ["a.py", "b.py"]


# --- ReleaseGuard ---------------------------------------------------------------------


def test_risk_of_a_branch_touching_a_hotspot(repo):
    sh(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "change total\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
           {"pkg/core.py": "def total(xs):\n    return sum(x for x in xs if x)\n"})  # fmt: skip
    report = assess(Settings(root=repo), "main")
    assert report.changed == ["pkg/core.py"]
    assert set(report.affected) == {"pkg/api.py", "pkg/jobs.py", "pkg/store.py"}
    signals = {s.name: s for s in report.signals}
    assert signals["history"].value > 0  # three fix commits on core.py
    assert signals["tests"].value > 0  # core.py is changed and no test changed
    assert signals["criticality"].value > 0
    assert report.ai_commits == 1 and report.commits == 1
    assert 0 < report.score <= 100
    assert any("pkg/core.py" in item for item in report.checklist)
    assert report.suggestions[0]["files"] == ["pkg/core.py"]
    text = to_markdown(report)
    assert "Risk:" in text and "kontrol listesi" in text and "pkg/jobs.py" in text


def test_uncommitted_changes_count_and_no_change_scores_zero(repo):
    assert assess(Settings(root=repo), "main").score == 0
    (repo / "pkg" / "api.py").write_text("def handler(xs):\n    return 0\n")
    report = assess(Settings(root=repo), "main")
    assert report.changed == ["pkg/api.py"]


def test_risk_outside_git_fails(tmp_path):
    with pytest.raises(GuardError):
        assess(Settings(root=tmp_path), "")


# --- CLI and web ----------------------------------------------------------------------


def test_cli_scan_findings_risk(repo):
    (repo / "pkg" / "rules.py").write_text(COMPLEX)
    result = runner.invoke(cli.app, ["scan", "-r", str(repo), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["open"] >= 2
    rows = json.loads(runner.invoke(cli.app, ["findings", "-r", str(repo), "--json"]).output)
    assert rows[0]["priority"] >= rows[-1]["priority"]
    ignored = runner.invoke(cli.app, ["findings", "-r", str(repo), "--ignore", str(rows[0]["id"])])
    assert "ignored" in ignored.output
    risk = runner.invoke(cli.app, ["risk", "-r", str(repo), "-b", "main", "-f", "json"])
    assert risk.exit_code == 0 and json.loads(risk.output)["changed"] == ["pkg/rules.py"]
    fail = runner.invoke(cli.app, ["risk", "-r", str(repo), "-b", "main", "--fail-above", "1"])
    assert fail.exit_code == 1
    md = runner.invoke(cli.app, ["findings", "-r", str(repo), "--report"])
    assert "Haftalık sağlık raporu" in md.output


def test_cli_fix_applies_and_verifies(repo, monkeypatch, fake):
    (repo / "pkg" / "rules.py").write_text(COMPLEX)
    settings = Settings(root=repo)
    scan(settings)
    with HealthStore(health_path(settings)) as store:
        target = next(f for f in store.open_findings() if f["rule"] == "high-complexity")
    simple = "def decide(a, b, c, d):\n    return a if b and 0 <= a < 12 else -1\n"
    edit = "pkg/rules.py\n<<<<<<< SEARCH\n" + COMPLEX + "=======\n" + simple + ">>>>>>> REPLACE\n"
    llm = fake('["pkg/rules.py"]', edit)
    monkeypatch.setattr(cli, "make_llm", lambda s: llm)
    result = runner.invoke(cli.app, ["fix", str(target["id"]), "-r", str(repo), "-y"])
    assert result.exit_code == 0, result.output
    assert "Verified" in result.output
    assert (repo / "pkg" / "rules.py").read_text() == simple


def test_web_health_findings_and_risk(repo):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from tokenwise.web import create_app

    (repo / "pkg" / "rules.py").write_text(COMPLEX)
    client = TestClient(create_app(repo, llm_factory=lambda s: None))
    pid = client.get("/api/projects").json()[0]["id"]
    assert client.get(f"/api/projects/{pid}/health").json()["latest"] is None
    scanned = client.post(f"/api/projects/{pid}/health/scan").json()
    assert scanned["open"] >= 2
    info = client.get(f"/api/projects/{pid}/health").json()
    assert info["latest"]["overall"] == scanned["scores"]["overall"]
    first = info["findings"][0]
    task = client.get(f"/api/projects/{pid}/findings/{first['id']}/task").json()
    assert task["request"] and task["files"]
    status = client.post(
        f"/api/projects/{pid}/findings/{first['id']}/status", json={"status": "ignored"}
    )
    assert status.json()["status"] == "ignored"
    assert (
        client.post(
            f"/api/projects/{pid}/findings/999/status", json={"status": "ignored"}
        ).status_code
        == 404
    )
    verify = client.post(f"/api/projects/{pid}/findings/{first['id']}/verify").json()
    assert verify["resolved"] is False
    risk = client.post(f"/api/projects/{pid}/risk", json={"base": "main"}).json()
    assert risk["changed"] == ["pkg/rules.py"] and "markdown" in risk
    assert client.post(f"/api/projects/{pid}/risk", json={"base": "nope"}).status_code == 400
