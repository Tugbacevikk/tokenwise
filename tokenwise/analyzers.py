"""Deterministic checks that turn source files into findings. Each finding has a fingerprint that
does not depend on line numbers, so the same problem keeps its identity while code moves around.
Models never decide what is a finding; they only explain or fix one later."""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field

from .config import Settings
from .graph import Graph
from .repo_map import is_support_file, is_test_file
from .syntax import language_for
from .textio import read_source

SEVERITIES = ("high", "medium", "low")
CATEGORIES = ("quality", "testing", "security", "architecture", "maintainability")

COMPLEXITY_MEDIUM = 10
COMPLEXITY_HIGH = 20
LONG_FUNCTION = 80
LARGE_FILE = 600
TODO_MIN = 3

_TODO = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
_SECRETS = (
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    (
        "hardcoded-secret",
        re.compile(
            r"""(?i)\b(api[_-]?key|secret|passw(?:or)?d|token)\b\s*[:=]\s*['"]([^'"\s]{12,})['"]"""
        ),
    ),
)
_PLACEHOLDER = re.compile(r"(?i)(example|changeme|dummy|placeholder|your[_-]|xxx|<|\*\*\*|\$\{)")


@dataclass
class Finding:
    rule: str
    category: str
    severity: str
    file: str
    title: str
    detail: str = ""
    line: int = 0
    key: str = ""  # what identifies this problem inside the file (a function name, a pattern)
    data: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        raw = f"{self.rule}|{self.file}|{self.key}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def complexity(node: ast.AST) -> int:
    """McCabe-style count: 1 + one per branch point inside the function (nested functions
    are counted on their own)."""
    count = 1
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.Assert)):
            count += 1
        elif isinstance(child, ast.ExceptHandler):
            count += 1
        elif isinstance(child, ast.BoolOp):
            count += len(child.values) - 1
        elif isinstance(child, ast.comprehension):
            count += 1 + len(child.ifs)
        elif isinstance(child, ast.match_case):
            count += 1
        stack.extend(ast.iter_child_nodes(child))
    return count


def _functions(tree: ast.AST):
    """(qualified name, node) for every function, methods as Class.method."""
    stack = [("", tree)]
    while stack:
        prefix, parent = stack.pop()
        for node in ast.iter_child_nodes(parent):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{node.name}"
                yield name, node
                stack.append((name + ".", node))
            elif isinstance(node, ast.ClassDef):
                stack.append((f"{prefix}{node.name}.", node))


def _call_name(node: ast.Call) -> str:
    func = node.func
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    return ".".join(reversed(parts))


def size_findings(path: str, name: str, score: int, length: int, line: int) -> list[Finding]:
    """Complexity and length findings for one function, in any language."""
    found = []
    if score > COMPLEXITY_MEDIUM:
        found.append(
            Finding(
                "high-complexity",
                "quality",
                "high" if score > COMPLEXITY_HIGH else "medium",
                path,
                f"{name} karmaşık (karmaşıklık {score})",
                f"Dallanma sayısı {score}; sınır {COMPLEXITY_MEDIUM}.",
                line,
                name,
                {"complexity": score, "function": name},
            )
        )
    if length > LONG_FUNCTION:
        found.append(
            Finding(
                "long-function",
                "quality",
                "medium" if length > 2 * LONG_FUNCTION else "low",
                path,
                f"{name} uzun ({length} satır)",
                f"Sınır {LONG_FUNCTION} satır.",
                line,
                name,
                {"lines": length, "function": name},
            )
        )
    return found


def tree_findings(path: str, source: str) -> list[Finding]:
    """Function size, and for JavaScript/TypeScript security, in a Tree-sitter language
    (JavaScript, TypeScript, Go, Java). Nothing when its grammar is not installed."""
    from .callgraph import definitions, enclosing, function_sizes
    from .js_security import security_hits

    found = []
    for definition, score in function_sizes(path, source):
        length = definition.end - definition.start + 1
        found += size_findings(path, definition.name, score, length, definition.start)
    hits = security_hits(path, source)
    defs = definitions(path, source) if hits else []
    for hit in hits:
        found.append(
            Finding(
                hit.rule,
                "security",
                "medium",
                path,
                hit.title,
                hit.detail,
                hit.line,
                f"{hit.call}@{enclosing(defs, hit.line)}",
            )
        )
    return found


js_findings = tree_findings  # the name from when only JavaScript was parsed


def code_findings(path: str, source: str) -> list[Finding]:
    """Function-level findings for a source file in a language we can parse."""
    if path.endswith(".py"):
        return python_findings(path, source)
    if language_for(path) is not None:
        return tree_findings(path, source)
    return []


def python_findings(path: str, source: str) -> list[Finding]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        return [
            Finding(
                "syntax-error",
                "quality",
                "high",
                path,
                "Dosya ayrıştırılamıyor",
                str(exc),
                getattr(exc, "lineno", 0) or 0,
            )
        ]
    found = []
    for name, node in _functions(tree):
        length = (node.end_lineno or node.lineno) - node.lineno + 1
        found += size_findings(path, name, complexity(node), length, node.lineno)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = _call_name(node)
        rule, title = "", ""
        if call in ("eval", "exec"):
            rule, title = "dynamic-code", f"{call}() çağrısı"
        elif call in ("pickle.loads", "pickle.load", "marshal.loads"):
            rule, title = "unsafe-deserialize", f"{call}() güvenilmeyen veride tehlikeli"
        elif call == "yaml.load" and not any(k.arg == "Loader" for k in node.keywords):
            rule, title = "unsafe-yaml", "yaml.load Loader olmadan çağrılıyor"
        elif call.startswith("subprocess.") or call in ("os.system", "os.popen"):
            shell = any(
                k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
                for k in node.keywords
            )
            if shell or call in ("os.system", "os.popen"):
                rule, title = "shell-command", f"{call} kabukla komut çalıştırıyor"
        if rule:
            found.append(
                Finding(
                    rule,
                    "security",
                    "medium",
                    path,
                    title,
                    "Girdi kullanıcıdan geliyorsa komut ya da kod enjeksiyonuna açık.",
                    node.lineno,
                    f"{call}@{_enclosing(tree, node.lineno)}",
                )
            )
    return found


def _enclosing(tree: ast.AST, line: int) -> str:
    best = "<module>"
    for name, node in _functions(tree):
        if node.lineno <= line <= (node.end_lineno or node.lineno):
            best = name
    return best


def text_findings(path: str, source: str) -> list[Finding]:
    found = []
    lines = source.splitlines()
    if len(lines) > LARGE_FILE and not is_test_file(path):
        found.append(
            Finding(
                "large-file",
                "quality",
                "medium" if len(lines) > 2 * LARGE_FILE else "low",
                path,
                f"Büyük dosya ({len(lines)} satır)",
                f"Sınır {LARGE_FILE} satır; birden fazla sorumluluk taşıyor olabilir.",
                1,
                data={"lines": len(lines)},
            )
        )
    todos = [i for i, line in enumerate(lines, 1) if _TODO.search(line)]
    if len(todos) >= TODO_MIN:
        found.append(
            Finding(
                "todo-density",
                "maintainability",
                "low",
                path,
                f"{len(todos)} TODO/FIXME notu",
                "Ertelenmiş işler birikiyor.",
                todos[0],
                data={"count": len(todos)},
            )
        )
    if not is_test_file(path):
        for i, line in enumerate(lines, 1):
            for rule, pattern in _SECRETS:
                match = pattern.search(line)
                if not match or _PLACEHOLDER.search(match.group(0)):
                    continue
                digest = hashlib.sha1(match.group(0).encode()).hexdigest()[:8]
                found.append(
                    Finding(
                        "secret",
                        "security",
                        "high",
                        path,
                        f"Kodda gizli anahtar olabilir ({rule})",
                        "Anahtarı koddan çıkarıp ortam değişkenine taşıyın ve iptal edin.",
                        i,
                        f"{rule}:{digest}",
                    )
                )
    return found


def graph_findings(
    graph: Graph, complex_files: set[str], behaviour: set[str] | None = None
) -> list[Finding]:
    """`behaviour`: files that define a function or class. A module of constants or generated
    tables has nothing to test, so only these can be "missing tests" (None = every file)."""
    found = []
    for group in graph.cycles():
        if all(is_support_file(f) for f in group):  # an example app's own cycle
            continue
        found.append(
            Finding(
                "import-cycle",
                "architecture",
                "medium",
                group[0],
                f"{len(group)} dosya birbirini içe aktarıyor",
                " → ".join(group),
                key="|".join(group),
                data={"files": group},
            )
        )
    for path in graph.files:
        if is_test_file(path) or graph.tested(path):
            continue
        if is_support_file(path) or (behaviour is not None and path not in behaviour):
            continue
        fan_in = graph.fan_in(path)
        risky = fan_in >= 3 or path in complex_files
        found.append(
            Finding(
                "missing-tests",
                "testing",
                "medium" if risky else "low",
                path,
                "Test yok" + (f" ({fan_in} dosya buna bağlı)" if fan_in else ""),
                "Hiçbir test dosyası bu dosyayı içe aktarmıyor ya da adını taşımıyor.",
                key="",
                data={"fan_in": fan_in},
            )
        )
    return found


def analyze(settings: Settings, graph: Graph) -> list[Finding]:
    """Every finding in the project, in file order."""
    from .repo_map import outline

    found: list[Finding] = []
    behaviour: set[str] = set()
    for path in graph.files:
        try:
            source = read_source(settings.root / path, strict=False)
        except OSError:
            continue
        if outline(path, source):
            behaviour.add(path)
        support = is_support_file(path)
        if not is_test_file(path):
            # documentation snippets and scripts: their security still matters, their style less
            found.extend(f for f in code_findings(path, source)
                         if not support or f.category == "security")  # fmt: skip
        found.extend(
            f
            for f in text_findings(path, source)
            # a large module of data (generated tables) has no responsibilities to split
            if not (f.rule == "large-file" and (support or path not in behaviour))
        )
    complex_files = {f.file for f in found if f.rule == "high-complexity"}
    found.extend(graph_findings(graph, complex_files, behaviour))
    order = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted(found, key=lambda f: (f.file, order[f.severity], f.line, f.rule))
