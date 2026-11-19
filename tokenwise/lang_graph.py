"""Which file uses which in Go and Java, read from their syntax trees.

Both languages group files into packages, and files of one package use each other's names
without importing them. So an edge comes from two places: an import of another package
(to every file of it, or the one class named), and a top-level name of the same package
that the file mentions. Method names are left out of the second: `String()` or `close()`
in many files would tie unrelated files together.

The edges are not "load time" edges: Go refuses import cycles at compile time, and classes
referring to each other are ordinary Java. So they never make an import-cycle finding; they
feed fan-in, missing tests, the reach of a change and the callers of a changed function."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from . import syntax
from .repo_map import is_test_file

_NAMES = ("identifier", "type_identifier")


def _mentions(tree) -> set[str]:
    return {syntax.text(n) for n in syntax.walk(tree.root_node) if n.type in _NAMES}


def _same_package(groups: dict[str, list[str]], declared: dict[str, set[str]],
                  mentions: dict[str, set[str]]) -> list[tuple[str, str]]:  # fmt: skip
    edges = []
    for members in groups.values():
        for path in members:
            used = mentions.get(path, set())
            for other in members:
                if other != path and used & declared.get(other, set()):
                    edges.append((path, other))
    return edges


# ---------------------------------------------------------------- Go


def go_modules(root: Path, ignore_dirs: Iterable[str]) -> dict[str, str]:
    """Module path -> its folder ("" for the project root), from every go.mod."""
    found: dict[str, str] = {}
    ignore = set(ignore_dirs) | {"vendor", "node_modules"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore and not d.startswith(".")]
        if "go.mod" not in filenames:
            continue
        try:
            module = _module_path(Path(dirpath, "go.mod").read_text("utf-8", errors="replace"))
        except OSError:
            continue
        if module:
            base = Path(dirpath).relative_to(root).as_posix()
            found[module] = "" if base == "." else base
    return found


def _module_path(go_mod: str) -> str:
    for line in go_mod.splitlines():
        parts = line.split("//")[0].split()
        if len(parts) == 2 and parts[0] == "module":
            return parts[1].strip('"')
    return ""


def _go_declared(tree) -> set[str]:
    names = set()
    for node in tree.root_node.children:
        if node.type == "function_declaration":
            names.add(syntax.text(node.child_by_field_name("name")))
        elif node.type in ("type_declaration", "const_declaration", "var_declaration"):
            for spec in node.named_children:
                names |= {syntax.text(c) for c in spec.children if c.type in _NAMES}
    return names - {"_", "init", "main"}


def _go_package(tree) -> str:
    clause = next((n for n in tree.root_node.children if n.type == "package_clause"), None)
    name = next((c for c in clause.children if c.type == "package_identifier"), None) if (
        clause is not None
    ) else None  # fmt: skip
    return syntax.text(name)


def _go_imports(tree) -> list[str]:
    found = []
    for node in syntax.walk(tree.root_node):
        if node.type == "import_spec":
            value = syntax.string_value(node.child_by_field_name("path"))
            if value:
                found.append(value)
    return found


def go_edges(files: list[str], sources: dict[str, str], modules: dict[str, str]):
    """(file, file it uses) for the Go files of the project."""
    by_dir: dict[str, list[str]] = {}
    for path in files:
        by_dir.setdefault(PurePosixPath(path).parent.as_posix(), []).append(path)
    groups: dict[str, list[str]] = {}
    declared: dict[str, set[str]] = {}
    mentions: dict[str, set[str]] = {}
    edges: list[tuple[str, str]] = []
    for path in files:
        tree = syntax.parse(path, sources.get(path, ""))
        if tree is None:
            continue
        folder = PurePosixPath(path).parent.as_posix()
        package = _go_package(tree)
        groups.setdefault(f"{folder}:{package.removesuffix('_test')}", []).append(path)
        declared[path] = _go_declared(tree)
        mentions[path] = _mentions(tree)
        for spec in _go_imports(tree):
            for target in _go_package_files(spec, modules, by_dir):
                if target != path:
                    edges.append((path, target))
    return edges + _same_package(groups, declared, mentions)


def _go_package_files(spec: str, modules: dict[str, str], by_dir: dict[str, list[str]]):
    for module, folder in modules.items():
        if spec != module and not spec.startswith(module + "/"):
            continue
        rest = spec[len(module) :].lstrip("/")
        target = "/".join(p for p in (folder, rest) if p) or "."
        return [f for f in by_dir.get(target, ()) if not f.endswith("_test.go")]
    return []


# ---------------------------------------------------------------- Java


def _java_package(tree) -> str:
    for node in tree.root_node.children:
        if node.type == "package_declaration":
            name = next((c for c in node.named_children if c.type != "annotation"), None)
            return syntax.text(name)
    return ""


def _java_imports(tree) -> list[tuple[str, bool, bool]]:
    """(name, static, wildcard) for every import."""
    found = []
    for node in tree.root_node.children:
        if node.type != "import_declaration":
            continue
        name = next((c for c in node.named_children if c.type in ("scoped_identifier",
                                                                  "identifier")), None)  # fmt: skip
        kinds = {c.type for c in node.children}
        if name is not None:
            found.append((syntax.text(name), "static" in kinds, "asterisk" in kinds))
    return found


def java_edges(files: list[str], sources: dict[str, str]):
    """(file, file it uses) for the Java files of the project. A class is found by its
    package and file name, so no source folder layout (src/main/java) has to be known."""
    classes: dict[str, str] = {}  # a.b.Order -> path
    packages: dict[str, list[str]] = {}
    trees = {}
    for path in files:
        tree = syntax.parse(path, sources.get(path, ""))
        if tree is None:
            continue
        trees[path] = tree
        package = _java_package(tree)
        stem = PurePosixPath(path).stem
        classes.setdefault(f"{package}.{stem}" if package else stem, path)
        packages.setdefault(package, []).append(path)
    edges: list[tuple[str, str]] = []
    declared = {path: {PurePosixPath(path).stem} for path in trees}
    mentions: dict[str, set[str]] = {}
    for path, tree in trees.items():
        mentions[path] = _mentions(tree)
        for name, static, wildcard in _java_imports(tree):
            for target in _java_targets(name, static, wildcard, classes, packages):
                if target != path:
                    edges.append((path, target))
    return edges + _same_package(packages, declared, mentions)


def _java_targets(name: str, static: bool, wildcard: bool, classes: dict[str, str],
                  packages: dict[str, list[str]]) -> list[str]:  # fmt: skip
    if wildcard and not static:  # import a.b.*;  (its test classes share the package)
        return [p for p in packages.get(name, ()) if not is_test_file(p)]
    if name in classes:  # import a.b.C;  import static a.b.C.*;
        return [classes[name]]
    owner = name.rsplit(".", 1)[0]  # import static a.b.C.method;  import a.b.C.Inner;
    return [classes[owner]] if owner in classes else []
