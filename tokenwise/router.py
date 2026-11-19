"""Guesses how hard a request is and picks the rung of the model ladder to start from."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

EASY = 0
MEDIUM = 1
HARD = 2
NAMES = {EASY: "easy", MEDIUM: "medium", HARD: "hard"}

# Words that usually mean a small, local change (Turkish and English).
_EASY_WORDS = (
    "typo",
    "yazım",
    "rename",
    "yeniden adlandır",
    "comment",
    "yorum",
    "docstring",
    "log",
    "print",
    "readme",
    "default",
    "varsayılan",
    "message",
    "mesaj",
    "import",
    "constant",
    "sabit",
)
# Words that usually mean a change touching design, many places or tricky behaviour.
# Only words about the kind of change: a topic noun such as "cache", "database" or "schema"
# says what the code is about, not how hard the change is ("add a --json option to the cache
# command" is easy), so those are left out.
_HARD_WORDS = (
    "refactor",
    "yeniden düzenle",
    "architecture",
    "mimari",
    "rewrite",
    "baştan yaz",
    "migrate",
    "taşı",
    "redesign",
    "performance",
    "performans",
    "concurrency",
    "concurrent",
    "thread",
    "async",
    "race",
    "security",
    "güvenlik",
)
_BIG_CHANGE_LINES = 600


def _has(words: tuple[str, ...], text: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", text) for w in words)


@dataclass
class Signal:
    """One reason the router moved the starting rung: `points` up (+) or down (-)."""

    name: str  # words, size, files, complexity, reach, history
    value: str  # what was measured, e.g. "18" or "4/6"
    points: int


@dataclass
class Route:
    level: int
    signals: list[Signal] = field(default_factory=list)

    @property
    def name(self) -> str:
        return NAMES[self.level]

    def describe(self) -> str:
        """For the terminal: why this rung (English, like the rest of the CLI)."""
        words = {
            "words": "the request's wording",
            "size": "lines the editor sees",
            "files": "files changed",
            "complexity": "complexity of the target code",
            "reach": "files that import it",
            "history": "escalations at this level here",
        }
        parts = [f"{words[s.name]} {s.value} ({s.points:+d})" for s in self.signals if s.points]
        return "; ".join(parts) or "nothing unusual"

    def to_dict(self) -> list[dict]:
        return [{"signal": s.name, "value": s.value, "points": s.points} for s in self.signals]


# Starting rung from the summed points: ≤ -1 easy, 0 medium, ≥ 1 hard. A request with no
# signal starts in the middle, as before; one signal moves it one rung.
COMPLEX_FUNCTION = 15  # a target function this tangled needs a stronger model (+1)
WIDE_REACH = 5  # this many other files import the target: a mistake spreads (+1)
HISTORY_MIN_RUNS = 5  # escalation history counts only from this many runs at a level
HISTORY_ESCALATION_SHARE = 0.5  # ...and only when at least this share had to escalate


def _words(text: str) -> Signal:
    if _has(_HARD_WORDS, text):
        return Signal("words", "hard", 1)
    if _has(_EASY_WORDS, text):
        return Signal("words", "easy", -1)
    return Signal("words", "neutral", 0)


def _seen_lines(root: Path, paths: list[str], focus: dict[str, list[str]] | None) -> int:
    """The lines the editor will actually see: for a focused file, only its view."""
    from .focus import focused_view

    lines = 0
    for rel in paths:
        path = root / rel
        if not path.is_file():
            continue
        source = path.read_text("utf-8", errors="replace")
        view = focused_view(rel, source, focus[rel]) if focus and rel in focus else None
        lines += (view or source).count("\n")
    return lines


def _size(root: Path, paths: list[str], focus: dict[str, list[str]] | None) -> list[Signal]:
    lines = _seen_lines(root, paths, focus)
    big = len(paths) >= 4 or lines > _BIG_CHANGE_LINES
    if lines > _BIG_CHANGE_LINES:
        return [Signal("size", str(lines), 1)]
    return [Signal("files", str(len(paths)), 1 if big else 0)]


def target_complexity(root: Path, paths: list[str], focus: dict[str, list[str]] | None) -> int:
    """The most tangled function the change is about: the focused functions when the
    selector named them, else every function in the chosen files. 0 when none is known."""
    from .analyzers import _functions, complexity
    from .callgraph import js_function_sizes

    worst = 0
    for rel in paths:
        path = root / rel
        if not path.is_file():
            continue
        source = path.read_text("utf-8", errors="replace")
        wanted = set((focus or {}).get(rel, ()))
        if rel.endswith(".py"):
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            found = [(name, complexity(node)) for name, node in _functions(tree)]
        else:
            found = [(d.name, score) for d, score in js_function_sizes(rel, source)]
        for name, score in found:
            if not wanted or name in wanted or name.rsplit(".", 1)[-1] in wanted:
                worst = max(worst, score)
    return worst


def _complexity(root: Path, paths: list[str], focus: dict[str, list[str]] | None) -> Signal:
    worst = target_complexity(root, paths, focus)
    return Signal("complexity", str(worst), 1 if worst >= COMPLEX_FUNCTION else 0)


def _reach(graph, paths: list[str]) -> Signal:
    users = set()
    for rel in paths:
        users |= {u for u in graph.rdeps.get(rel, ()) if u not in paths}
    return Signal("reach", str(len(users)), 1 if len(users) >= WIDE_REACH else 0)


HISTORY_DAYS = 30  # older runs are forgotten, so a raised start is tried lower again later


def escalation_history(settings, level: int, limit: int = 20) -> tuple[int, int]:
    """(runs that had to climb, runs) among this project's recent requests that started at
    `level`: the model that finished was not the first one tried."""
    import sqlite3
    from datetime import UTC, datetime, timedelta

    from .ledger import ledger_path

    path = ledger_path(settings)
    if not path.exists():
        return 0, 0
    first = ladder(settings.models, level)[0]
    since = (datetime.now(UTC) - timedelta(days=HISTORY_DAYS)).isoformat(timespec="seconds")
    try:
        with sqlite3.connect(path) as db:
            rows = db.execute(
                "SELECT model FROM runs WHERE method = 'tokenwise' AND difficulty = ? AND ok = 1"
                " AND ts >= ? ORDER BY id DESC LIMIT ?",
                (NAMES[level], since, limit),
            ).fetchall()
    except sqlite3.Error:
        return 0, 0
    return sum(model != first for (model,) in rows), len(rows)


def _level(points: int) -> int:
    return EASY if points <= -1 else MEDIUM if points == 0 else HARD


def route(request: str, settings, paths: list[str], focus: dict[str, list[str]] | None = None,
          graph=None, history: bool = True) -> Route:  # fmt: skip
    """The starting rung and why. No model call, no cost.

    Signals: the request's wording; how much code the editor sees and how many files; how
    tangled the target functions are; how many other files import them (`graph`, optional);
    and this project's own record: when most recent requests that started at a level had to
    climb to a stronger model anyway, start one rung higher and skip the wasted attempts."""
    root = settings.root
    signals = [_words(request.lower()), *_size(root, paths, focus)]
    if getattr(settings, "router", "smart") == "simple":
        return Route(_level(sum(s.points for s in signals)), signals)
    signals.append(_complexity(root, paths, focus))
    if graph is not None:
        signals.append(_reach(graph, paths))
    level = _level(sum(s.points for s in signals))
    if history and level < HARD:
        climbed, runs = escalation_history(settings, level)
        if runs >= HISTORY_MIN_RUNS and climbed / runs >= HISTORY_ESCALATION_SHARE:
            signals.append(Signal("history", f"{climbed}/{runs}", 1))
            level += 1
    return Route(level, signals)


def difficulty(
    request: str, root: Path, paths: list[str], focus: dict[str, list[str]] | None = None
) -> int:
    """0 = easy, 1 = medium, 2 = hard, from the request and the code alone (no history)."""
    from .config import Settings

    return route(request, Settings(root=root, router="simple"), paths, focus).level


def ladder(models: tuple[str, ...], level: int) -> list[str]:
    """Models to try in order, starting at the rung for `level` and climbing to the strongest.

    A ladder shorter than three rungs is stretched: with one model every level uses it,
    with two the easy level starts at the first and the others at the second.
    """
    if not models:
        raise ValueError("models must not be empty")
    rung = min(level, len(models) - 1)
    if len(models) == 2 and level >= MEDIUM:
        rung = 1
    return list(models[rung:])
