"""Compares tokenwise with the usual approach on real tasks, in throwaway copies of the project.

baseline   every source file goes to one model, which returns whole changed files
tokenwise  map → chosen files → routed model → edit blocks (the normal pipeline)

Each run happens in a fresh temporary copy, the change is applied there and the task's check
command (for example its tests) decides whether the change works. The original never changes.

Besides "did it solve the task" a run measures what else matters for trusting a change:
- regression: with a `suite` (the project's whole test command), a change that turns a
  passing suite into a failing one broke something else, even if the task's check passed;
- cost, time, model calls and whether the router had to escalate;
- with --repeat, how much the solve rate moves between identical runs (models are not
  deterministic), so a difference between two versions can be told from noise.
Results can be saved as JSON and compared with an earlier run (`--compare`).
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from .checker import copy_project, run_command
from .config import Settings
from .editor import Change, EditError, apply_changes, resolve
from .llm import LLM
from .pipeline import Result, run
from .prompts import BASELINE_SYSTEM
from .repo_map import list_source_files
from .textio import read_source

METHODS = ("baseline", "tokenwise")
CHECK_TIMEOUT = 300
_FILE_BLOCK = re.compile(
    r"^=== FILE: (?P<path>.+?) ===\n(?P<body>.*?)^=== END FILE ===", re.S | re.M
)


@dataclass
class Task:
    name: str
    project: Path
    request: str
    check: str = ""
    suite: str = ""  # the project's own tests: a pass before and a fail after = regression


@dataclass
class Outcome:
    task: Task
    method: str
    result: Result
    passed: bool | None  # None = the task has no check
    seconds: float
    check_output: str = ""
    run: int = 1
    suite_before: bool | None = None  # None: no suite, or it could not run
    suite_after: bool | None = None  # None: no suite, or nothing was applied

    @property
    def regression(self) -> bool:
        return self.suite_before is True and self.suite_after is False

    @property
    def calls(self) -> int:
        return len(self.result.replies)

    @property
    def escalated(self) -> bool:
        """The edit came from a stronger model than the router started with."""
        r = self.result
        return bool(r.model and r.models and r.model != r.models[0])


REPOS_DIR = ".repos"
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def fetch_repo(repo: str, ref: str, cache: Path) -> Path:
    """Clones github.com/<repo> at commit `ref` into cache/<owner>__<name>__<ref>, once."""
    if not _REPO.match(repo):
        raise ValueError(f"repo must look like owner/name, got {repo!r}")
    if not _SHA.match(ref):
        raise ValueError(
            f"ref must be a commit hash (so every run uses the same code), got {ref!r}"
        )
    target = cache / f"{repo.replace('/', '__')}__{ref[:12]}"
    if (target / ".git").is_dir():
        return target
    target.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=target, check=True, capture_output=True, timeout=600)

    try:
        git("init", "-q")
        git("fetch", "-q", "--depth", "1", f"https://github.com/{repo}", ref)
        git("checkout", "-q", "FETCH_HEAD")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        raise ValueError(
            f"could not fetch {repo}@{ref}: {detail.decode(errors='replace')}"
        ) from None
    return target


def load_tasks(path: Path) -> list[Task]:
    """Reads [[task]] tables from a TOML file.

    A task names its code either with `project` (a folder, relative to the file) or with
    `repo` = "owner/name" plus `ref` = a commit hash, which is cloned into .repos/ next to
    the file on first use. `subdir` picks a folder inside the repository.
    """
    data = tomllib.loads(path.read_text("utf-8"))
    tasks = []
    for item in data.get("task", []):
        name = item.get("name", "?")
        missing = {"name", "request"} - item.keys()
        if "project" not in item and not {"repo", "ref"} <= item.keys():
            missing.add("project (or repo and ref)")
        if missing:
            raise ValueError(f"task {name}: missing {', '.join(sorted(missing))}")
        if "project" in item:
            project = (path.parent / item["project"]).resolve()
        else:
            project = fetch_repo(item["repo"], item["ref"], path.parent / REPOS_DIR)
            project = (project / item.get("subdir", ".")).resolve()
        if not project.is_dir():
            raise ValueError(f"task {name}: project folder {project} does not exist")
        tasks.append(Task(name, project, item["request"], item.get("check", ""),
                          item.get("suite", data.get("suite", ""))))  # fmt: skip
    if not tasks:
        raise ValueError(f"{path}: no [[task]] entries")
    return tasks


def baseline_model(settings: Settings) -> str:
    """The middle rung: what people typically reach for when they just 'ask the model'."""
    return settings.models[min(1, len(settings.models) - 1)]


def parse_files(text: str) -> list[tuple[str, str]]:
    return [(m.group("path").strip(), m.group("body")) for m in _FILE_BLOCK.finditer(text)]


def baseline(request: str, settings: Settings, llm: LLM, model: str) -> Result:
    result = Result(request=request, level="baseline", models=[model], prices=settings.prices)
    files = list_source_files(settings)
    result.files = files
    parts = [f"Request: {request}", "", "Project files:"]
    for rel in files:
        parts.append(f"{rel}\n```\n{read_source(settings.root / rel, strict=False)}\n```")
    reply = llm.complete(
        model=model,
        system=BASELINE_SYSTEM,
        prompt="\n".join(parts),
        max_tokens=max(settings.max_output_tokens, 16_000),
    )
    reply.role = "baseline"
    result.replies.append(reply)
    result.model = model

    try:
        for rel, body in parse_files(reply.text):
            path = resolve(settings.root, rel)
            old = read_source(path, strict=False) if path.exists() else None
            if body != old:
                result.changes.append(Change(rel, old, body))
    except EditError as exc:
        result.error = str(exc)
        return result
    if not result.changes:
        result.error = "no whole-file blocks in the reply"
    return result


_suite_before: dict[tuple[str, str], bool | None] = {}


def suite_before(task: Task, checks_dir: Path) -> bool | None:
    """Whether the project's suite passes before any change (once per project and suite)."""
    key = (str(task.project), task.suite)
    if key not in _suite_before:
        with tempfile.TemporaryDirectory(prefix="tokenwise-suite-") as tmp:
            copy = Path(tmp) / task.project.name
            copy_project(task.project, copy)
            done = run_command(task.suite, copy, CHECK_TIMEOUT, checks_dir)
            _suite_before[key] = None if done.skipped else done.passed
    return _suite_before[key]


def run_task(
    task: Task, method: str, settings: Settings, llm: LLM, checks_dir: Path, model: str = "",
    run_number: int = 1,
) -> Outcome:  # fmt: skip
    before = suite_before(task, checks_dir) if task.suite else None
    with tempfile.TemporaryDirectory(prefix="tokenwise-bench-") as tmp:
        copy = Path(tmp) / task.project.name
        copy_project(task.project, copy)
        local = replace(settings, root=copy, cache=False, check="")

        start = time.perf_counter()
        if method == "baseline":
            result = baseline(task.request, local, llm, model or baseline_model(settings))
        else:
            result = run(task.request, local, llm, model)
        seconds = time.perf_counter() - start

        passed: bool | None = None
        after: bool | None = None
        output = ""
        if result.ok:
            apply_changes(local, result.changes)
            if task.check:
                check = run_command(task.check, copy, CHECK_TIMEOUT, checks_dir)
                passed = None if check.skipped else check.passed
                output = check.output
            if task.suite and before is not None:
                suite = run_command(task.suite, copy, CHECK_TIMEOUT, checks_dir)
                after = None if suite.skipped else suite.passed
                if after is False and before:
                    output += "\n--- suite (regression) ---\n" + suite.output[-3000:]
        elif task.check:
            passed = False
    return Outcome(task, method, result, passed, seconds, output, run_number, before, after)


def savings(before: float, after: float) -> str:
    """Relative change from baseline to tokenwise; negative means tokenwise used less."""
    if not before:
        return "–"
    return f"{100 * (after - before) / before:+.0f}%"


def totals(outcomes: list[Outcome], method: str) -> dict:
    mine = [o for o in outcomes if o.method == method]
    checked = [o for o in mine if o.passed is not None]
    passed = sum(bool(o.passed) for o in checked)
    cost = sum(o.result.cost for o in mine)
    guarded = [o for o in mine if o.suite_before is True and o.suite_after is not None]
    rates = run_rates(mine)
    return {
        "tasks": len(mine),
        "ok": sum(o.result.ok for o in mine),
        "passed": passed,
        "checked": len(checked),
        "solve_rate": passed / len(checked) if checked else None,
        "solve_rate_min": min(rates) if rates else None,
        "solve_rate_max": max(rates) if rates else None,
        "runs": len(rates),
        "regressions": sum(o.regression for o in mine),
        "suite_runs": len(guarded),
        "input": sum(o.result.usage.input_tokens + o.result.usage.cache_read_tokens for o in mine),
        "output": sum(o.result.usage.output_tokens for o in mine),
        "cost": cost,
        "cost_per_solved": cost / passed if passed else None,
        "lines": sum(sum(o.result.lines) for o in mine),
        "seconds": sum(o.seconds for o in mine),
        "median_seconds": statistics.median([o.seconds for o in mine]) if mine else 0.0,
        "calls": sum(o.calls for o in mine),
        "escalated": sum(o.escalated for o in mine),
    }


def run_rates(outcomes: list[Outcome]) -> list[float]:
    """The solve rate of each repeat (run 1, run 2, ...) on its own."""
    rates = []
    for number in sorted({o.run for o in outcomes}):
        checked = [o for o in outcomes if o.run == number and o.passed is not None]
        if checked:
            rates.append(sum(bool(o.passed) for o in checked) / len(checked))
    return rates


def to_json(outcomes: list[Outcome]) -> dict:
    """Every run and the totals, to compare with a later run (`--compare`)."""
    methods = [m for m in METHODS if any(o.method == m for o in outcomes)]
    return {
        "runs": [
            {
                "task": o.task.name,
                "method": o.method,
                "run": o.run,
                "ok": o.result.ok,
                "error": o.result.error,
                "passed": o.passed,
                "regression": o.regression,
                "suite_before": o.suite_before,
                "suite_after": o.suite_after,
                "model": o.result.model,
                "models": o.result.models,
                "level": o.result.level,
                "route": o.result.route,
                "escalated": o.escalated,
                "calls": o.calls,
                "cost": round(o.result.cost, 6),
                "input": o.result.usage.input_tokens + o.result.usage.cache_read_tokens,
                "output": o.result.usage.output_tokens,
                "lines": sum(o.result.lines),
                "seconds": round(o.seconds, 2),
            }
            for o in outcomes
        ],  # fmt: skip
        "totals": {m: totals(outcomes, m) for m in methods},
    }


COMPARED = (  # (key, label, format, higher is better)
    ("solve_rate", "Solve rate", ".0%", True),
    ("regressions", "Regressions", "d", False),
    ("cost", "Cost ($)", ".4f", False),
    ("cost_per_solved", "Cost per solved task ($)", ".4f", False),
    ("median_seconds", "Median time (s)", ".1f", False),
    ("calls", "Model calls", "d", False),
    ("escalated", "Escalations", "d", False),
)


def compare(old: dict, new: dict) -> str:
    """A table of what moved between two saved runs, per method both of them have."""
    lines = []
    for method in [m for m in METHODS if m in old["totals"] and m in new["totals"]]:
        a, b = old["totals"][method], new["totals"][method]
        lines += [f"### {method}", "", "| | before | after | |", "| --- | ---: | ---: | :---: |"]
        for key, label, fmt, higher_better in COMPARED:
            x, y = a.get(key), b.get(key)
            if x is None or y is None:
                lines.append(f"| {label} | {'–' if x is None else format(x, fmt)} | "
                             f"{'–' if y is None else format(y, fmt)} | |")  # fmt: skip
                continue
            mark = "" if x == y else ("better" if (y > x) == higher_better else "worse")
            lines.append(f"| {label} | {format(x, fmt)} | {format(y, fmt)} | {mark} |")
        spread = b.get("solve_rate_max") is not None and b["runs"] > 1
        if spread:
            lines.append(f"\nSolve rate moved between {b['solve_rate_min']:.0%} and "
                         f"{b['solve_rate_max']:.0%} over {b['runs']} identical runs: a "
                         "difference smaller than that is noise.")  # fmt: skip
        lines.append("")
    return "\n".join(lines)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text("utf-8"))


def details(outcomes: list[Outcome]) -> str:
    """What went wrong, per failed run: the error or the diff plus the check's output."""
    parts = []
    for o in outcomes:
        if o.passed is not False and o.result.ok:
            continue
        diff = "".join(c.diff for c in o.result.changes)
        lines = [
            f"<details><summary>{o.task.name} · {o.method}: "
            f"{o.result.error or 'check failed'}</summary>",
            "",
        ]
        if diff:
            lines += ["```diff", diff.rstrip(), "```"]
        elif o.result.replies:  # nothing applied: show what the model last said
            last = o.result.replies[-1]
            text = last.text if len(last.text) <= 3000 else last.text[:3000] + "\n…"
            lines += [f"Last reply ({last.role}, {last.model}):", "", "```", text.rstrip(), "```"]
        if o.check_output:
            lines += ["", "```", o.check_output.rstrip(), "```"]
        lines += ["", "</details>", ""]
        parts.append("\n".join(lines))
    return ("\n## Failures\n\n" + "\n".join(parts)) if parts else ""


def markdown(outcomes: list[Outcome]) -> str:
    def check(o: Outcome) -> str:
        return "–" if o.passed is None else ("✅" if o.passed else "❌")

    def suite(o: Outcome) -> str:
        if o.regression:
            return "⚠️ broke"
        return "–" if o.suite_after is None else ("✅" if o.suite_after else "❌")

    repeats = len({o.run for o in outcomes}) > 1
    lines = [
        "| Task | Method | " + ("Run | " if repeats else "") + "Model | Calls | Input tok | "
        "Output tok | Cost ($) | Time (s) | Lines changed | Check | Suite |",
        "| --- | --- | " + ("---: | " if repeats else "") + "--- | ---: | ---: | ---: | ---: | "
        "---: | ---: | :---: | :---: |",
    ]
    for o in outcomes:
        u = o.result.usage
        model = o.result.model or (o.result.models[-1] if o.result.models else "")
        if o.escalated:
            model += " ↑"
        run = f"{o.run} | " if repeats else ""
        lines.append(
            f"| {o.task.name} | {o.method} | {run}{model} | {o.calls} | "
            f"{u.input_tokens + u.cache_read_tokens:,} | {u.output_tokens:,} | "
            f"{o.result.cost:.4f} | {o.seconds:.1f} | {sum(o.result.lines)} | {check(o)} | "
            f"{suite(o)} |"
        )
    lines.append("")
    lines.append("↑ = the router escalated to a stronger model. Suite ⚠️ = the task's check may "
                 "have passed, but the project's own tests broke.")  # fmt: skip

    methods = [m for m in METHODS if any(o.method == m for o in outcomes)]
    stats = {m: totals(outcomes, m) for m in methods}
    two = len(methods) == 2
    lines += ["", "| Total | " + " | ".join(methods) + (" | Change |" if two else " |"),
              "| --- |" + " ---: |" * len(methods) + (" ---: |" if two else "")]  # fmt: skip

    def row(label: str, cells: list[str], change: str = "") -> str:
        return (
            f"| {label} | " + " | ".join(cells) + (f" | {change} |" if len(methods) == 2 else " |")
        )

    def numeric(label: str, key: str, fmt: str) -> str:
        values = [stats[m][key] for m in methods]
        cells = ["–" if v is None else format(v, fmt) for v in values]
        change = savings(*values) if len(values) == 2 and None not in values else ""
        return row(label, cells, change)

    def solve(m: str) -> str:
        t = stats[m]
        if t["solve_rate"] is None:
            return "–"
        text = f"{t['passed']}/{t['checked']} ({t['solve_rate']:.0%})"
        if t["runs"] > 1:
            text += f", runs {t['solve_rate_min']:.0%}–{t['solve_rate_max']:.0%}"
        return text

    lines += [
        row("Solved (check passed)", [solve(m) for m in methods]),
        row(
            "Regressions (suite broke)",
            [f"{stats[m]['regressions']}/{stats[m]['suite_runs']}" for m in methods],
        ),  # fmt: skip
        numeric("Cost ($)", "cost", ".4f"),
        numeric("Cost per solved task ($)", "cost_per_solved", ".4f"),
        numeric("Input tokens", "input", ","),
        numeric("Output tokens", "output", ","),
        numeric("Lines changed", "lines", ","),
        numeric("Median time (s)", "median_seconds", ".1f"),
        numeric("Model calls", "calls", ","),
        row("Escalations", [str(stats[m]["escalated"]) for m in methods]),
    ]
    return "\n".join(lines) + "\n" + details(outcomes)
