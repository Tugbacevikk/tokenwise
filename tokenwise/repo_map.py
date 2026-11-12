"""Project map: file paths plus function and class signatures, without their bodies."""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

from .config import Settings


def list_source_files(settings: Settings) -> list[str]:
    """Source files as root-relative POSIX paths. Respects .gitignore inside a git repo."""
    candidates = _git_files(settings.root)
    if candidates is None:
        candidates = _walk_files(settings.root, settings.ignore_dirs)

    result = []
    for rel in candidates:
        parts = rel.split("/")
        if any(part in settings.ignore_dirs for part in parts[:-1]):
            continue
        if not rel.endswith(settings.extensions):
            continue
        path = settings.root / rel
        if path.is_file() and path.stat().st_size <= settings.max_file_bytes:
            result.append(rel)
    return sorted(result)


def _git_files(root: Path) -> list[str] | None:
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [line for line in out.splitlines() if line]


def _walk_files(root: Path, ignore_dirs: tuple[str, ...]) -> list[str]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
        for name in filenames:
            files.append(Path(dirpath, name).relative_to(root).as_posix())
    return files


def outline_python(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ["(could not parse)"]

    lines: list[str] = []

    def visit(nodes: list[ast.stmt], depth: int) -> None:
        pad = "  " * depth
        for node in nodes:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
                lines.append(f"{pad}{kind} {node.name}({ast.unparse(node.args)}){returns}")
            elif isinstance(node, ast.ClassDef):
                bases = ", ".join(ast.unparse(b) for b in node.bases)
                lines.append(f"{pad}class {node.name}" + (f"({bases})" if bases else ""))
                visit(node.body, depth + 1)

    visit(tree.body, 0)
    return lines


_PREFIX = r"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?"
_JS_PATTERNS = [
    (
        re.compile(_PREFIX + r"(?:async[ \t]+)?function\s*\*?\s*(\w+)\s*\(([^)]*)\)", re.M),
        "function {0}({1})",
    ),
    (re.compile(_PREFIX + r"(?:abstract[ \t]+)?class[ \t]+(\w+)", re.M), "class {0}"),
    (
        re.compile(
            _PREFIX
            + r"(?:const|let|var)[ \t]+(\w+)[^=\n]*=[ \t]*(?:async[ \t]*)?\(([^)]*)\)[^=\n]*=>",
            re.M,
        ),
        "const {0} = ({1}) =>",
    ),
    (re.compile(_PREFIX + r"(interface|type)[ \t]+(\w+)", re.M), "{0} {1}"),
]


def outline_js(source: str) -> list[str]:
    found = []
    for pattern, template in _JS_PATTERNS:
        for match in pattern.finditer(source):
            groups = [" ".join(g.split()) for g in match.groups()]
            found.append((match.start(), template.format(*groups)))
    return [text for _, text in sorted(found)]


def outline(path: str, source: str) -> list[str]:
    if path.endswith(".py"):
        return outline_python(source)
    return outline_js(source)


def build_map(settings: Settings, files: list[str]) -> str:
    blocks = []
    for rel in files:
        source = (settings.root / rel).read_text("utf-8", errors="replace")
        symbols = outline(rel, source)
        blocks.append("\n".join([rel, *("  " + s for s in symbols)]))
    return "\n".join(blocks)


def estimate_tokens(text: str) -> int:
    """Rough estimate (about 4 characters per token); good enough for comparisons."""
    return max(1, len(text) // 4) if text else 0


def source_tokens(settings: Settings, files: list[str]) -> int:
    total = sum((settings.root / rel).stat().st_size for rel in files)
    return total // 4
