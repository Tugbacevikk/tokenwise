"""Finding files from what the request and the model actually name, without asking a model.

Seen in real runs with small local models: the selector picked the wrong file although the
request named the function (`save_config`, `number_to_words`); the selector invented a path
that does not exist (`core/isci_takip.py` for `isci_takip.py`); and the editor copied the
placeholder from its instructions (`path/to/predict.py`). Plain string matching fixes all three.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from .config import Settings
from .repo_map import outline

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?")
_DEFINED = re.compile(
    r"(?:def|class|function|fun|fn|func|interface|type|struct|enum|trait|record|object|const)\s+"
    r"(?:\([^)]*\)\s*)?(\w+)"
)
_METHOD = re.compile(r"^\s+(\w+)\(")
MIN_NAME = 4
MAX_FILES_PER_NAME = 3


def definitions(path: str, source: str) -> dict[str, str]:
    """Names defined in a file -> the symbol to focus on ("name" or "Class.method")."""
    found: dict[str, str] = {}
    owner = ""
    for line in outline(path, source):
        nested = line.startswith(" ")
        match = _DEFINED.search(line) or _METHOD.match(line)
        if not match:
            continue
        name = match.group(1)
        if not nested:
            owner = name if re.match(r"\s*(class|interface|struct|object|record)\b", line) else ""
            found.setdefault(name, name)
        elif owner:
            found.setdefault(name, f"{owner}.{name}")
    return found


def mentioned_targets(request: str, settings: Settings, files: list[str]) -> list[tuple[str, str]]:
    """(path, symbol) for code names in the request that are defined in at most a few files."""
    words = set()
    for token in _IDENTIFIER.findall(request):
        for part in [token, *token.split(".")]:
            if len(part) >= MIN_NAME:
                words.add(part)
    if not words:
        return []
    where: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for rel in files:
        source = (settings.root / rel).read_text("utf-8", errors="replace")
        for name, symbol in definitions(rel, source).items():
            if name in words or symbol in words:
                where[name].append((rel, symbol))
    targets = []
    for name in sorted(where, key=request.find):
        if len(where[name]) <= MAX_FILES_PER_NAME:
            targets += where[name]
    return list(dict.fromkeys(targets))


def resolve_path(path: str, known: list[str]) -> str:
    """A path that does not exist, matched to the one known file with the same name.

    `path/to/predict.py` -> `predict.py`; `core/isci_takip.py` -> `isci_takip.py`. If no file or
    several files share the name, the path is returned unchanged (it may be a new file)."""
    if path in known:
        return path
    name = Path(path).name
    same = [k for k in known if Path(k).name == name]
    if len(same) == 1:
        return same[0]
    tail = [k for k in same if path.endswith("/" + k) or k.endswith("/" + path)]
    return tail[0] if len(tail) == 1 else path


def merge_selection(
    chosen: list[str],
    focus: dict[str, list[str]],
    mentioned: list[tuple[str, str]],
    all_files: list[str],
    max_files: int,
) -> tuple[list[str], dict[str, list[str]]]:
    """The selector's choice with invented paths fixed, plus files defining named symbols
    (first, since the request names them). Named symbols become the focus of their file, which
    matters only for large files; the editor can still ask for the whole file (NEED_FILES)."""
    fixed = [resolve_path(p, all_files) for p in chosen]
    focus = {resolve_path(p, all_files): names for p, names in focus.items()}
    named_files = list(dict.fromkeys(p for p, _ in mentioned))
    for path, symbol in mentioned:  # only applied to large files; small ones go whole
        names = focus.setdefault(path, [])
        if symbol not in names:
            names.append(symbol)
    paths = list(dict.fromkeys(named_files + fixed))[:max_files]
    return paths, {p: n for p, n in focus.items() if p in paths}
