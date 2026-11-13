"""Compares tokenwise with the usual approach on real tasks, in throwaway copies of the project.

baseline   every source file goes to one model, which returns whole changed files
tokenwise  map → chosen files → routed model → edit blocks (the normal pipeline)

Each run happens in a fresh temporary copy, the change is applied there and the task's check
command (for example its tests) decides whether the change works. The original never changes.
"""

from __future__ import annotations

import re
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


@dataclass
class Outcome:
    task: Task
    method: str
    result: Result
    passed: bool | None  # None = the task has no check
    seconds: float
    check_output: str = ""


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
        tasks.append(Task(name, project, item["request"], item.get("check", "")))
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


def run_task(
    task: Task, method: str, settings: Settings, llm: LLM, checks_dir: Path, model: str = ""
) -> Outcome:
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
        output = ""
        if result.ok:
            apply_changes(local, result.changes)
            if task.check:
                check = run_command(task.check, copy, CHECK_TIMEOUT, checks_dir)
                passed, output = check.passed, check.output
        elif task.check:
            passed = False
    return Outcome(task, method, result, passed, seconds, output)


def savings(before: float, after: float) -> str:
    """Relative change from baseline to tokenwise; negative means tokenwise used less."""
    if not before:
        return "–"
    return f"{100 * (after - before) / before:+.0f}%"


def totals(outcomes: list[Outcome], method: str) -> dict:
    mine = [o for o in outcomes if o.method == method]
    checked = [o for o in mine if o.passed is not None]
    return {
        "tasks": len(mine),
        "ok": sum(o.result.ok for o in mine),
        "passed": sum(bool(o.passed) for o in checked),
        "checked": len(checked),
        "input": sum(o.result.usage.input_tokens + o.result.usage.cache_read_tokens for o in mine),
        "output": sum(o.result.usage.output_tokens for o in mine),
        "cost": sum(o.result.cost for o in mine),
        "lines": sum(sum(o.result.lines) for o in mine),
        "seconds": sum(o.seconds for o in mine),
    }


def details(outcomes: list[Outcome]) -> str:
    """What went wrong, per failed run: the error or the diff plus the check's output."""
    parts = []
    for o in outcomes:
        if o.passed is not False and o.result.ok:
            continue
        diff = "".join(c.diff for c in o.result.changes) or "(no change)"
        lines = [
            f"<details><summary>{o.task.name} · {o.method}: "
            f"{o.result.error or 'check failed'}</summary>",
            "",
        ]
        lines += ["```diff", diff.rstrip(), "```"]
        if o.check_output:
            lines += ["", "```", o.check_output.rstrip(), "```"]
        lines += ["", "</details>", ""]
        parts.append("\n".join(lines))
    return ("\n## Failures\n\n" + "\n".join(parts)) if parts else ""


def markdown(outcomes: list[Outcome]) -> str:
    def check(o: Outcome) -> str:
        return "–" if o.passed is None else ("✅" if o.passed else "❌")

    lines = [
        "| Task | Method | Model | Input tok | Output tok | Cost ($) | Lines changed | Check |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | :---: |",
    ]
    for o in outcomes:
        u = o.result.usage
        model = o.result.model or (o.result.models[-1] if o.result.models else "")
        lines.append(
            f"| {o.task.name} | {o.method} | {model} | {u.input_tokens + u.cache_read_tokens:,} | "
            f"{u.output_tokens:,} | {o.result.cost:.4f} | {sum(o.result.lines)} | {check(o)} |"
        )

    methods = [m for m in METHODS if any(o.method == m for o in outcomes)]
    if len(methods) == 2:
        b, t = totals(outcomes, "baseline"), totals(outcomes, "tokenwise")

        def row(label: str, key: str, fmt: str) -> str:
            return f"| {label} | {b[key]:{fmt}} | {t[key]:{fmt}} | {savings(b[key], t[key])} |"

        lines += [
            "",
            "| Total | baseline | tokenwise | Change |",
            "| --- | ---: | ---: | ---: |",
            row("Cost ($)", "cost", ".4f"),
            row("Input tokens", "input", ","),
            row("Output tokens", "output", ","),
            row("Lines changed", "lines", ","),
            f"| Checks passed | {b['passed']}/{b['checked']} | {t['passed']}/{t['checked']} | |",
            f"| Time (s) | {b['seconds']:.0f} | {t['seconds']:.0f} | |",
        ]
    return "\n".join(lines) + "\n" + details(outcomes)
