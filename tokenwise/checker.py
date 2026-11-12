"""Runs a check command (usually the tests) against a change, in a throwaway copy of the project.

The project itself is never touched: the copy gets the change, the command runs there, and
the result says whether it passed plus the last part of its output for the model to read.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from .config import Settings
from .editor import Change, apply_changes

COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".tokenwise", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache"
)
OUTPUT_TAIL = 3000


@dataclass
class CheckResult:
    passed: bool
    output: str


def expand(command: str, checks_dir: Path | None = None) -> str:
    command = command.replace("{python}", f'"{sys.executable}"')
    if checks_dir is not None:
        command = command.replace("{checks}", str(checks_dir))
    return command


def run_command(
    command: str, root: Path, timeout: int, checks_dir: Path | None = None
) -> CheckResult:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root), os.getenv("PYTHONPATH", "")])}
    try:
        done = subprocess.run(
            expand(command, checks_dir),
            shell=True,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(False, f"check timed out after {timeout} s")
    output = (done.stdout + done.stderr).strip()
    return CheckResult(done.returncode == 0, output[-OUTPUT_TAIL:])


def copy_project(source: Path, target: Path) -> None:
    shutil.copytree(source, target, ignore=COPY_IGNORE)


def check_in_copy(
    settings: Settings, changes: list[Change], command: str, checks_dir: Path | None = None
) -> CheckResult:
    with tempfile.TemporaryDirectory(prefix="tokenwise-check-") as tmp:
        copy = Path(tmp) / settings.root.name
        copy_project(settings.root, copy)
        apply_changes(replace(settings, root=copy), changes)
        return run_command(command, copy, settings.check_timeout, checks_dir)


def make_verifier(settings: Settings, command: str) -> Callable[[list[Change]], CheckResult]:
    return lambda changes: check_in_copy(settings, changes, command)
