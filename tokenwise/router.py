"""Guesses how hard a request is and picks the rung of the model ladder to start from."""

from __future__ import annotations

import re
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
    "cache",
    "önbellek",
    "database",
    "veritabanı",
    "schema",
    "protocol",
    "protokol",
)
_BIG_CHANGE_LINES = 600


def _has(words: tuple[str, ...], text: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", text) for w in words)


def difficulty(request: str, root: Path, paths: list[str]) -> int:
    """0 = easy, 1 = medium, 2 = hard. Rules only; no model call, no cost."""
    text = request.lower()
    score = MEDIUM
    if _has(_HARD_WORDS, text):
        score = HARD
    elif _has(_EASY_WORDS, text):
        score = EASY

    lines = 0
    for rel in paths:
        path = root / rel
        if path.is_file():
            lines += path.read_text("utf-8", errors="replace").count("\n")
    if len(paths) >= 4 or lines > _BIG_CHANGE_LINES:
        score += 1
    return min(score, HARD)


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
