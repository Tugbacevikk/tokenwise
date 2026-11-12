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


def parse_paths(text: str) -> list[str]:
    match = _ARRAY.search(text)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    paths = [normalize_path(item) for item in items if isinstance(item, str)]
    return list(dict.fromkeys(p for p in paths if is_safe_path(p)))


def select_files(
    request: str, repo_map: str, settings: Settings, llm: LLM
) -> tuple[list[str], Reply]:
    system = SELECTOR_SYSTEM.format(max_files=settings.max_files, repo_map=repo_map)
    reply = llm.complete(
        model=settings.selector_model, system=system, prompt=request, max_tokens=400
    )
    reply.role = "selector"
    return parse_paths(reply.text)[: settings.max_files], reply
