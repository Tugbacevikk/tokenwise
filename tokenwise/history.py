"""What git remembers about each file: how often it changes, how often a fix touched it and which
files usually change with it. Read once from `git log`; nothing here calls a model."""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

FIX_WORDS = re.compile(
    r"\b(fix|fixes|fixed|bug|hotfix|regression|revert|crash|hata|düzelt\w*|onar\w*)\b", re.I
)
AI_TRAILER = re.compile(
    r"co-authored-by:.*(claude|copilot|cursor|codex|gemini|devin|aider|tokenwise|\[bot\])", re.I
)
_RECORD, _UNIT = "\x1e", "\x1f"
MAX_FILES_PER_COMMIT = 30  # bigger commits (renames, formatting) say little about coupling


@dataclass
class Commit:
    sha: str
    subject: str
    body: str
    files: list[str]

    @property
    def is_fix(self) -> bool:
        return bool(FIX_WORDS.search(self.subject))

    @property
    def ai_assisted(self) -> bool:
        return bool(AI_TRAILER.search(self.body))


@dataclass
class History:
    commits: list[Commit] = field(default_factory=list)
    churn: Counter = field(default_factory=Counter)  # file -> commits touching it
    fixes: Counter = field(default_factory=Counter)  # file -> fix commits touching it
    pairs: Counter = field(default_factory=Counter)  # (a, b) sorted -> commits touching both

    def co_changed(self, files: list[str], min_count: int = 2) -> list[tuple[str, str, int]]:
        """Files outside `files` that changed together with one of them at least `min_count`
        times, as (outside file, file in the set, times), most coupled first."""
        inside = set(files)
        found: dict[str, tuple[str, int]] = {}
        for (a, b), count in self.pairs.items():
            if count < min_count:
                continue
            for mine, other in ((a, b), (b, a)):
                if mine in inside and other not in inside:
                    if count > found.get(other, ("", 0))[1]:
                        found[other] = (mine, count)
        ranked = sorted(found.items(), key=lambda item: (-item[1][1], item[0]))
        return [(other, mine, count) for other, (mine, count) in ranked]


def git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=120
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def parse_log(text: str) -> list[Commit]:
    commits = []
    for record in text.split(_RECORD):
        if not record.strip():
            continue
        head, _, rest = record.partition(_UNIT + _UNIT)
        sha, _, subject = head.strip().partition(_UNIT)
        body, _, names = rest.partition(_UNIT)
        files = [line.strip() for line in names.splitlines() if line.strip()]
        commits.append(Commit(sha=sha, subject=subject.strip(), body=body.strip(), files=files))
    return commits


def load_history(root: Path, limit: int = 1000, revision: str = "HEAD") -> History:
    """The last `limit` non-merge commits. An empty history outside git."""
    fmt = f"{_RECORD}%H{_UNIT}%s{_UNIT}{_UNIT}%b{_UNIT}"
    text = git(root, "log", revision, "--no-merges", f"-n{limit}", f"--format={fmt}", "--name-only")
    history = History()
    if not text:
        return history
    history.commits = parse_log(text)
    for commit in history.commits:
        files = sorted(set(commit.files))
        history.churn.update(files)
        if commit.is_fix:
            history.fixes.update(files)
        if len(files) <= MAX_FILES_PER_COMMIT:
            history.pairs.update(combinations(files, 2))
    return history


def head_commit(root: Path) -> str:
    return (git(root, "rev-parse", "--short", "HEAD") or "").strip()
