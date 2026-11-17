"""Function-level impact: which functions a change touched, and which functions and tests call
them. Python is read with `ast`; JavaScript and TypeScript with Tree-sitter when it is installed
(`pip install tokenwise-coder[impact]`), otherwise those files are left out of this view.

Calls are matched by name among the files that import the changed file (and the file itself),
so `client.get()` in an unrelated module is not mistaken for a caller. Nothing here calls a
model; the result is evidence for ReleaseGuard, not a proof."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import cache
from pathlib import Path

from .graph import Graph
from .history import git
from .repo_map import is_test_file

_JS_EXT = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)
MAX_LISTED = 8


@dataclass
class Definition:
    start: int
    end: int
    name: str  # qualified: Class.method
    simple: str


@dataclass
class FunctionImpact:
    file: str
    function: str
    callers: list[str] = field(default_factory=list)  # "file:function", code only
    tests: list[str] = field(default_factory=list)  # "file:function" in test files

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- Python


def _python_definitions(source: str) -> list[Definition]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    found = []
    stack: list[tuple[str, ast.AST]] = [("", tree)]
    while stack:
        prefix, parent = stack.pop()
        for node in ast.iter_child_nodes(parent):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                found.append(Definition(start, node.end_lineno or node.lineno, name, node.name))
                stack.append((name + ".", node))
            elif isinstance(node, ast.ClassDef):
                stack.append((prefix + node.name + ".", node))
    return found


def _python_calls(source: str) -> list[tuple[str, int]]:
    """(called name, line) — the last part of `a.b.c()` is `c`."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                calls.append((func.attr, node.lineno))
            elif isinstance(func, ast.Name):
                calls.append((func.id, node.lineno))
    return calls


# ---------------------------------------------------------------- JavaScript / TypeScript


@cache
def _parser(suffix: str):
    try:
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    try:
        if suffix in (".ts", ".tsx"):
            import tree_sitter_typescript as ts

            lang = ts.language_tsx() if suffix == ".tsx" else ts.language_typescript()
        else:
            import tree_sitter_javascript as js

            lang = js.language()
    except ImportError:
        return None
    return Parser(Language(lang))


def tree_sitter_available() -> bool:
    return _parser(".ts") is not None and _parser(".js") is not None


def _js_tree(path: str, source: str):
    parser = _parser(Path(path).suffix)
    return None if parser is None else parser.parse(source.encode("utf-8"))


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


def _js_units(path: str, source: str) -> list[tuple[Definition, object]]:
    """Every named function, method and function-valued variable, with its syntax node."""
    tree = _js_tree(path, source)
    if tree is None:
        return []
    found: list[tuple[Definition, object]] = []

    def visit(node, prefix: str) -> None:
        for child in node.children:
            kind = child.type
            name_node = child.child_by_field_name("name")
            if kind in ("class_declaration", "class", "abstract_class_declaration") and (
                name_node is not None
            ):
                visit(child, prefix + _text(name_node) + ".")
                continue
            simple, body = "", child
            if kind in ("function_declaration", "generator_function_declaration",
                        "method_definition"):  # fmt: skip
                simple = _text(name_node)
            elif kind == "variable_declarator":
                value = child.child_by_field_name("value")
                if value is not None and value.type in _JS_FUNCTIONS:
                    simple, body = _text(name_node), value
            if simple:
                name = prefix + simple
                start, end = child.start_point[0] + 1, child.end_point[0] + 1
                found.append((Definition(start, end, name, simple), body))
                visit(child, name + ".")
            else:
                visit(child, prefix)

    visit(tree.root_node, "")
    return found


_JS_FUNCTIONS = ("arrow_function", "function_expression", "function", "generator_function")
_JS_BRANCHES = {
    "if_statement", "for_statement", "for_in_statement", "while_statement", "do_statement",
    "catch_clause", "ternary_expression", "switch_case",
}  # fmt: skip
_JS_LOGIC = {"&&", "||", "??"}


def js_complexity(node, units: set[tuple[int, int]]) -> int:
    """McCabe-style count like the Python one: 1 + one per branch point. Named functions
    inside are counted on their own; anonymous callbacks count toward the enclosing one."""
    count = 1
    stack = list(node.children)
    while stack:
        child = stack.pop()
        if (child.start_byte, child.end_byte) in units:
            continue
        if child.type in _JS_BRANCHES:
            count += 1
        elif child.type == "binary_expression":
            operator = child.child_by_field_name("operator")
            if operator is not None and operator.type in _JS_LOGIC:
                count += 1
        stack.extend(child.children)
    return count


def js_function_sizes(path: str, source: str) -> list[tuple[Definition, int]]:
    """(definition, complexity) for every named JS/TS function; empty without Tree-sitter."""
    found = _js_units(path, source)
    units = {(body.start_byte, body.end_byte) for _, body in found}
    units |= {(b.parent.start_byte, b.parent.end_byte) for _, b in found if b.parent is not None
              and b.parent.type == "variable_declarator"}  # fmt: skip
    return [(d, js_complexity(body, units)) for d, body in found]


def _js_definitions(path: str, source: str) -> list[Definition]:
    return [d for d, _ in _js_units(path, source)]


def _js_calls(path: str, source: str) -> list[tuple[str, int]]:
    tree = _js_tree(path, source)
    if tree is None:
        return []
    calls = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in ("call_expression", "new_expression"):
            func = node.child_by_field_name("function") or node.child_by_field_name("constructor")
            if func is not None:
                if func.type == "member_expression":
                    func = func.child_by_field_name("property")
                if func is not None and func.type in (
                    "identifier", "property_identifier", "type_identifier"
                ):  # fmt: skip
                    calls.append((_text(func), node.start_point[0] + 1))
        stack.extend(node.children)
    return calls


# ---------------------------------------------------------------- shared


def definitions(path: str, source: str) -> list[Definition]:
    if path.endswith(".py"):
        return _python_definitions(source)
    if path.endswith(_JS_EXT):
        return _js_definitions(path, source)
    return []


def calls(path: str, source: str) -> list[tuple[str, int]]:
    if path.endswith(".py"):
        return _python_calls(source)
    if path.endswith(_JS_EXT):
        return _js_calls(path, source)
    return []


def enclosing(defs: list[Definition], line: int) -> str:
    best, size = "<module>", None
    for d in defs:
        if d.start <= line <= d.end and (size is None or d.end - d.start < size):
            best, size = d.name, d.end - d.start
    return best


def changed_lines(root: Path, base: str, path: str) -> set[int] | None:
    """Line numbers in the new version of `path` that the change added or edited. None for a
    file git does not know yet (all of it is new)."""
    diff = git(root, "diff", "-U0", "--no-color", base, "--", path)
    if diff is None:
        return set()
    if not diff.strip():
        tracked = git(root, "ls-files", "--error-unmatch", "--", path)
        return set() if tracked is not None else None
    lines: set[int] = set()
    for match in _HUNK.finditer(diff):
        start, count = int(match.group(1)), int(match.group(2) or 1)
        if count == 0:  # a pure deletion: the function around it still changed
            lines.add(max(start, 1))
        lines.update(range(start, start + count))
    return lines


def touched(defs: list[Definition], lines: set[int] | None) -> list[Definition]:
    """The innermost function around each changed line; every function in a new file."""
    if lines is None:
        return defs
    hits: dict[str, Definition] = {}
    for line in lines:
        inside = [d for d in defs if d.start <= line <= d.end]
        if inside:
            best = min(inside, key=lambda d: d.end - d.start)
            hits[best.name] = best
    return sorted(hits.values(), key=lambda d: d.start)


def function_impact(
    root: Path,
    graph: Graph,
    base: str,
    changed: list[str],
    read: Callable[[str], str],
) -> list[FunctionImpact]:
    results: list[FunctionImpact] = []
    parsed: dict[str, tuple[list[Definition], list[tuple[str, int]]]] = {}

    def parse(path: str):
        if path not in parsed:
            try:
                source = read(path)
            except (OSError, ValueError, KeyError):
                source = ""
            parsed[path] = (definitions(path, source), calls(path, source))
        return parsed[path]

    for path in changed:
        if is_test_file(path):
            continue
        defs, _ = parse(path)
        if not defs:
            continue
        hit = [d for d in touched(defs, changed_lines(root, base, path))
               if not (d.simple.startswith("__") and d.simple.endswith("__"))]  # fmt: skip
        if not hit:
            continue
        by_name = {d.simple: FunctionImpact(path, d.name) for d in hit}
        for user in sorted({path, *graph.rdeps.get(path, ())}):
            user_defs, user_calls = parse(user)
            for name, line in user_calls:
                impact = by_name.get(name)
                if impact is None:
                    continue
                where = enclosing(user_defs, line)
                if user == path and where.split(".")[-1] == name:
                    continue  # recursion is not a caller
                label = f"{user}:{where}"
                bucket = impact.tests if is_test_file(user) else impact.callers
                if label not in bucket:
                    bucket.append(label)
        results.extend(by_name.values())
    return results


def describe(impacts: list[FunctionImpact]) -> list[str]:
    """One evidence line per changed function, most used first."""
    lines = []
    ranked = sorted(impacts, key=lambda i: (-(len(i.callers) + len(i.tests)), i.file, i.function))
    for item in ranked[:MAX_LISTED]:
        parts = []
        if item.callers:
            shown = ", ".join(f"`{c}`" for c in item.callers[:3])
            more = f" ve {len(item.callers) - 3} tane daha" if len(item.callers) > 3 else ""
            parts.append(f"{len(item.callers)} çağıran ({shown}{more})")
        parts.append(f"{len(item.tests)} test" if item.tests else "hiçbir test çağırmıyor")
        lines.append(f"`{item.file}:{item.function}` değişti: " + ", ".join(parts))
    if len(ranked) > MAX_LISTED:
        lines.append(f"ve {len(ranked) - MAX_LISTED} değişen fonksiyon daha")
    return lines
