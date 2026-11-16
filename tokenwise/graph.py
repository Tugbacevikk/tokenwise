"""Which file imports which: the dependency graph shared by the agent, the health scan and the
release check. Python imports are read with `ast`; JavaScript and TypeScript relative imports
with a pattern. Nothing here calls a model."""

from __future__ import annotations

import ast
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .config import Settings
from .repo_map import is_test_file, list_source_files
from .textio import read_source

_JS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_JS_IMPORT = re.compile(
    r"""(?:import\s[^'"]*?from\s*|import\s*\(\s*|require\s*\(\s*|export\s[^'"]*?from\s*|import\s+)"""
    r"""['"]([^'"]+)['"]"""
)


@dataclass
class Graph:
    files: list[str]
    deps: dict[str, set[str]] = field(default_factory=dict)  # file -> files it imports
    rdeps: dict[str, set[str]] = field(default_factory=dict)  # file -> files importing it

    def add(self, src: str, dst: str) -> None:
        if src == dst:
            return
        self.deps.setdefault(src, set()).add(dst)
        self.rdeps.setdefault(dst, set()).add(src)

    def fan_in(self, path: str, tests: bool = False) -> int:
        """How many files import this one; test files only when `tests` is true."""
        users = self.rdeps.get(path, ())
        return len(users) if tests else sum(not is_test_file(u) for u in users)

    def affected(self, changed: list[str], depth: int = 2) -> dict[str, int]:
        """Files that import a changed file, directly (1) or through others (2..depth)."""
        seen: dict[str, int] = {}
        queue = deque((path, 0) for path in changed)
        start = set(changed)
        while queue:
            path, level = queue.popleft()
            if level >= depth:
                continue
            for user in sorted(self.rdeps.get(path, ())):
                if user not in start and user not in seen:
                    seen[user] = level + 1
                    queue.append((user, level + 1))
        return seen

    def tested(self, path: str) -> bool:
        """True if a test file imports the file or is named after it."""
        if is_test_file(path):
            return True
        if any(is_test_file(user) for user in self.rdeps.get(path, ())):
            return True
        stem = PurePosixPath(path).stem
        if stem == "__init__":
            return True
        return any(is_test_file(f) and _names_stem(f, stem) for f in self.files)

    def cycles(self) -> list[list[str]]:
        """Groups of files that import each other (Tarjan's strongly connected components)."""
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        found: list[list[str]] = []
        counter = 0

        def visit(node: str) -> None:
            nonlocal counter
            work = [(node, iter(sorted(self.deps.get(node, ()))))]
            index[node] = low[node] = counter
            counter += 1
            stack.append(node)
            on_stack.add(node)
            while work:
                current, children = work[-1]
                child = next(children, None)
                if child is not None:
                    if child not in index:
                        index[child] = low[child] = counter
                        counter += 1
                        stack.append(child)
                        on_stack.add(child)
                        work.append((child, iter(sorted(self.deps.get(child, ())))))
                    elif child in on_stack:
                        low[current] = min(low[current], index[child])
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[current])
                if low[current] == index[current]:
                    group = []
                    while True:
                        item = stack.pop()
                        on_stack.discard(item)
                        group.append(item)
                        if item == current:
                            break
                    if len(group) > 1:
                        found.append(sorted(group))

        for node in sorted(self.deps):
            if node not in index:
                visit(node)
        return sorted(found)


def _names_stem(test_path: str, stem: str) -> bool:
    name = PurePosixPath(test_path).stem.lower()
    for part in ("test_", "_test", ".test", ".spec", "_spec", "tests_"):
        name = name.replace(part, "")
    return name == stem.lower()


def _module_names(path: str) -> list[str]:
    """`src/app/auth.py` -> ["src.app.auth", "app.auth"]; packages map to their folder."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    names = []
    for start in range(len(parts)):
        if parts[start:]:
            names.append(".".join(parts[start:]))
    return names


def _python_imports(path: str, source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    package = list(PurePosixPath(path).parent.parts)
    is_init = PurePosixPath(path).stem == "__init__"
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                keep = len(package) - (node.level - 1)
                if keep < 0 or (not package and not is_init and node.level > 1):
                    continue
                prefix = ".".join(package[:keep])
                base = f"{prefix}.{base}".strip(".") if base else prefix
            if base:
                found.append(base)
            found.extend(f"{base}.{alias.name}".strip(".") for alias in node.names)
    return found


def _resolve_js(path: str, spec: str, files: set[str]) -> str | None:
    if not spec.startswith("."):
        return None
    target = PurePosixPath(path).parent.joinpath(spec)
    parts: list[str] = []
    for part in target.parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part != ".":
            parts.append(part)
    base = "/".join(parts)
    candidates = [base] + [base + ext for ext in _JS_EXT]
    candidates += [f"{base}/index{ext}" for ext in _JS_EXT]
    stem = str(PurePosixPath(base).with_suffix(""))
    candidates += [stem + ext for ext in _JS_EXT]  # "./x.js" written for "x.ts"
    return next((c for c in candidates if c in files), None)


def build_graph(settings: Settings, files: list[str] | None = None) -> Graph:
    files = files if files is not None else list_source_files(settings)
    graph = Graph(files=files)
    file_set = set(files)
    modules: dict[str, str] = {}
    for path in files:
        if path.endswith(".py"):
            for name in _module_names(path):
                modules.setdefault(name, path)

    for path in files:
        try:
            source = read_source(settings.root / path)
        except (OSError, ValueError):
            continue
        if path.endswith(".py"):
            for name in _python_imports(path, source):
                target = modules.get(name)
                if target:
                    graph.add(path, target)
        elif path.endswith(_JS_EXT):
            for spec in _JS_IMPORT.findall(source):
                target = _resolve_js(path, spec, file_set)
                if target:
                    graph.add(path, target)
    return graph
