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
from pathlib import Path

from . import syntax
from .graph import Graph
from .history import git
from .repo_map import is_test_file

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)
MAX_LISTED = 8


@dataclass
class Definition:
    start: int
    end: int
    name: str  # qualified: Class.method
    simple: str
    nested: bool = False  # defined inside another function: a private helper of it


@dataclass
class FunctionImpact:
    file: str
    function: str
    callers: list[str] = field(default_factory=list)  # "file:function", code only
    tests: list[str] = field(default_factory=list)  # "file:function" in test files
    nested: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- Python


def _python_definitions(source: str) -> list[Definition]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    found = []
    stack: list[tuple[str, ast.AST, bool]] = [("", tree, False)]
    while stack:
        prefix, parent, in_function = stack.pop()
        for node in ast.iter_child_nodes(parent):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                found.append(Definition(start, node.end_lineno or node.lineno, name, node.name,
                                        in_function))  # fmt: skip
                stack.append((name + ".", node, True))
            elif isinstance(node, ast.ClassDef):
                stack.append((prefix + node.name + ".", node, in_function))
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


# ---------------------------------------------------------------- Tree-sitter languages
# JavaScript, TypeScript, Go and Java; syntax.py says what counts as a function in each.


def tree_sitter_available() -> bool:
    return syntax.available(syntax.JAVASCRIPT)


def _receiver(node) -> str:
    """`Svc.` for the Go method `func (s *Svc) Run()`."""
    receiver = node.child_by_field_name("receiver")
    for child in syntax.walk(receiver) if receiver is not None else ():
        if child.type == "type_identifier":
            return syntax.text(child) + "."
    return ""


def _function_name(lang: syntax.Lang, node) -> tuple[str, str, object] | None:
    """(type prefix, name, body) when `node` defines a named function, else None."""
    if node.type in lang.functions:
        name = syntax.text(node.child_by_field_name("name"))
        if not name:
            return None
        own = _receiver(node) if lang is syntax.GO and node.type == "method_declaration" else ""
        return own, name, node
    if node.type == "variable_declarator":  # const f = () => ..., Runnable r = () -> ...
        value = node.child_by_field_name("value")
        name = syntax.text(node.child_by_field_name("name"))
        if value is not None and value.type in lang.anonymous and name:
            return "", name, value
    return None


def _units(path: str, source: str) -> list[tuple[Definition, object]]:
    """Every named function, method and function-valued variable, with its syntax node."""
    lang, tree = syntax.language_for(path), syntax.parse(path, source)
    if lang is None or tree is None:
        return []
    found: list[tuple[Definition, object]] = []

    def visit(node, prefix: str, in_function: bool = False) -> None:
        for child in node.children:
            name_node = child.child_by_field_name("name")
            if child.type in lang.containers and name_node is not None:
                visit(child, prefix + syntax.text(name_node) + ".", in_function)
                continue
            named = _function_name(lang, child)
            if named:
                own, simple, body = named
                name = prefix + own + simple
                start, end = child.start_point[0] + 1, child.end_point[0] + 1
                found.append((Definition(start, end, name, simple, in_function), body))
                visit(child, name + ".", True)
            else:
                # an anonymous function's body is still inside a function
                visit(child, prefix, in_function or child.type in lang.anonymous)

    visit(tree.root_node, "")
    return found


def tree_complexity(lang: syntax.Lang, node, units: set[tuple[int, int]]) -> int:
    """McCabe-style count like the Python one: 1 + one per branch point. Named functions
    inside are counted on their own; anonymous callbacks count toward the enclosing one."""
    count = 1
    stack = list(node.children)
    while stack:
        child = stack.pop()
        if (child.start_byte, child.end_byte) in units:
            continue
        kind = child.type
        if kind in lang.branches:
            count += 1
        elif kind in lang.cases:
            count += not (child.children and child.children[0].type == "default")
        elif kind == "binary_expression":
            operator = child.child_by_field_name("operator")
            if operator is not None and operator.type in lang.logic:
                count += 1
        stack.extend(child.children)
    return count


def function_sizes(path: str, source: str) -> list[tuple[Definition, int]]:
    """(definition, complexity) for every named function in a Tree-sitter language; empty
    when the file is not one or its grammar is not installed."""
    lang = syntax.language_for(path)
    found = _units(path, source)
    units = {(body.start_byte, body.end_byte) for _, body in found}
    units |= {(b.parent.start_byte, b.parent.end_byte) for _, b in found if b.parent is not None
              and b.parent.type == "variable_declarator"}  # fmt: skip
    return [(d, tree_complexity(lang, body, units)) for d, body in found]


js_function_sizes = function_sizes  # the name from when only JavaScript was parsed

_CALL_NODES = ("call_expression", "new_expression", "method_invocation",
               "object_creation_expression")  # fmt: skip
_NAMES = ("identifier", "property_identifier", "type_identifier", "field_identifier")


def _callee(node) -> str:
    """The called name: `save` for `repo.save(x)`, `Order` for `new Order()`."""
    if node.type == "method_invocation":
        return syntax.text(node.child_by_field_name("name"))
    func = next((node.child_by_field_name(f) for f in ("function", "constructor", "type")
                 if node.child_by_field_name(f) is not None), None)  # fmt: skip
    if func is not None and func.type == "member_expression":
        func = func.child_by_field_name("property")
    elif func is not None and func.type == "selector_expression":
        func = func.child_by_field_name("field")
    func = _type_name(func)
    return syntax.text(func) if func is not None and func.type in _NAMES else ""


def _type_name(node):
    """List<T> -> List, java.util.List -> List; anything else as it is."""
    while node is not None and node.type in ("generic_type", "scoped_type_identifier"):
        inner = [c for c in node.children if c.type.endswith("type_identifier")]
        if not inner:
            return None
        node = inner[0] if node.type == "generic_type" else inner[-1]
    return node


def _tree_calls(path: str, source: str) -> list[tuple[str, int]]:
    tree = syntax.parse(path, source)
    if tree is None:
        return []
    found = []
    for node in syntax.walk(tree.root_node):
        if node.type in _CALL_NODES:
            name = _callee(node)
            if name:
                found.append((name, node.start_point[0] + 1))
    return found


# ---------------------------------------------------------------- shared


def definitions(path: str, source: str) -> list[Definition]:
    if path.endswith(".py"):
        return _python_definitions(source)
    return [d for d, _ in _units(path, source)]


def calls(path: str, source: str) -> list[tuple[str, int]]:
    if path.endswith(".py"):
        return _python_calls(source)
    return _tree_calls(path, source)


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
        by_name = {d.simple: FunctionImpact(path, d.name, nested=d.nested) for d in hit}
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
