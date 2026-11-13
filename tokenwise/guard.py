"""Cheap checks on a proposed change, before anyone sees it.

Found in the first real runs with a small local model: an edit that leaves a Python file
unparsable, one that deletes a function the file still calls, and one that renames a setting
other modules import. None needs tests to notice, so every change goes through these checks and
a failure is sent back to the model like any other failed attempt.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .textio import read_source

MAX_PROBLEMS = 5


def _top_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _used_names(tree: ast.Module) -> set[str]:
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def _python_problems(path: str, old: str | None, new: str) -> tuple[list[str], set[str]]:
    """Problems inside the file, and top-level names the change removed."""
    old_tree = None
    if old is not None:
        try:
            old_tree = ast.parse(old)
        except SyntaxError:
            return [], set()  # it was already broken; do not blame the change
    try:
        new_tree = ast.parse(new)
    except SyntaxError as exc:
        line = (exc.text or "").strip()
        where = f"line {exc.lineno}" + (f" (`{line}`)" if line else "")
        return [f"{path}: your change makes the file invalid Python at {where}: {exc.msg}"], set()
    if old_tree is None:
        return [], set()
    removed = _top_names(old_tree) - _top_names(new_tree)
    still_used = sorted(removed & _used_names(new_tree))
    problems = [f"{path}: you removed `{name}`, but the file still uses it" for name in still_used]
    return problems, removed - set(still_used)


def problems(root: Path, changes: list, all_files: list[str]) -> list[str]:
    """Human-readable problems with `changes` (objects with path, old, new); [] if none."""
    found: list[str] = []
    removed_by_file: dict[str, set[str]] = {}
    changed = {c.path for c in changes}
    for change in changes:
        if not change.path.endswith(".py"):
            continue
        own, removed = _python_problems(change.path, change.old, change.new)
        found += own
        if removed:
            removed_by_file[change.path] = removed

    if removed_by_file:
        others = [f for f in all_files if f not in changed and f.endswith(".py")]
        new_text = {c.path: c.new for c in changes}
        for path, names in removed_by_file.items():
            module = Path(path).stem
            for name in sorted(names):
                pattern = re.compile(rf"\b{re.escape(name)}\b")
                for other in others:
                    text = read_source(root / other, strict=False)
                    if module in text and pattern.search(text):
                        found.append(f"{path}: you removed `{name}`, but {other} still uses it")
                        break
                else:
                    for other_changed, text in new_text.items():
                        if other_changed != path and pattern.search(text):
                            found.append(
                                f"{path}: you removed `{name}`, but {other_changed} still uses it"
                            )
                            break
    return found[:MAX_PROBLEMS]
