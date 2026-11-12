"""Asks the editor model for small edit blocks, turns them into file changes, applies and undoes."""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .llm import LLM, Reply
from .prompts import EDITOR_SYSTEM
from .selector import is_safe_path, normalize_path
from .textio import NotTextError, Style, read_source, read_style, write_source

ATTEMPTS_PER_MODEL = 2
MAX_LISTED_FILES = 200
UNDO_FILE = "last_change.json"

_EDIT_BLOCK = re.compile(
    r"^(?P<path>[^\n]+?)[ \t]*\n(?:```[^\n]*\n)?"
    r"<<<<<<< SEARCH\n(?P<search>.*?)^=======\n(?P<replace>.*?)^>>>>>>> REPLACE",
    re.S | re.M,
)
_NEED_FILES = re.compile(r"^NEED_FILES:\s*(.+)$", re.M)


class EditError(Exception):
    pass


@dataclass
class Edit:
    path: str
    search: str
    replace: str


@dataclass
class Change:
    path: str
    old: str | None  # None = new file
    new: str

    @property
    def diff(self) -> str:
        lines = difflib.unified_diff(
            (self.old or "").splitlines(keepends=True),
            self.new.splitlines(keepends=True),
            fromfile="/dev/null" if self.old is None else f"a/{self.path}",
            tofile=f"b/{self.path}",
        )
        return "".join(lines)

    @property
    def stats(self) -> tuple[int, int]:
        added = removed = 0
        for line in self.diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        return added, removed


@dataclass
class Proposal:
    changes: list[Change]
    replies: list[Reply] = field(default_factory=list)
    files_sent: list[str] = field(default_factory=list)
    model: str = ""
    error: str | None = None


def parse_edits(text: str) -> list[Edit]:
    edits = []
    for match in _EDIT_BLOCK.finditer(text):
        path = normalize_path(match.group("path"))
        edits.append(Edit(path, match.group("search"), match.group("replace")))
    return edits


def parse_need_files(text: str) -> list[str]:
    match = _NEED_FILES.search(text)
    if not match or "<<<<<<< SEARCH" in text:
        return []
    return [normalize_path(p) for p in match.group(1).split(",") if p.strip()]


def resolve(root: Path, rel: str) -> Path:
    if not is_safe_path(rel):
        raise EditError(f"{rel}: path must stay inside the project")
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()):
        raise EditError(f"{rel}: path must stay inside the project")
    return path


def _strip_blank_edges(text: str) -> str:
    lines = text.splitlines(keepends=True)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "".join(lines)


def plan_changes(root: Path, edits: list[Edit]) -> list[Change]:
    """Applies edits in memory. Raises EditError if any SEARCH part does not match exactly once."""
    originals: dict[str, str | None] = {}
    working: dict[str, str] = {}
    for edit in edits:
        if edit.path not in working:
            path = resolve(root, edit.path)
            try:
                originals[edit.path] = read_source(path) if path.exists() else None
            except NotTextError as exc:
                raise EditError(str(exc)) from None
            working[edit.path] = originals[edit.path] or ""
        text = working[edit.path]

        if not edit.search.strip():
            if text.strip():
                raise EditError(f"{edit.path}: file is not empty, SEARCH must not be empty")
            working[edit.path] = edit.replace
            continue

        search, replace = edit.search, edit.replace
        if text.count(search) == 0:
            search, replace = _strip_blank_edges(search), _strip_blank_edges(replace)
        count = text.count(search) if search else 0
        if count == 0:
            raise EditError(f"{edit.path}: SEARCH text not found")
        if count > 1:
            raise EditError(f"{edit.path}: SEARCH text matches {count} places, make it unique")
        working[edit.path] = text.replace(search, replace, 1)

    return [Change(p, originals[p], working[p]) for p in working if working[p] != originals[p]]


def build_prompt(
    request: str, paths: list[str], root: Path, all_files: list[str], feedback: str = ""
) -> str:
    parts = [f"Request: {request}", "", "Files:"]
    for rel in paths:
        path = root / rel
        if path.is_file():
            parts.append(f"{rel}\n```\n{read_source(path, strict=False)}\n```")
        else:
            parts.append(f"{rel}\n(does not exist yet)")
    others = [f for f in all_files if f not in paths][:MAX_LISTED_FILES]
    if others:
        parts += ["", "Other project files (ask with NEED_FILES if needed):", ", ".join(others)]
    if feedback:
        parts += ["", feedback]
    return "\n".join(parts)


def propose(
    request: str,
    paths: list[str],
    settings: Settings,
    llm: LLM,
    all_files: list[str],
    models: list[str] | None = None,
) -> Proposal:
    """Asks each model in `models` (cheapest first) for edits, climbing to the next on failure.

    Each model gets ATTEMPTS_PER_MODEL tries with feedback. A NEED_FILES reply adds the file
    and does not count as a failure.
    """
    models = models or list(settings.models)
    paths = list(dict.fromkeys(paths))
    replies: list[Reply] = []
    feedback = ""
    for model in models:
        attempts = 0
        while attempts < ATTEMPTS_PER_MODEL:
            prompt = build_prompt(request, paths, settings.root, all_files, feedback)
            reply = llm.complete(
                model=model,
                system=EDITOR_SYSTEM,
                prompt=prompt,
                max_tokens=settings.max_output_tokens,
            )
            reply.role = "editor"
            replies.append(reply)

            needed = [p for p in parse_need_files(reply.text) if p in all_files and p not in paths]
            if needed:
                paths += needed
                feedback = ""
                continue
            attempts += 1
            edits = parse_edits(reply.text)
            if not edits:
                feedback = "Your reply had no valid edit blocks. Reply only with edit blocks."
                continue
            try:
                return Proposal(plan_changes(settings.root, edits), replies, paths, model=model)
            except EditError as exc:
                feedback = (
                    f"Your previous edit could not be applied: {exc}. "
                    "Copy the SEARCH lines exactly from the files above."
                )
    tried = " → ".join(dict.fromkeys(models))
    return Proposal([], replies, paths, error=f"no applicable edit from {tried}")


def apply_changes(settings: Settings, changes: list[Change]) -> None:
    """Writes the changes, keeping each file's line endings and BOM; remembers how to undo."""
    paths = {c.path: resolve(settings.root, c.path) for c in changes}
    styles = {c.path: read_style(paths[c.path]) for c in changes if c.old is not None}
    # A new file follows the files changed with it (else LF), so a CRLF project stays CRLF.
    new_style = next(iter(styles.values()), Style())
    undo = {
        c.path: None if c.old is None else {"text": c.old, **styles[c.path].to_dict()}
        for c in changes
    }
    settings.state_dir.mkdir(exist_ok=True)
    (settings.state_dir / UNDO_FILE).write_text(json.dumps(undo, ensure_ascii=False), "utf-8")
    for change in changes:
        write_source(paths[change.path], change.new, styles.get(change.path, new_style))


def undo_last(settings: Settings) -> list[str]:
    undo_path = settings.state_dir / UNDO_FILE
    if not undo_path.exists():
        return []
    undo: dict[str, dict | str | None] = json.loads(undo_path.read_text("utf-8"))
    for rel, old in undo.items():
        path = resolve(settings.root, rel)
        if old is None:
            path.unlink(missing_ok=True)
        elif isinstance(old, str):  # written by tokenwise 1.0.0
            write_source(path, old, Style())
        else:
            style = Style(newline=old["newline"], bom=old["bom"])
            write_source(path, old["text"], style)
    undo_path.unlink()
    return list(undo)
