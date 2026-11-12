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
    # class methods: indented `name(args) {` with optional modifiers and return type
    (
        re.compile(
            r"^[ \t]+(?:(?:public|private|protected|static|async|readonly|override|get|set)[ \t]+)*"
            r"(?!(?:if|for|while|switch|catch|return|function|constructor)\b)(\w+)[ \t]*"
            r"\(([^)]*)\)(?:[ \t]*:[^{;=\n]+)?[ \t]*\{",
            re.M,
        ),
        "  {0}({1})",
    ),
]

_MOD = (
    r"(?:(?:public|private|protected|internal|static|final|abstract|sealed|partial|open|data"
    r"|override|virtual|async|synchronized|suspend|inline|readonly)[ \t]+)"
)
_TYPE_DECL = (
    re.compile(rf"^[ \t]*{_MOD}*(class|interface|enum|record|struct|object)[ \t]+(\w+)", re.M),
    "{0} {1}",
)
_LANG_PATTERNS = {
    # Java and C#: a method needs at least one modifier, which keeps out calls and statements
    (".java", ".cs"): [
        _TYPE_DECL,
        (
            re.compile(rf"^[ \t]+{_MOD}+[\w<>\[\],.? ]+?[ \t]+(\w+)[ \t]*\(([^)]*)\)", re.M),
            "  {0}({1})",
        ),
    ],
    (".kt", ".kts"): [
        _TYPE_DECL,
        (
            re.compile(
                rf"^[ \t]*{_MOD}*fun[ \t]+(?:<[^>]*>[ \t]*)?(?:[\w.<>?, ]+\.)?"
                r"(\w+)[ \t]*\(([^)]*)\)",
                re.M,
            ),
            "fun {0}({1})",
        ),
    ],
    (".go",): [
        (
            re.compile(r"^func[ \t]+(?:\(([^)]*)\)[ \t]*)?(\w+)[ \t]*\(([^)]*)\)", re.M),
            "func ({0}) {1}({2})",
        ),
        (re.compile(r"^type[ \t]+(\w+)[ \t]+(struct|interface)", re.M), "type {0} {1}"),
    ],
    (".rs",): [
        (
            re.compile(
                r"^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?(?:async[ \t]+)?(?:unsafe[ \t]+)?"
                r"fn[ \t]+(\w+)[ \t]*(?:<[^>]*>)?\(([^)]*)\)",
                re.M,
            ),
            "fn {0}({1})",
        ),
        (
            re.compile(r"^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?(struct|enum|trait)[ \t]+(\w+)", re.M),
            "{0} {1}",
        ),
        (re.compile(r"^[ \t]*impl(?:<[^>]*>)?[ \t]+([^{\n]+?)[ \t]*\{", re.M), "impl {0}"),
    ],
    (".php",): [
        _TYPE_DECL,
        (
            re.compile(
                r"^[ \t]*(?:(?:public|private|protected|static|abstract|final)[ \t]+)*"
                r"function[ \t]+(\w+)[ \t]*\(([^)]*)\)",
                re.M,
            ),
            "function {0}({1})",
        ),
    ],
}


def _outline_with(patterns: list, source: str) -> list[str]:
    found = {}
    for pattern, template in patterns:
        for match in pattern.finditer(source):
            groups = [" ".join((g or "").split()) for g in match.groups()]
            text = template.format(*groups).replace("func () ", "func ")
            found.setdefault(match.start(), text)
    return [text for _, text in sorted(found.items())]


def outline_js(source: str) -> list[str]:
    return _outline_with(_JS_PATTERNS, source)


def outline(path: str, source: str) -> list[str]:
    if path.endswith(".py"):
        return outline_python(source)
    for extensions, patterns in _LANG_PATTERNS.items():
        if path.endswith(extensions):
            return _outline_with(patterns, source)
    return outline_js(source)


def build_map(settings: Settings, files: list[str]) -> str:
    blocks = []
    for rel in files:
        source = (settings.root / rel).read_text("utf-8", errors="replace")
        symbols = outline(rel, source)
        lines = source.count("\n") + 1
        title = f"{rel} [large: {lines} lines]" if lines > settings.focus_lines else rel
        blocks.append("\n".join([title, *("  " + s for s in symbols)]))
    return "\n".join(blocks)


def estimate_tokens(text: str) -> int:
    """Rough estimate (about 4 characters per token); good enough for comparisons."""
    return max(1, len(text) // 4) if text else 0


def source_tokens(settings: Settings, files: list[str]) -> int:
    total = sum((settings.root / rel).stat().st_size for rel in files)
    return total // 4
