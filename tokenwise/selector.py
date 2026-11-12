"""Asks a cheap model which files the request needs, using only the project map."""

from __future__ import annotations

import json
import re

from .config import Settings
from .llm import LLM, Reply
from .prompts import SELECTOR_SYSTEM

_ARRAY = re.compile(r"\[.*?\]", re.S)


def normalize_path(path: str) -> str:
    path = path.strip().strip("`'\"").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def is_safe_path(path: str) -> bool:
    return bool(path) and not path.startswith("/") and ".." not in path.split("/")


def parse_targets(text: str) -> tuple[list[str], dict[str, list[str]]]:
    """File paths in order, and for files named as "path::symbol" the symbols asked for.

    A file named both bare and with symbols is sent whole (no focus entry)."""
    match = _ARRAY.search(text)
    if not match:
        return [], {}
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        return [], {}
    paths: list[str] = []
    focus: dict[str, list[str]] = {}
    whole: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        raw, _, symbol = item.partition("::")
        path = normalize_path(raw)
        if not is_safe_path(path):
            continue
        if path not in paths:
            paths.append(path)
        if symbol.strip():
            focus.setdefault(path, []).append(symbol.strip())
        else:
            whole.add(path)
    return paths, {p: names for p, names in focus.items() if p not in whole}


def parse_paths(text: str) -> list[str]:
    return parse_targets(text)[0]


def select_files(
    request: str, repo_map: str, settings: Settings, llm: LLM
) -> tuple[list[str], Reply, dict[str, list[str]]]:
    system = SELECTOR_SYSTEM.format(max_files=settings.max_files, repo_map=repo_map)
    reply = llm.complete(
        model=settings.selector_model, system=system, prompt=request, max_tokens=400
    )
    reply.role = "selector"
    paths, focus = parse_targets(reply.text)
    paths = paths[: settings.max_files]
    return paths, reply, {p: focus[p] for p in paths if p in focus}
