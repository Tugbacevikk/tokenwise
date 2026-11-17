"""Which file imports which: the dependency graph shared by the agent, the health scan and the
release check. Python imports are read with `ast`; JavaScript and TypeScript relative imports
with a pattern. Nothing here calls a model."""

from __future__ import annotations

import ast
import re
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .config import Settings
from .repo_map import is_test_file, list_source_files
from .textio import read_source

_JS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_JS_IMPORT = re.compile(
    r"""(?:import\s[^'"]*?from\s*|import\s*\(\s*|require\s*\(\s*|export\s[^'"]*?from\s*|import\s+)"""
    r"""['"]([^'"]+)['"]"""
)


_STDLIB = frozenset(sys.stdlib_module_names)


@dataclass
class Graph:
    files: list[str]
    deps: dict[str, set[str]] = field(default_factory=dict)  # file -> files it imports
    rdeps: dict[str, set[str]] = field(default_factory=dict)  # file -> files importing it
    # imports that run when the module loads: not inside a function, not `if TYPE_CHECKING`,
    # not `import type`. Only these can make an import cycle.
    runtime: dict[str, set[str]] = field(default_factory=dict)

    def add(self, src: str, dst: str, runtime: bool = True) -> None:
        if src == dst:
            return
        self.deps.setdefault(src, set()).add(dst)
        self.rdeps.setdefault(dst, set()).add(src)
        if runtime:
            self.runtime.setdefault(src, set()).add(dst)

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
        """True if a test file is named after the file or reaches it through imports: directly,
        through package entry files (`__init__.py`, `index.js`) that re-export it, or through
        one ordinary module in between. Libraries are tested through their public package,
        so `import httpx` in a test covers `httpx/_api.py`."""
        if is_test_file(path):
            return True
        if self._reached_by_test(path):
            return True
        stem = PurePosixPath(path).stem
        if stem == "__init__":
            return True
        return any(is_test_file(f) and _names_stem(f, stem) for f in self.files)

    def _reached_by_test(self, path: str) -> bool:
        seen = {path}
        queue = deque([(path, 0)])
        while queue:
            node, plain_hops = queue.popleft()
            for user in self.rdeps.get(node, ()):
                if is_test_file(user):
                    return True
                hops = plain_hops + (0 if _is_entry(user) else 1)
                if user not in seen and hops <= 1:
                    seen.add(user)
                    queue.append((user, hops))
        return False

    def cycles(self) -> list[list[str]]:
        """Groups of files whose load-time imports go round in a circle (Tarjan's strongly
        connected components). An import inside a function is the usual way to break a
        cycle, so it does not count."""
        edges = self.runtime
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        found: list[list[str]] = []
        counter = 0

        def visit(node: str) -> None:
            nonlocal counter
            work = [(node, iter(sorted(edges.get(node, ()))))]
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
                        work.append((child, iter(sorted(edges.get(child, ())))))
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

        for node in sorted(edges):
            if node not in index:
                visit(node)
        return sorted(found)


def _is_entry(path: str) -> bool:
    name = PurePosixPath(path).name
    return name == "__init__.py" or PurePosixPath(name).stem == "index"


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


def _python_imports(path: str, source: str) -> list[tuple[list[str], bool, bool]]:
    """Each import as (module names to try, most specific first; runs at load time;
    absolute)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    package = list(PurePosixPath(path).parent.parts)
    is_init = PurePosixPath(path).stem == "__init__"
    found: list[tuple[list[str], bool, bool]] = []

    def visit(node: ast.AST, runtime: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                visit(child, False)
            elif isinstance(child, ast.If) and (
                _is_type_checking(child.test) or _is_main_guard(child.test)
            ):
                for item in child.body:
                    visit_one(item, False)
                for item in child.orelse:
                    visit_one(item, runtime)
            else:
                visit_one(child, runtime)

    def visit_one(child: ast.AST, runtime: bool) -> None:
        if isinstance(child, ast.Import):
            for alias in child.names:
                found.append(([alias.name], runtime, True))
        elif isinstance(child, ast.ImportFrom):
            base = child.module or ""
            if child.level:
                keep = len(package) - (child.level - 1)
                if keep < 0 or (not package and not is_init and child.level > 1):
                    return
                prefix = ".".join(package[:keep])
                base = f"{prefix}.{base}".strip(".") if base else prefix
            for alias in child.names:
                # `from pkg import mod` imports pkg/mod.py; `from pkg import name` imports pkg
                names = [f"{base}.{alias.name}".strip(".")] + ([base] if base else [])
                found.append((names, runtime, not child.level))
        else:
            visit(child, runtime)

    visit(tree, True)
    return found


def _is_main_guard(test: ast.AST) -> bool:
    """`if __name__ == "__main__":` runs only as a script, never on import."""
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in test.comparators)
    )


def _is_type_checking(test: ast.AST) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _js_packages(root, files: set[str], ignore_dirs) -> dict[str, str]:
    """The project's own packages as import names: "zod/v4" -> "packages/zod/src/v4/index.ts".
    Read from each package.json's name, exports (any condition that points at a source file)
    and main/module/source fields. Tests usually import a package by its name, not by path."""
    import json
    import os

    found: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs and d != "node_modules"]
        if "package.json" not in filenames:
            continue
        try:
            data = json.loads(Path(dirpath, "package.json").read_text("utf-8"))
        except (OSError, ValueError):
            continue
        name = data.get("name") if isinstance(data, dict) else None
        if not isinstance(name, str) or not name:
            continue
        base = Path(dirpath).relative_to(root).as_posix()
        base = "" if base == "." else base + "/"
        exports = data.get("exports")
        if isinstance(exports, (str, dict)) and not (
            isinstance(exports, dict) and any(k.startswith(".") for k in exports)
        ):
            exports = {".": exports}
        entries = dict(exports) if isinstance(exports, dict) else {}
        entries.setdefault(".", [data.get(k) for k in ("source", "module", "main")])
        for sub, target in entries.items():
            spec = name if sub == "." else f"{name}/{sub.removeprefix('./')}"
            for candidate in _targets(target):
                path = _js_file(base + candidate.removeprefix("./"), files)
                if path:
                    found.setdefault(spec, path)
                    break
        found.setdefault(f"{name}/*", base)  # name/sub -> try <dir>/src/sub, <dir>/sub
    return found


def _targets(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [t for v in value for t in _targets(v)]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _targets(v)]
    return []


def _js_file(base: str, files: set[str]) -> str | None:
    stem = str(PurePosixPath(base).with_suffix("")) if PurePosixPath(base).suffix else base
    candidates = [base, *(stem + ext for ext in _JS_EXT), *(f"{base}/index{e}" for e in _JS_EXT)]
    return next((c for c in candidates if c in files), None)


def _resolve_package(spec: str, packages: dict[str, str], files: set[str]) -> str | None:
    if spec in packages:
        return packages[spec]
    for key, base in packages.items():
        if key.endswith("/*") and spec.startswith(key[:-1]):
            rest = spec[len(key) - 1 :]
            for folder in ("src/", "lib/", "source/", ""):
                path = _js_file(f"{base}{folder}{rest}", files)
                if path:
                    return path
    return None


def _resolve_js(path: str, spec: str, files: set[str],
                packages: dict[str, str] | None = None) -> str | None:  # fmt: skip
    if not spec.startswith("."):
        return _resolve_package(spec, packages, files) if packages else None
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
    if not base:  # require("..") from a top-level folder: the package's own entry point
        return next((f"index{ext}" for ext in _JS_EXT if f"index{ext}" in files), None)
    candidates = [base] + [base + ext for ext in _JS_EXT]
    candidates += [f"{base}/index{ext}" for ext in _JS_EXT]
    stem = str(PurePosixPath(base).with_suffix(""))
    candidates += [stem + ext for ext in _JS_EXT]  # "./x.js" written for "x.ts"
    return next((c for c in candidates if c in files), None)


def build_graph(
    settings: Settings,
    files: list[str] | None = None,
    read: Callable[[str], str] | None = None,
) -> Graph:
    """The import graph of `files` (default: the project's source files). `read` returns a
    file's text; by default it is read from disk, the calibration reads it from a git commit."""
    files = files if files is not None else list_source_files(settings)
    read = read or (lambda path: read_source(settings.root / path))
    graph = Graph(files=files)
    file_set = set(files)
    packages = (
        _js_packages(settings.root, file_set, settings.ignore_dirs)
        if any(f.endswith(_JS_EXT) for f in files) else {}
    )  # fmt: skip
    modules: dict[str, str] = {}
    rooted: dict[str, str] = {}  # names counted from the project root (or its src/ folder)
    for path in files:
        if path.endswith(".py"):
            names = _module_names(path)
            for name in names:
                modules.setdefault(name, path)
            for name in names[: 2 if names[0].startswith("src.") else 1]:
                rooted.setdefault(name, path)

    for path in files:
        try:
            source = read(path)
        except (OSError, ValueError, KeyError):
            continue
        if path.endswith(".py"):
            for names, runtime, absolute in _python_imports(path, source):
                # `import json` is the standard library, not flask/json/: a stdlib name only
                # matches a module that really sits at the top of the project
                table = rooted if absolute and names[-1].split(".")[0] in _STDLIB else modules
                target = next((table[n] for n in names if n in table), None)
                if target:
                    graph.add(path, target, runtime)
        elif path.endswith(_JS_EXT):
            for match in _JS_IMPORT.finditer(source):
                target = _resolve_js(path, match.group(1), file_set, packages)
                if target:
                    text = match.group(0)
                    lazy = text.startswith(("import(", "import (")) or re.match(
                        r"(import|export)\s+type\b", text
                    )
                    graph.add(path, target, not lazy)
    return graph
