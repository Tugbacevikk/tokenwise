"""Focused view of a large file: its header, the functions/classes the request needs, and a
list of everything else, instead of the whole file.

The shown parts are copied verbatim, so SEARCH text taken from them still matches the real
file. Gaps are marked with a "⋯" line the model is told never to copy. When a symbol cannot be
found, or the view would not be much smaller, the caller sends the whole file instead.
"""

from __future__ import annotations

import ast
import re

from .repo_map import outline

GAP = "⋯"
HEADER_MAX = 40
WORTH_IT = 0.7  # a focused view must be under 70% of the file, else send it all
_DECL = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:public|private|protected|internal|static|async|abstract"
    r"|final|override|virtual|pub(?:\(\w+\))?|open|suspend|inline|\s)*"
    r"(?:function\*?|class|interface|struct|enum|trait|impl|type|const|let|var|def|fn|func"
    r"|fun|record|object|module|namespace)?\b"
)


def _python_ranges(source: str, names: list[str]) -> list[tuple[int, int]] | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    found: dict[str, list[tuple[int, int]]] = {}

    def visit(nodes: list[ast.stmt], prefix: str) -> None:
        for node in nodes:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                span = (start, node.end_lineno or node.lineno)
                found.setdefault(prefix + node.name, []).append(span)
                found.setdefault(node.name, []).append(span)
                if isinstance(node, ast.ClassDef):
                    visit(node.body, f"{prefix}{node.name}.")

    visit(tree.body, "")
    ranges = []
    for name in names:
        spans = found.get(name)
        if not spans or (len(spans) > 1 and "." not in name and name not in _top_level(tree)):
            return None
        ranges.append(spans[0])
    return ranges


def _top_level(tree: ast.Module) -> set[str]:
    return {
        n.name
        for n in tree.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }


def _python_header_end(source: str) -> int:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            first = min([d.lineno for d in node.decorator_list] + [node.lineno])
            return min(first - 1, HEADER_MAX)
    return min(len(source.splitlines()), HEADER_MAX)


_HEADER_LINE = re.compile(
    r"^\s*(?:$|import\b|from\b|using\b|package\b|#include|#pragma|require\(|//|/\*|\*"
    r"|['\"]use |const \w+ = require\(|@file)"
)


def _brace_header_end(lines: list[str]) -> int:
    """Leading imports, package lines, blank lines and comments."""
    end = 0
    for i, line in enumerate(lines[:HEADER_MAX]):
        if not _HEADER_LINE.match(line):
            break
        end = i + 1
    return end


def _brace_range(lines: list[str], name: str) -> tuple[int, int] | None:
    """Declaration line of `name` and the line where its { ... } block closes (1-based)."""
    short = name.split(".")[-1]
    pattern = re.compile(rf"\b{re.escape(short)}\b\s*(?:[=(<:{{]|$|\s+extends|\s+implements)")
    for i, line in enumerate(lines):
        if not pattern.search(line) or not _DECL.match(line):
            continue
        if line.lstrip().startswith(("//", "*", "/*", "#", "return ", "if ", "} ")):
            continue
        depth, opened = 0, False
        for j in range(i, len(lines)):
            code = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`[^`]*`|//.*$', "", lines[j])
            depth += code.count("{") - code.count("}")
            opened = opened or "{" in code
            if not opened and j - i >= 3:
                break
            if opened and depth <= 0:
                return i + 1, j + 1
            if not opened and code.rstrip().endswith(";"):
                return i + 1, j + 1  # one-line declaration, e.g. a type alias or arrow function
        return None
    return None


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 2:  # bridge gaps of one line
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def focused_view(path: str, source: str, names: list[str]) -> str | None:
    """The focused text for `path`, or None when the whole file should be sent."""
    lines = source.splitlines(keepends=True)
    if not names or not lines:
        return None
    if path.endswith(".py"):
        ranges = _python_ranges(source, names)
        header = _python_header_end(source)
    else:
        bare = [line.rstrip("\n") for line in lines]
        found = [_brace_range(bare, n) for n in names]
        ranges = None if any(r is None for r in found) else [r for r in found if r]
        header = _brace_header_end(bare)
    if not ranges:
        return None
    if header > 0:
        ranges.append((1, header))
    merged = _merge(ranges)
    shown = sum(end - start + 1 for start, end in merged)
    if shown >= WORTH_IT * len(lines):
        return None

    out, cursor = [], 1
    for start, end in merged:
        if start > cursor:
            out.append(f"{GAP} {start - cursor} lines omitted\n")
        out.extend(lines[start - 1 : end])
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        cursor = end + 1
    if cursor <= len(lines):
        out.append(f"{GAP} {len(lines) - cursor + 1} lines omitted\n")
    others = [s.strip() for s in outline(path, source)]
    return "".join(out) + "Everything defined in this file: " + "; ".join(others)
