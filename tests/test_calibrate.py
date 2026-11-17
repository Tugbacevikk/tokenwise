"""Calibration replays history as PRs; findings --stats counts false alarms per rule."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.test_platform import COMPLEX, commit, sh
from tokenwise import cli
from tokenwise.analyzers import Finding
from tokenwise.calibrate import (
    CalibrationError,
    auc,
    calibrate,
    read_log,
    save_weights,
    suggest_weights,
    tree_sources,
)
from tokenwise.config import Settings
from tokenwise.health import HealthStore, health_path
from tokenwise.release import WEIGHTS, load_weights, weights_path

runner = CliRunner()


@pytest.fixture
def history_repo(tmp_path: Path) -> Path:
    """A risky module (complex, imported by many) that keeps getting fixed, and a quiet one."""
    sh(tmp_path, "init", "-q", "-b", "main")
    users = {f"app/u{i}.py": "from app import hot\n" for i in range(5)}
    commit(tmp_path, "start", {"app/__init__.py": "", "app/hot.py": COMPLEX,
                               "app/quiet.py": "X = 1\n", **users})  # fmt: skip
    for n in range(30):
        if n % 3 == 0:
            commit(tmp_path, f"change hot {n}", {"app/hot.py": COMPLEX + f"\nV = {n}\n"})
            commit(tmp_path, f"fix hot bug {n}", {"app/hot.py": COMPLEX + f"\nV = {n} + 0\n"})
        else:
            commit(tmp_path, f"tweak quiet {n}", {"app/quiet.py": f"X = {n + 100}\n"})
    return tmp_path


def test_auc_handles_ties_and_one_sided_labels():
    assert auc([1, 2, 3, 4], [False, False, True, True]) == 1.0
    assert auc([1, 2, 3, 4], [True, True, False, False]) == 0.0
    assert auc([5, 5, 5, 5], [True, False, True, False]) == 0.5
    assert auc([1, 2], [True, True]) is None


def test_suggest_weights_drop_useless_signals_and_sum_to_one():
    weights = suggest_weights({"impact": 0.7, "history": 0.6, "tests": 0.4, "criticality": 0.5,
                               "complexity": None, "size": 0.55})  # fmt: skip
    assert weights["tests"] == weights["criticality"] == weights["complexity"] == 0
    assert weights["impact"] > weights["history"] > weights["size"] > 0
    assert abs(sum(weights.values()) - 1) < 1e-9
    assert suggest_weights(dict.fromkeys(WEIGHTS, 0.4)) == {}


def test_read_log_and_tree_sources(history_repo):
    settings = Settings(root=history_repo)
    log = read_log(history_repo, 1000)
    assert log[0].commit.subject == "start" and len(log) == 41
    assert log[1].stats == {"app/hot.py": (2, 0)}
    files = tree_sources(settings, log[0].commit.sha)
    assert "app/hot.py" in files and len(files["app/hot.py"]) == 40


def test_calibration_finds_the_risky_module(history_repo):
    settings = Settings(root=history_repo)
    result = calibrate(settings, sample=100, window=3)
    assert result.positives >= 10
    # every change to hot.py is risky by these signals; only the ones a fix followed are labelled
    assert result.auc["criticality"] > 0.75 and result.auc["complexity"] > 0.75
    assert result.auc["size"] is not None and result.auc["score"] > 0.75
    assert result.suggested and result.suggested["criticality"] > 0
    path = save_weights(settings, result)
    assert path == weights_path(settings)
    assert load_weights(settings) == result.suggested


def test_calibration_needs_history(tmp_path):
    sh(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "start", {"a.py": "x = 1\n"})
    with pytest.raises(CalibrationError):
        calibrate(Settings(root=tmp_path))


def test_broken_weights_file_falls_back_to_defaults(tmp_path):
    settings = Settings(root=tmp_path)
    weights_path(settings).parent.mkdir(parents=True)
    weights_path(settings).write_text(json.dumps({"weights": {"impact": 1.0}}), "utf-8")
    assert load_weights(settings) == WEIGHTS
    weights_path(settings).write_text("not json", "utf-8")
    assert load_weights(settings) == WEIGHTS


def test_cli_calibrate_saves_and_risk_uses_the_weights(history_repo):
    result = runner.invoke(cli.app, ["calibrate", "--root", str(history_repo), "--window", "3",
                                     "--save"])  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "Saved" in result.output
    sh(history_repo, "checkout", "-q", "-b", "feature")
    commit(history_repo, "touch hot", {"app/hot.py": COMPLEX + "\nW = 1\n"})
    out = runner.invoke(cli.app, ["risk", "--root", str(history_repo), "--format", "json"])
    report = json.loads(out.output)
    saved = load_weights(Settings(root=history_repo))
    assert {s["name"]: s["weight"] for s in report["signals"]} == saved
    data = runner.invoke(cli.app, ["calibrate", "--root", str(history_repo), "--window", "3",
                                   "--json"])  # fmt: skip
    assert json.loads(data.output)["positives"] >= 10


def test_rule_stats_and_cli(tmp_path):
    settings = Settings(root=tmp_path)
    a = Finding("todo-pile", "maintainability", "low", "a.py", "TODO", key="a")
    b = Finding("todo-pile", "maintainability", "low", "b.py", "TODO", key="b")
    c = Finding("shell-command", "security", "medium", "c.py", "shell", key="c")
    with HealthStore(health_path(settings)) as store:
        store.record("r", "1", [a, b, c], 3, 0.1)
        store.record("r", "2", [b, c], 3, 0.1)  # a resolved by the code
        ignored = store.db.execute("SELECT id FROM findings WHERE file = 'b.py'").fetchone()[0]
        store.set_status(ignored, "ignored")
        stats = {r["rule"]: r for r in store.rule_stats()}
    assert stats["todo-pile"]["resolved"] == 1 and stats["todo-pile"]["ignored"] == 1
    assert stats["todo-pile"]["false_alarm_rate"] == 0.5
    assert stats["shell-command"]["false_alarm_rate"] is None
    out = runner.invoke(cli.app, ["findings", "--root", str(tmp_path), "--stats", "--json"])
    assert {r["rule"] for r in json.loads(out.output)} == {"todo-pile", "shell-command"}
    out = runner.invoke(cli.app, ["findings", "--root", str(tmp_path), "--stats"])
    assert "50%" in out.output
