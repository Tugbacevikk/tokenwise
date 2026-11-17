"""Findings from established tools: Ruff (lint), Bandit (security) and an existing coverage report.

Ruff and Bandit only read source files, like the built-in analyzers; nothing in the project is
executed. Coverage is never measured here: the report the project's own test run left behind
(`coverage.json` or `coverage.xml`) is read. A tool that is not installed is skipped and named in
the scan result, so a missing tool is never mistaken for a clean project."""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .analyzers import Finding
from .config import Settings
from .repo_map import is_support_file, is_test_file
from .textio import read_source

TOOLS = ("ruff", "bandit", "coverage")
TOOL_TIMEOUT = 300
LOW_COVERAGE = 0.5
VERY_LOW_COVERAGE = 0.2
MIN_STATEMENTS = 20  # tiny files say little about coverage
_BANDIT_SEVERITY = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}
_RUFF_SERIOUS = ("E9", "F821", "F822", "F823", "F63", "F7", "PLE")
# Without a Ruff configuration of its own, a project is held only to rules that point at real
# bugs: a style rule it never chose is noise, not a finding.
RUFF_DEFAULT_SELECT = "E9,F63,F7,F82,F811,F841,B006,B008,B023,B904,PLE"
_RUFF_CONFIGS = ("ruff.toml", ".ruff.toml")


def has_ruff_config(root: Path) -> bool:
    if any((root / name).exists() for name in _RUFF_CONFIGS):
        return True
    pyproject = root / "pyproject.toml"
    try:
        return "[tool.ruff" in pyproject.read_text("utf-8")
    except OSError:
        return False


@dataclass
class ToolRun:
    findings: list[Finding] = field(default_factory=list)
    ran: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # tool -> why
    coverage: dict[str, float] = field(default_factory=dict)  # file -> covered share


def _command(tool: str) -> list[str] | None:
    if path := shutil.which(tool):
        return [path]
    if importlib.util.find_spec(tool) is not None:
        return [sys.executable, "-m", tool]
    return None


def _run(args: list[str], root: Path) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            args, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=TOOL_TIMEOUT,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None


def _rel(root: Path, path: str) -> str:
    p = Path(path)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return p.as_posix()
    return p.as_posix().removeprefix("./")


def _enclosing_names(source: str) -> list[tuple[int, int, str]]:
    """(start, end, qualified name) of every function, for naming where an issue sits."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    spans = []
    stack: list[tuple[str, ast.AST]] = [("", tree)]
    while stack:
        prefix, parent = stack.pop()
        for node in ast.iter_child_nodes(parent):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                spans.append((node.lineno, node.end_lineno or node.lineno, name))
                stack.append((name + ".", node))
            elif isinstance(node, ast.ClassDef):
                stack.append((prefix + node.name + ".", node))
    return spans


def _where(spans: list[tuple[int, int, str]], line: int) -> str:
    best, size = "<module>", None
    for start, end, name in spans:
        if start <= line <= end and (size is None or end - start < size):
            best, size = name, end - start
    return best


def ruff_findings(settings: Settings, files: set[str]) -> list[Finding] | None:
    """One finding per file and rule, with how many places it occurs: a long lint list is
    noise, a rule that keeps firing in a file is worth one line."""
    command = _command("ruff")
    if command is None:
        return None
    args = [*command, "check", "--output-format", "json", "--exit-zero", "--no-cache", "--quiet"]
    if not has_ruff_config(settings.root):
        args += ["--isolated", "--select", RUFF_DEFAULT_SELECT]
    done = _run([*args, "."], settings.root)
    if done is None:
        return None
    try:
        items = json.loads(done.stdout or "[]")
    except ValueError:
        return None
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in items:
        path = _rel(settings.root, item.get("filename", ""))
        code = item.get("code") or "syntax"
        # tests break rules on purpose (an undefined name to provoke an error)
        if path in files and not is_support_file(path) and not is_test_file(path):
            grouped[(path, code)].append(item)
    found = []
    for (path, code), hits in sorted(grouped.items()):
        first = min(hits, key=lambda h: (h.get("location") or {}).get("row", 0))
        line = (first.get("location") or {}).get("row", 0)
        security = code.startswith("S")
        serious = code.startswith(_RUFF_SERIOUS) or code == "syntax"
        title = f"{code}: {first.get('message', '')}"
        if len(hits) > 1:
            title += f" ({len(hits)} yerde)"
        found.append(
            Finding(
                f"ruff:{code}",
                "security" if security else "quality",
                "high" if serious else "medium" if security else "low",
                path,
                title,
                "Ruff kuralı; ayrıntı: https://docs.astral.sh/ruff/rules/",
                line,
                code,
                {"count": len(hits), "tool": "ruff"},
            )
        )
    return found


def bandit_findings(settings: Settings, files: set[str]) -> list[Finding] | None:
    command = _command("bandit")
    if command is None:
        return None
    targets = sorted(f for f in files if f.endswith(".py") and not is_test_file(f)
                     and not is_support_file(f))  # fmt: skip
    if not targets:
        return []
    # medium severity and confidence and up: the low ones (assert, importing subprocess, a
    # "password" string, a guessed shell=True) are what makes people switch Bandit off
    done = _run([*command, "-f", "json", "-q", "-ll", "-ii", *targets], settings.root)
    if done is None:
        return None
    try:
        data = json.loads(done.stdout or "{}")
    except ValueError:
        return None
    spans_by_file: dict[str, list] = {}
    counts: dict[tuple[str, str], int] = defaultdict(int)
    found = []
    for item in sorted(data.get("results", []), key=lambda r: (r["filename"], r["line_number"])):
        path = _rel(settings.root, item["filename"])
        if path not in files:
            continue
        if path not in spans_by_file:
            try:
                spans_by_file[path] = _enclosing_names(
                    read_source(settings.root / path, strict=False)
                )
            except OSError:
                spans_by_file[path] = []
        where = _where(spans_by_file[path], item["line_number"])
        test_id = item.get("test_id", "B000")
        n = counts[(path, f"{test_id}@{where}")]
        counts[(path, f"{test_id}@{where}")] += 1
        key = f"{test_id}@{where}" + (f"#{n}" if n else "")
        found.append(
            Finding(
                f"bandit:{test_id}",
                "security",
                _BANDIT_SEVERITY.get(item.get("issue_severity", "LOW"), "low"),
                path,
                f"{test_id}: {item.get('issue_text', '')}",
                f"Güven: {item.get('issue_confidence', '?')}. {item.get('more_info', '')}".strip(),
                item["line_number"],
                key,
                {"tool": "bandit", "confidence": item.get("issue_confidence", "")},
            )
        )
    return found


def read_coverage(root: Path) -> dict[str, float] | None:
    """Covered share of statements per file from coverage.json or a Cobertura coverage.xml."""
    json_path, xml_path = root / "coverage.json", root / "coverage.xml"
    result: dict[str, float] = {}
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        for name, info in (data.get("files") or {}).items():
            summary = info.get("summary") or {}
            total = summary.get("num_statements", 0)
            if total >= MIN_STATEMENTS:
                result[_rel(root, name)] = summary.get("covered_lines", 0) / total
        return result
    if xml_path.exists():
        try:
            tree = ET.parse(xml_path)
        except (OSError, ET.ParseError):
            return None
        sources = [s.text.strip() for s in tree.iter("source") if s.text]
        for cls in tree.iter("class"):
            lines = list(cls.iter("line"))
            if len(lines) < MIN_STATEMENTS:
                continue
            hit = sum(int(line.get("hits", "0")) > 0 for line in lines)
            name = cls.get("filename", "")
            for source in sources:
                candidate = _rel(root, str(Path(source) / name))
                if (root / candidate).exists():
                    name = candidate
                    break
            result[_rel(root, name)] = hit / len(lines)
        return result
    return None


def coverage_findings(coverage: dict[str, float], files: set[str]) -> list[Finding]:
    found = []
    for path, share in sorted(coverage.items()):
        if path not in files or is_test_file(path) or share >= LOW_COVERAGE:
            continue
        found.append(
            Finding(
                "low-coverage",
                "testing",
                "medium" if share < VERY_LOW_COVERAGE else "low",
                path,
                f"Kapsam düşük (%{round(share * 100)})",
                "Projenin son test çalıştırmasının kapsam raporuna göre.",
                data={"coverage": round(share, 3), "tool": "coverage"},
            )
        )
    return found


# Built-in security rules that Bandit also reports: when Bandit runs, its finding on the same
# line is the one kept, so one problem is not counted twice.
_BANDIT_TWINS = {"dynamic-code": ("B102", "B307"), "shell-command": ("B602", "B604", "B605"),
                 "unsafe-deserialize": ("B301",), "unsafe-yaml": ("B506",)}  # fmt: skip


def drop_twins(found: list[Finding], extra: list[Finding]) -> list[Finding]:
    bandit = {(f.file, f.line, f.rule.split(":", 1)[1]) for f in extra
              if f.rule.startswith("bandit:")}  # fmt: skip
    return [
        f for f in found
        if not any((f.file, f.line, code) in bandit for code in _BANDIT_TWINS.get(f.rule, ()))
    ]  # fmt: skip


def run_tools(settings: Settings, files: list[str], tools: tuple[str, ...]) -> ToolRun:
    run = ToolRun()
    file_set = set(files)
    for tool in tools:
        if tool == "coverage":
            coverage = read_coverage(settings.root)
            if coverage is None:
                run.skipped[tool] = "coverage.json ya da coverage.xml yok"
                continue
            run.coverage = coverage
            run.findings += coverage_findings(coverage, file_set)
        elif tool in ("ruff", "bandit"):
            reader = ruff_findings if tool == "ruff" else bandit_findings
            found = reader(settings, file_set)
            if found is None:
                run.skipped[tool] = "kurulu değil ya da çalışmadı"
                continue
            run.findings += found
        else:
            run.skipped[tool] = "bilinmeyen araç"
            continue
        run.ran.append(tool)
    return run
