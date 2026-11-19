"""benchmark/reproduce and benchmark/labeling: the tools others use to check our numbers."""

import csv
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load(relative: str):
    spec = importlib.util.spec_from_file_location(Path(relative).stem, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = load("benchmark/reproduce/run.py")
sheets = load("benchmark/labeling/make_sheets.py")
score = load("benchmark/labeling/score.py")


def test_expected_numbers_cover_every_pinned_repository():
    import tomllib

    repos = tomllib.loads((ROOT / "benchmark/reproduce/repos.toml").read_text("utf-8"))["repo"]
    expected = json.loads((ROOT / "benchmark/reproduce/expected.json").read_text("utf-8"))
    assert {r["name"] for r in repos} == set(expected)
    assert all(len(r["commit"]) == 40 for r in repos)  # full SHAs: nothing can move


def test_compare_reports_only_what_moved_past_the_tolerance():
    expected = {"a": {"findings": 100, "auc": {"score": 0.70, "size": 0.60}}}
    same = [{"name": "a", "findings": 104, "auc": {"score": 0.71, "size": 0.59}}]
    assert run.compare(same, expected) == []
    moved = [{"name": "a", "findings": 110, "auc": {"score": 0.65, "size": 0.60}}]
    problems = run.compare(moved, expected)
    assert len(problems) == 2 and "110 findings" in problems[0] and "score" in problems[1]
    assert "no numbers" in run.compare([{"name": "b", "findings": 1, "auc": {}}], expected)[0]


def _results(tmp_path: Path) -> Path:
    results = tmp_path / "results"
    results.mkdir()
    rows = [{"repo": "flask", "commit": "c" * 40, "fingerprint": f"f{i}", "rule": rule,
             "title": f"t{i}", "detail": "", "file": "src/a.py", "line": i}
            for i, rule in enumerate(["long-function"] * 20 + ["import-cycle"] * 3)]  # fmt: skip
    (results / "findings.jsonl").write_text("\n".join(json.dumps(r) for r in rows), "utf-8")
    samples = [{"sha": f"{i:012x}", "subject": f"s{i}", "regression": i % 2 == 0,
                "fixed_by": f"{i + 1:012x}" if i % 2 == 0 else ""} for i in range(80)]  # fmt: skip
    (results / "flask.calibrate.json").write_text(json.dumps({"samples": samples}), "utf-8")
    return results


def test_sheets_are_the_same_for_everyone_and_hide_the_tool_s_answer(tmp_path):
    results = _results(tmp_path)
    first = sheets.finding_rows(results, sheets.random.Random(sheets.SEED))
    again = sheets.finding_rows(results, sheets.random.Random(sheets.SEED))
    assert first == again
    rules = [r["kural"] for r in first]
    assert rules.count("long-function") == sheets.PER_RULE and rules.count("import-cycle") == 3
    assert first[0]["bağlantı"].startswith("https://github.com/pallets/flask/blob/" + "c" * 40)
    commits = sheets.regression_rows(results, sheets.random.Random(sheets.SEED))
    assert len(commits) == sheets.REGRESSIONS and all(c["düzeltme"] for c in commits)
    sheets.write(tmp_path / "findings.csv", first)
    header = (tmp_path / "findings.csv").read_text("utf-8-sig").splitlines()[0]
    assert "_rule" not in header and "_fingerprint" not in header and "karar" in header
    key = json.loads((tmp_path / "findings.key.json").read_text("utf-8"))
    assert key["1"]["_rule"] in ("long-function", "import-cycle")


def test_scores_with_intervals_and_agreement(tmp_path):
    path = tmp_path / "a.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["satır", "karar"])
        w.writeheader()
        w.writerows([{"satır": 1, "karar": "d"}, {"satır": 2, "karar": "Yanlış"},
                     {"satır": 3, "karar": "?"}, {"satır": 4, "karar": "evet"}])  # fmt: skip
    assert score.read(path) == {1: True, 2: False, 3: None, 4: True}
    low, high = score.wilson(1, 10)
    assert low == pytest.approx(0.018, abs=0.001) and high == pytest.approx(0.404, abs=0.001)
    same = {1: True, 2: False, 3: True, 4: False}
    assert score.kappa(same, same) == (1.0, 4)
    flipped = {k: not v for k, v in same.items()}
    assert score.kappa(same, flipped)[0] == pytest.approx(-1.0)
    key = {"1": {"_rule": "a"}, "2": {"_rule": "a"}, "4": {"_rule": "b"}}
    assert score.per_rule(key, {1: True, 2: False, 4: True}) == [("a", 1, 2), ("b", 0, 1)]
