"""Runs a check command (usually the tests) against a change, in a throwaway copy of the project.

The project itself is never touched: the copy gets the change, the command runs there, and
the result says whether it passed plus the last part of its output for the model to read.

With `sandbox = "docker"` the command runs inside a container that sees only the copy: no
network, a read-only system, no capabilities, CPU, memory and process limits. When Docker is not
available the check is reported as skipped, never as a failure, so a model is not sent to fix a
problem that is not in the code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
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
    skipped: bool = False  # the check could not run here (exit code 77, e.g. a tool is missing)


SKIP_EXIT = 77  # the usual "skipped" exit code of test runners (automake, meson)


def expand(command: str, checks_dir: Path | str | None = None, python: str = "") -> str:
    """Fills {python} and {checks}, quoted: paths such as C:\\Users\\ADIL CEVIK contain spaces.
    A quoted part followed by more text ("C:\\a b\\checks"/t.py) is still one argument for
    both cmd.exe programs and POSIX shells."""
    command = command.replace("{python}", f'"{python or sys.executable}"')
    if checks_dir is not None:
        command = command.replace("{checks}", f'"{checks_dir}"')
    return command


def run_command(
    command: str, root: Path, timeout: int, checks_dir: Path | None = None
) -> CheckResult:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(root), os.getenv("PYTHONPATH", "")]),
        "PYTHONIOENCODING": "utf-8",  # Windows pipes default to the ANSI code page
        "PYTHONUTF8": "1",
    }
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
    output = (done.stdout + done.stderr).strip()[-OUTPUT_TAIL:]
    if done.returncode == SKIP_EXIT:
        return CheckResult(False, output, skipped=True)
    return CheckResult(done.returncode == 0, output)


DOCKER_ERROR = 125  # `docker run` itself failed: no daemon, no image, bad option


def docker_args(settings: Settings, root: Path, name: str, checks_dir: Path | None) -> list[str]:
    args = [
        "docker", "run", "--rm", "--name", name,
        "--network", "none",
        "--cpus", str(settings.sandbox_cpus),
        "--memory", settings.sandbox_memory,
        "--pids-limit", "256",
        "--read-only",
        "--tmpfs", "/tmp:rw,exec,size=512m",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "-e", "HOME=/tmp",
        "-e", "PYTHONPATH=/work",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "PYTHONUTF8=1",
        "-v", f"{root}:/work",
        "-w", "/work",
    ]  # fmt: skip
    if hasattr(os, "getuid"):  # write the copy as its owner, never as root
        args += ["--user", f"{os.getuid()}:{os.getgid()}"]
    if checks_dir is not None:
        args += ["-v", f"{checks_dir}:/checks:ro"]
    return args


def run_in_docker(
    settings: Settings, command: str, root: Path, checks_dir: Path | None = None
) -> CheckResult:
    name = f"tokenwise-check-{uuid.uuid4().hex[:12]}"
    script = expand(command, "/checks" if checks_dir else None, python="python")
    args = docker_args(settings, root, name, checks_dir)
    args += [settings.sandbox_image, "sh", "-c", script]
    try:
        done = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=settings.check_timeout,
        )  # fmt: skip
    except FileNotFoundError:
        return CheckResult(False, "docker is not installed; the check was not run", skipped=True)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=30)
        return CheckResult(False, f"check timed out after {settings.check_timeout} s")
    output = (done.stdout + done.stderr).strip()[-OUTPUT_TAIL:]
    unreachable = ("Cannot connect to the Docker daemon", "error during connect")
    if done.returncode == DOCKER_ERROR or any(u in done.stderr for u in unreachable):
        return CheckResult(False, f"docker could not run the check: {output}", skipped=True)
    if done.returncode == SKIP_EXIT:
        return CheckResult(False, output, skipped=True)
    return CheckResult(done.returncode == 0, output)


def run_check(
    settings: Settings, command: str, root: Path, checks_dir: Path | None = None
) -> CheckResult:
    """Runs `command` in `root` (already a throwaway copy) where `settings.sandbox` says."""
    if settings.sandbox == "docker":
        return run_in_docker(settings, command, root, checks_dir)
    return run_command(command, root, settings.check_timeout, checks_dir)


def copy_project(source: Path, target: Path) -> None:
    shutil.copytree(source, target, ignore=COPY_IGNORE)


def check_in_copy(
    settings: Settings, changes: list[Change], command: str, checks_dir: Path | None = None
) -> CheckResult:
    with tempfile.TemporaryDirectory(prefix="tokenwise-check-") as tmp:
        copy = Path(tmp) / settings.root.name
        copy_project(settings.root, copy)
        apply_changes(replace(settings, root=copy), changes)
        return run_check(settings, command, copy, checks_dir)


def make_verifier(settings: Settings, command: str) -> Callable[[list[Change]], CheckResult]:
    return lambda changes: check_in_copy(settings, changes, command)
