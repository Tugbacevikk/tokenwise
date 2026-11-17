"""Calibrating ReleaseGuard against the project's own past.

Every past commit is replayed as if it were a pull request: the six signals are computed with
the code as it was at that commit and the history git knew just before it. A commit is labelled
"needed a fix" when a fix commit within the next `window` commits touched one of its code files.
How well each signal separates the two groups is measured with AUC (0.5 = coin toss, 1 = perfect).

Reads git only; no model is called and the working tree is never touched."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

from .config import Settings
from .graph import build_graph
from .history import MAX_FILES_PER_COMMIT, Commit, History, git
from .release import WEIGHTS, ChangeInput, score_change, weights_path
from .repo_map import is_test_file

_RECORD, _UNIT = "\x1e", "\x1f"
MIN_POSITIVES = 10  # fewer labelled regressions than this and the weights are not suggested


class CalibrationError(Exception):
    pass


@dataclass
class Sample:
    sha: str
    subject: str
    regression: bool
    values: dict[str, float]
    score: int


@dataclass
class Calibration:
    commits: int  # commits read from history
    window: int
    samples: list[Sample] = field(default_factory=list)
    auc: dict[str, float] = field(default_factory=dict)  # per signal and "score"
    suggested: dict[str, float] = field(default_factory=dict)  # empty = not enough data
    suggested_auc: float | None = None
    seconds: float = 0.0

    @property
    def positives(self) -> int:
        return sum(s.regression for s in self.samples)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["positives"] = self.positives
        data.pop("samples")
        data["sample_count"] = len(self.samples)
        return data


class Blobs:
    """Reads file contents straight from git objects with one long-lived `git cat-file`."""

    def __init__(self, root: Path) -> None:
        self.proc = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.cache: dict[str, str] = {}

    def read(self, blob: str) -> str:
        if blob in self.cache:
            return self.cache[blob]
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(blob.encode() + b"\n")
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().decode().split()
        if len(header) < 3 or header[1] == "missing":
            raise KeyError(blob)
        data = self.proc.stdout.read(int(header[2]))
        self.proc.stdout.read(1)  # trailing newline
        text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
        self.cache[blob] = text
        return text

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait(timeout=10)


@dataclass
class _Commit:
    commit: Commit
    stats: dict[str, tuple[int, int]]


def read_log(root: Path, limit: int) -> list[_Commit]:
    """The last `limit` non-merge commits, oldest first, with per-file line counts."""
    fmt = f"{_RECORD}%H{_UNIT}%s{_UNIT}%b{_UNIT}"
    text = git(root, "log", "--no-merges", "--no-renames", f"-n{limit}", "--reverse",
               f"--format={fmt}", "--numstat")  # fmt: skip
    if text is None:
        raise CalibrationError("Bu klasör bir git deposu değil ya da geçmişi okunamadı.")
    result = []
    for record in text.split(_RECORD):
        if not record.strip():
            continue
        sha, subject, body, numstat = (record.split(_UNIT, 3) + ["", "", ""])[:4]
        stats: dict[str, tuple[int, int]] = {}
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                a, r, path = parts
                stats[path] = (int(a) if a.isdigit() else 0, int(r) if r.isdigit() else 0)
        commit = Commit(sha=sha.strip(), subject=subject.strip(), body=body.strip(),
                        files=sorted(stats))  # fmt: skip
        result.append(_Commit(commit, stats))
    return result


def _is_source(settings: Settings, path: str, size: int) -> bool:
    parts = path.split("/")
    return (
        path.endswith(settings.extensions)
        and not any(p in settings.ignore_dirs for p in parts[:-1])
        and size <= settings.max_file_bytes
    )


def tree_sources(settings: Settings, sha: str) -> dict[str, str]:
    """Source files at a commit: path -> blob id."""
    out = git(settings.root, "ls-tree", "-r", "-l", sha) or ""
    files = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 4 and parts[1] == "blob":
            size = int(parts[3]) if parts[3].isdigit() else 0
            if _is_source(settings, path, size):
                files[path] = parts[2]
    return files


def auc(values: Iterable[float], labels: Iterable[bool]) -> float | None:
    """Probability that a random regression scores above a random clean commit (ties = half)."""
    pairs = list(zip(values, labels, strict=True))
    pos = [v for v, label in pairs if label]
    neg = [v for v, label in pairs if not label]
    if not pos or not neg:
        return None
    ranked = sorted(pairs, key=lambda p: p[0])
    ranks: dict[int, float] = {}
    i = 0
    while i < len(ranked):
        j = i
        while j + 1 < len(ranked) and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        i = j + 1
    pos_rank_sum = sum(ranks[k] for k, (_, label) in enumerate(ranked) if label)
    u = pos_rank_sum - len(pos) * (len(pos) + 1) / 2
    return round(u / (len(pos) * len(neg)), 3)


def suggest_weights(signal_auc: dict[str, float | None]) -> dict[str, float]:
    """Weight each signal by how far it beats a coin toss; signals at or below 0.5 get none.
    Empty when no signal helps."""
    lift = {k: max(0.0, (signal_auc.get(k) or 0.5) - 0.5) for k in WEIGHTS}
    total = sum(lift.values())
    if total <= 0:
        return {}
    weights = {k: round(v / total, 2) for k, v in lift.items()}
    top = max(weights, key=lambda k: weights[k])
    weights[top] = round(weights[top] + 1 - sum(weights.values()), 2)
    return weights


def _add(history: History, commit: Commit) -> None:
    files = sorted(set(commit.files))
    history.commits.append(commit)
    history.churn.update(files)
    if commit.is_fix:
        history.fixes.update(files)
    if len(files) <= MAX_FILES_PER_COMMIT:
        history.pairs.update(combinations(files, 2))


def calibrate(
    settings: Settings, sample: int = 200, window: int = 20, limit: int = 3000
) -> Calibration:
    started = time.monotonic()
    log = read_log(settings.root, limit)
    if len(log) <= window + 10:
        raise CalibrationError(
            f"Geçmiş çok kısa ({len(log)} commit); en az {window + 11} commit gerekiyor."
        )

    def code_files(entry: _Commit) -> set[str]:
        return {
            p for p in entry.stats
            if p.endswith(settings.extensions) and not is_test_file(p)
        }  # fmt: skip

    eligible = [i for i in range(len(log) - window) if code_files(log[i])]
    chosen = set(eligible[-sample:])
    result = Calibration(commits=len(log), window=window)
    history = History()
    blobs = Blobs(settings.root)
    try:
        for i, entry in enumerate(log):
            if i in chosen:
                mine = code_files(entry)
                regression = any(
                    later.commit.is_fix and mine & set(later.stats)
                    for later in log[i + 1 : i + 1 + window]
                )
                tree = tree_sources(settings, entry.commit.sha)
                files = sorted(tree)
                graph = build_graph(settings, files, read=lambda p, t=tree: blobs.read(t[p]))
                change = ChangeInput(
                    stats=entry.stats,
                    sources=set(files),
                    graph=graph,
                    history=history,
                    read=lambda p, t=tree: blobs.read(t[p]),
                    commits=[entry.commit],
                )
                report = score_change(change, entry.commit.sha[:12], WEIGHTS)
                result.samples.append(
                    Sample(
                        sha=entry.commit.sha[:12],
                        subject=entry.commit.subject,
                        regression=regression,
                        values={s.name: round(s.value, 4) for s in report.signals},
                        score=report.score,
                    )
                )
            _add(history, entry.commit)
    finally:
        blobs.close()

    labels = [s.regression for s in result.samples]
    for name in WEIGHTS:
        result.auc[name] = auc([s.values[name] for s in result.samples], labels)
    result.auc["score"] = auc([s.score for s in result.samples], labels)
    if result.positives >= MIN_POSITIVES and len(result.samples) - result.positives > 0:
        result.suggested = suggest_weights(result.auc)
        if result.suggested:
            combined = [sum(result.suggested[k] * s.values[k] for k in WEIGHTS)
                        for s in result.samples]  # fmt: skip
            result.suggested_auc = auc(combined, labels)
    result.seconds = round(time.monotonic() - started, 1)
    return result


def save_weights(settings: Settings, result: Calibration) -> Path:
    if not result.suggested:
        raise CalibrationError("Kaydedilecek ağırlık yok: yeterli veri ya da faydalı sinyal yok.")
    path = weights_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "weights": result.suggested,
        "auc": result.auc,
        "suggested_auc": result.suggested_auc,
        "samples": len(result.samples),
        "positives": result.positives,
        "window": result.window,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(data, indent=2), "utf-8")
    return path
