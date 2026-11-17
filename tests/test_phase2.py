"""Container checks, external analyzers and function-level impact."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_platform import commit, sh
from tokenwise.analyzers import Finding
from tokenwise.callgraph import (
    changed_lines,
    definitions,
    function_impact,
    touched,
    tree_sitter_available,
)
from tokenwise.checker import CheckResult, check_in_copy, docker_args, run_in_docker
from tokenwise.config import Settings, load_settings
from tokenwise.external import (
    bandit_findings,
    coverage_findings,
    read_coverage,
    ruff_findings,
    run_tools,
)
from tokenwise.graph import build_graph
from tokenwise.health import HealthStore, health_path, scan, source_of
from tokenwise.release import assess

# A Docker image with python (and pytest) to run the real container tests, e.g.
# TOKENWISE_DOCKER_IMAGE=python:3.12-slim. Without it they are skipped.
DOCKER_IMAGE = os.getenv("TOKENWISE_DOCKER_IMAGE", "")
needs_docker = pytest.mark.skipif(
    not DOCKER_IMAGE or shutil.which("docker") is None, reason="set TOKENWISE_DOCKER_IMAGE"
)


# ---------------------------------------------------------------- sandbox


def test_sandbox_setting_is_validated(tmp_path):
    (tmp_path / "tokenwise.toml").write_text('[tokenwise]\nsandbox = "vm"\n', "utf-8")
    with pytest.raises(ValueError):
        load_settings(tmp_path)


def test_docker_args_lock_the_container_down(tmp_path):
    args = docker_args(Settings(root=tmp_path), tmp_path, "n1", tmp_path / "checks")
    joined = " ".join(args)
    for part in ("--network none", "--read-only", "--cap-drop ALL", "--pids-limit 256",
                 "no-new-privileges", f"{tmp_path}:/work", "/checks:ro", "--memory 1g"):  # fmt: skip
        assert part in joined


def test_missing_docker_is_a_skip_not_a_failure(tmp_path, monkeypatch):
    import tokenwise.checker as checker

    def no_docker(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(checker.subprocess, "run", no_docker)
    result = run_in_docker(Settings(root=tmp_path, sandbox="docker"), "true", tmp_path)
    assert result.skipped and not result.passed


def test_docker_error_is_a_skip(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "docker"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 'Cannot connect to the Docker daemon' >&2\nexit 125\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake.parent))
    if os.name == "nt":
        pytest.skip("shell script stub")
    result = run_in_docker(Settings(root=tmp_path, sandbox="docker"), "true", tmp_path)
    assert result.skipped and "Cannot connect" in result.output


def test_unreachable_daemon_is_a_skip_whatever_the_exit_code(tmp_path, monkeypatch):
    import subprocess as sp

    import tokenwise.checker as checker

    def down(args, **kwargs):
        return sp.CompletedProcess(args, 1, "", "error during connect: open //./pipe/docker")

    monkeypatch.setattr(checker.subprocess, "run", down)
    result = run_in_docker(Settings(root=tmp_path, sandbox="docker"), "true", tmp_path)
    assert result.skipped and not result.passed


def test_local_sandbox_still_runs_on_the_host(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", "utf-8")
    result = check_in_copy(Settings(root=tmp_path), [], '{python} -c "import a"')
    assert result == CheckResult(True, "")


@needs_docker
def test_real_container_has_no_network_and_a_read_only_system(tmp_path):
    (tmp_path / "m.py").write_text("X = 1\n", "utf-8")
    settings = Settings(root=tmp_path, sandbox="docker", sandbox_image=DOCKER_IMAGE,
                        check_timeout=20)  # fmt: skip
    assert check_in_copy(settings, [], '{python} -c "import m; assert m.X == 1"').passed
    net = check_in_copy(settings, [], "python -c \"import socket; "
                        "socket.create_connection(('1.1.1.1', 80), timeout=3)\"")  # fmt: skip
    assert not net.passed and not net.skipped
    ro = check_in_copy(settings, [], "python -c \"open('/usr/x', 'w')\"")
    assert not ro.passed and "Read-only" in ro.output
    wrote = check_in_copy(settings, [], "python -c \"open('m.py', 'w').write('gone')\"")
    assert wrote.passed and (tmp_path / "m.py").read_text("utf-8") == "X = 1\n"


@needs_docker
def test_real_container_is_stopped_on_timeout(tmp_path):
    settings = Settings(root=tmp_path, sandbox="docker", sandbox_image=DOCKER_IMAGE,
                        check_timeout=5)  # fmt: skip
    result = check_in_copy(settings, [], 'python -c "import time; time.sleep(60)"')
    assert not result.passed and "timed out" in result.output
    left = subprocess.run(["docker", "ps", "-q", "--filter", "name=tokenwise-check-"],
                          capture_output=True, text=True).stdout  # fmt: skip
    assert left.strip() == ""


# ---------------------------------------------------------------- external tools


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
def test_ruff_groups_hits_per_file_and_rule(tmp_path):
    (tmp_path / "a.py").write_text("import os\nimport sys\nx = undefined_name\n", "utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "conf.py").write_text("y = also_undefined\n", "utf-8")
    files = {"a.py", "docs/conf.py"}
    # no Ruff config of its own: only bug rules, not style it never chose
    by_rule = {f.rule: f for f in ruff_findings(Settings(root=tmp_path), files)}
    assert set(by_rule) == {"ruff:F821"} and by_rule["ruff:F821"].severity == "high"
    assert by_rule["ruff:F821"].file == "a.py"  # docs are not the product
    (tmp_path / "ruff.toml").write_text('[lint]\nselect = ["F"]\n', "utf-8")
    by_rule = {f.rule: f for f in ruff_findings(Settings(root=tmp_path), files)}
    assert by_rule["ruff:F401"].data["count"] == 2 and "2 yerde" in by_rule["ruff:F401"].title


@pytest.mark.skipif(shutil.which("bandit") is None, reason="bandit not installed")
def test_bandit_names_the_function_not_the_line(tmp_path):
    (tmp_path / "a.py").write_text(
        "import subprocess\n\ndef run(cmd):\n    subprocess.call(cmd, shell=True)\n", "utf-8"
    )
    found = bandit_findings(Settings(root=tmp_path), {"a.py"})
    hit = next(f for f in found if f.rule == "bandit:B602")
    assert hit.category == "security" and hit.key == "B602@run" and hit.severity == "high"
    assert "bandit:B404" not in {f.rule for f in found}  # low severity: left out


def test_coverage_json_and_xml_are_read(tmp_path):
    (tmp_path / "coverage.json").write_text(json.dumps({"files": {
        "pkg/a.py": {"summary": {"num_statements": 40, "covered_lines": 4}},
        "pkg/b.py": {"summary": {"num_statements": 40, "covered_lines": 36}},
        "pkg/tiny.py": {"summary": {"num_statements": 3, "covered_lines": 0}},
    }}), "utf-8")  # fmt: skip
    coverage = read_coverage(tmp_path)
    assert coverage == {"pkg/a.py": 0.1, "pkg/b.py": 0.9}
    found = coverage_findings(coverage, {"pkg/a.py", "pkg/b.py"})
    assert [(f.file, f.severity) for f in found] == [("pkg/a.py", "medium")]

    (tmp_path / "coverage.json").unlink()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n", "utf-8")
    lines = "".join(f'<line number="{i}" hits="{int(i < 15)}"/>' for i in range(30))
    (tmp_path / "coverage.xml").write_text(
        f'<coverage><sources><source>{tmp_path}</source></sources><packages><package>'
        f'<classes><class filename="pkg/a.py"><lines>{lines}</lines></class></classes>'
        "</package></packages></coverage>", "utf-8")  # fmt: skip
    assert read_coverage(tmp_path) == {"pkg/a.py": 0.5}


def test_unknown_or_missing_tools_are_named(tmp_path):
    run = run_tools(Settings(root=tmp_path), [], ("coverage", "pylint"))
    assert run.ran == [] and set(run.skipped) == {"coverage", "pylint"}


def test_coverage_replaces_the_import_guess_for_missing_tests(tmp_path):
    (tmp_path / "lib.py").write_text("def f():\n    return 1\n", "utf-8")
    settings = Settings(root=tmp_path, scan_tools=("coverage",))
    scan(settings, tools=())
    with HealthStore(health_path(settings)) as store:
        assert [f["rule"] for f in store.findings()] == ["missing-tests"]
    (tmp_path / "coverage.json").write_text(json.dumps({"files": {
        "lib.py": {"summary": {"num_statements": 25, "covered_lines": 25}}}}), "utf-8")  # fmt: skip
    result = scan(settings)
    assert result.tools == ["coverage"] and result.resolved == 1


def test_a_tool_that_did_not_run_does_not_resolve_its_findings(tmp_path):
    ruff = Finding("ruff:F401", "quality", "low", "a.py", "unused", key="F401")
    mine = Finding("todo-pile", "maintainability", "low", "a.py", "todo", key="t")
    with HealthStore(tmp_path / "h.db") as store:
        store.record("r", "1", [ruff, mine], 1, 0.1, sources={"builtin", "ruff"})
        result = store.record("r", "2", [], 1, 0.1, sources={"builtin"})
        assert result.resolved == 1
        assert {f["rule"] for f in store.findings("open")} == {"ruff:F401"}
    assert source_of("bandit:B602") == "bandit" and source_of("low-coverage") == "coverage"
    assert source_of("missing-tests") == "builtin"


# ---------------------------------------------------------------- function-level impact


def test_python_definitions_and_touched_lines():
    source = "class A:\n    def m(self):\n        return 1\n\ndef f():\n    return A().m()\n"
    defs = definitions("x.py", source)
    assert [d.name for d in defs] == ["A.m", "f"] or [d.name for d in defs] == ["f", "A.m"]
    assert [d.name for d in touched(defs, {3})] == ["A.m"]
    assert {d.name for d in touched(defs, None)} == {"A.m", "f"}


@pytest.fixture
def py_repo(tmp_path: Path) -> Path:
    sh(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "start", {
        "pkg/__init__.py": "",
        "pkg/util.py": "def size(x):\n    return len(x)\n\n\ndef other():\n    return 0\n",
        "pkg/api.py": "from pkg.util import size\n\n\nclass Client:\n"
                      "    def send(self, body):\n        return size(body)\n",
        "pkg/unrelated.py": "def size(x):\n    return 0\n\n\ndef use():\n    return size(1)\n",
        "tests/test_util.py": "from pkg import util\n\n\ndef test_size():\n"
                              "    assert util.size('ab') == 2\n",
    })  # fmt: skip
    sh(tmp_path, "checkout", "-q", "-b", "feature")
    return tmp_path


def test_function_impact_finds_callers_and_tests_through_imports(py_repo):
    path = py_repo / "pkg" / "util.py"
    path.write_text(path.read_text("utf-8").replace("len(x)", "len(x) or 0"), "utf-8")
    settings = Settings(root=py_repo)
    graph = build_graph(settings)
    assert changed_lines(py_repo, "main", "pkg/util.py") == {2}
    impact = function_impact(py_repo, graph, "main", ["pkg/util.py"],
                             lambda p: (py_repo / p).read_text("utf-8"))  # fmt: skip
    assert len(impact) == 1
    assert impact[0].function == "size"
    assert impact[0].callers == ["pkg/api.py:Client.send"]  # not pkg/unrelated.py
    assert impact[0].tests == ["tests/test_util.py:test_size"]
    report = assess(settings, "main")
    assert report.functions[0]["function"] == "size"
    impact_reasons = next(s for s in report.signals if s.name == "impact").reasons
    assert any("pkg/util.py:size" in r and "1 test" in r for r in impact_reasons)


def test_new_file_counts_every_function(py_repo):
    (py_repo / "pkg" / "new.py").write_text("def a():\n    return 1\n", "utf-8")
    assert changed_lines(py_repo, "main", "pkg/new.py") is None
    report = assess(Settings(root=py_repo), "main")
    assert [f["function"] for f in report.functions] == ["a"]


@pytest.mark.skipif(not tree_sitter_available(), reason="tree-sitter not installed")
def test_typescript_functions_methods_and_arrows(tmp_path):
    sh(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "start", {
        "src/math.ts": "export function add(a: number, b: number) {\n  return a + b;\n}\n"
                       "export const twice = (x: number) => add(x, x);\n",
        "src/calc.ts": "import { add } from './math';\n\nexport class Calc {\n"
                       "  sum(xs: number[]) {\n    return xs.reduce((a, b) => add(a, b), 0);\n"
                       "  }\n}\n",
        "src/math.test.ts": "import { add } from './math';\n\ntest('adds', () => {\n"
                            "  expect(add(1, 2)).toBe(3);\n});\n",
    })  # fmt: skip
    defs = definitions("src/math.ts", (tmp_path / "src/math.ts").read_text("utf-8"))
    assert {d.name for d in defs} == {"add", "twice"}
    sh(tmp_path, "checkout", "-q", "-b", "feature")
    path = tmp_path / "src" / "math.ts"
    path.write_text(path.read_text("utf-8").replace("a + b", "b + a"), "utf-8")
    report = assess(Settings(root=tmp_path), "main")
    (item,) = report.functions
    assert item["function"] == "add"
    assert item["callers"] == ["src/calc.ts:Calc.sum", "src/math.ts:twice"]
    assert item["tests"] == ["src/math.test.ts:<module>"]
