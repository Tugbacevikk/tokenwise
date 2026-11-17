"""Writing a proposal into the project, committing it, and the weekly budget."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..checker import check_in_copy
from ..config import Settings
from ..editor import Change, apply_changes, resolve
from ..ledger import Ledger, ledger_path
from ..pricing import format_usd
from ..textio import read_source
from . import ServiceError

SPEND_METHODS = ("tokenwise", "plan", "chat")  # ledger methods the web page counts as spending


@dataclass
class Budget:
    spent: float
    limit: float

    @property
    def used_up(self) -> bool:
        return bool(self.limit) and self.spent >= self.limit

    def message(self) -> str:
        return f"Haftalık bütçe doldu: {format_usd(self.spent)} / {format_usd(self.limit)}."


def budget(settings: Settings, methods: tuple[str, ...] | None = SPEND_METHODS) -> Budget:
    """Spending of the last 7 days against `budget_usd_week`. `methods=None` counts every
    ledger entry (the CLI's view)."""
    ledger = Ledger(ledger_path(settings))
    try:
        if methods is None:
            spent = ledger.summary(7)["cost_usd"]
        else:
            spent = sum(ledger.summary(7, method=m)["cost_usd"] for m in methods)
    finally:
        ledger.close()
    return Budget(spent, settings.budget_usd_week)


def require_budget(settings: Settings, allow_over: bool = False, hint: str = "") -> Budget:
    state = budget(settings)
    if state.used_up and not allow_over:
        raise ServiceError(409, state.message() + (f" {hint}" if hint else ""))
    return state


def stale_paths(root: Path, changes: list[Change]) -> list[str]:
    """Files that are no longer what the proposal was made from."""
    stale = []
    for change in changes:
        path = resolve(root, change.path)
        if change.old is None:
            changed = path.exists()
        else:
            changed = not path.exists() or read_source(path, strict=False) != change.old
        if changed:
            stale.append(change.path)
    return stale


def apply_proposal(settings: Settings, run_id: int, changes: list[Change]) -> dict:
    """Writes a proposal, marks it applied in the ledger and runs the project's check on the
    result. Refuses if a file changed since the proposal was made."""
    stale = stale_paths(settings.root, changes)
    if stale:
        raise ServiceError(409, f"{stale[0]} değişmiş. İsteği yeniden çalıştır.")
    apply_changes(settings, changes)
    ledger = Ledger(ledger_path(settings))
    try:
        ledger.mark_applied(run_id)
    finally:
        ledger.close()
    check = None
    if settings.check:  # the project's tests, now that the change is in place
        outcome = check_in_copy(settings, [], settings.check)
        check = {"passed": outcome.passed, "skipped": outcome.skipped,
                 "output": outcome.output[-2000:]}  # fmt: skip
    return {"applied": [c.path for c in changes], "check": check}


def is_git_repo(root: Path) -> bool:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
    except OSError:  # git is not installed
        return False
    return done.returncode == 0


def commit_paths(root: Path, request: str, paths: list[str]) -> dict:
    """Commits only the files tokenwise changed, nothing else that happens to be staged."""
    if not is_git_repo(root):
        raise ServiceError(400, "Proje bir git deposu değil.")
    message = "tokenwise: " + " ".join(request.split())[:72]
    for args in (["add", "--", *paths], ["commit", "-m", message, "--", *paths]):
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        if done.returncode:
            raise ServiceError(400, (done.stderr or done.stdout).strip())
    sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    return {"commit": sha, "message": message}
