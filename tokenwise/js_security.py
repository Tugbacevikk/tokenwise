"""Security checks for JavaScript and TypeScript, the counterparts of the Python ones:
code built from strings (`eval`, `new Function`), commands run through a shell
(`child_process.exec`), and HTML written from values (`innerHTML`). They read the syntax
tree, so a regular expression's `.exec()` or a word in a comment is not mistaken for one."""

from __future__ import annotations

from dataclasses import dataclass

from . import syntax

_CHILD_PROCESS = ("child_process", "node:child_process")
_ALWAYS_SHELL = {"exec", "execSync"}  # run their argument through /bin/sh
_SHELL_OPTION = {"spawn", "spawnSync", "execFile", "execFileSync"}  # only with {shell: true}
_HTML_PROPERTIES = {"innerHTML", "outerHTML"}


@dataclass
class Hit:
    rule: str
    title: str
    detail: str
    line: int
    call: str  # what was called or assigned, for the finding's identity


def _child_process(tree) -> tuple[set[str], dict[str, str]]:
    """Names bound to the child_process module (`cp`) and to its functions (`run` -> exec)."""
    modules: set[str] = set()
    functions: dict[str, str] = {}
    for node in syntax.walk(tree.root_node):
        if node.type == "import_statement":
            if syntax.string_value(node.child_by_field_name("source")) in _CHILD_PROCESS:
                _import_names(node, modules, functions)
        elif node.type == "variable_declarator" and _requires_child_process(
            node.child_by_field_name("value")
        ):
            _require_names(node.child_by_field_name("name"), modules, functions)
    return modules, functions


def _import_names(node, modules: set[str], functions: dict[str, str]) -> None:
    for part in syntax.walk(node):
        if part.type == "identifier" and part.parent.type in (
            "import_clause",  # import cp from "child_process"
            "namespace_import",  # import * as cp from "child_process"
        ):
            modules.add(syntax.text(part))
        elif part.type == "import_specifier":  # import { exec as run } from "child_process"
            name = syntax.text(part.child_by_field_name("name"))
            alias = part.child_by_field_name("alias")
            functions[syntax.text(alias) if alias is not None else name] = name


def _require_names(target, modules: set[str], functions: dict[str, str]) -> None:
    if target is None:
        return
    if target.type == "identifier":  # const cp = require("child_process")
        modules.add(syntax.text(target))
        return
    for part in target.named_children if target.type == "object_pattern" else ():
        if part.type == "shorthand_property_identifier_pattern":  # const { exec } = ...
            functions[syntax.text(part)] = syntax.text(part)
        elif part.type == "pair_pattern":  # const { exec: run } = ...
            key = syntax.text(part.child_by_field_name("key"))
            functions[syntax.text(part.child_by_field_name("value"))] = key


def _requires_child_process(node) -> bool:
    if node is None or node.type != "call_expression":
        return False
    func, args = node.child_by_field_name("function"), node.child_by_field_name("arguments")
    first = args.named_children[0] if args is not None and args.named_children else None
    return syntax.text(func) == "require" and syntax.string_value(first) in _CHILD_PROCESS


def _shell_option(args) -> bool:
    """True when an argument is an object literal with `shell: true`."""
    for arg in args.named_children if args is not None else ():
        if arg.type != "object":
            continue
        for pair in arg.named_children:
            if pair.type == "pair" and syntax.text(pair.child_by_field_name("key")) == "shell":
                value = pair.child_by_field_name("value")
                if value is not None and value.type == "true":
                    return True
    return False


def _process_call(node, modules: set[str], functions: dict[str, str]) -> str:
    """`exec` when `node` calls a child_process function, through any of its names."""
    func = node.child_by_field_name("function")
    if func is None:
        return ""
    if func.type == "identifier":
        return functions.get(syntax.text(func), "")
    if func.type == "member_expression":
        target = func.child_by_field_name("object")
        if target is not None and syntax.text(target) in modules:
            return syntax.text(func.child_by_field_name("property"))
    return ""


def _call_hit(node, modules: set[str], functions: dict[str, str]) -> Hit | None:
    func, args = node.child_by_field_name("function"), node.child_by_field_name("arguments")
    line = node.start_point[0] + 1
    return (
        _dynamic_code(func, args, line)
        or _shell(_process_call(node, modules, functions), args, line)
        or _html_call(func, line)
    )


def _dynamic_code(func, args, line: int) -> Hit | None:
    name = syntax.text(func) if func is not None and func.type == "identifier" else ""
    first = args.named_children[0] if args is not None and args.named_children else None
    with_text = name in ("setTimeout", "setInterval") and first is not None and (
        first.type == "string"
    )  # fmt: skip
    if name in ("eval", "Function") or with_text:
        return Hit("dynamic-code", f"{name}() metni kod olarak çalıştırıyor",
                   "Metin kullanıcıdan geliyorsa kod enjeksiyonuna açık.", line, name)  # fmt: skip
    return None


def _shell(called: str, args, line: int) -> Hit | None:
    if called in _ALWAYS_SHELL or (called in _SHELL_OPTION and _shell_option(args)):
        return Hit("shell-command", f"child_process.{called} kabukla komut çalıştırıyor",
                   "Girdi kullanıcıdan geliyorsa komut enjeksiyonuna açık; execFile ve"
                   " argüman listesi kullanın.", line, called)  # fmt: skip
    return None


def _html_call(func, line: int) -> Hit | None:
    if func is None or func.type != "member_expression":
        return None
    name = syntax.text(func)
    if name != "document.write":
        name = syntax.text(func.child_by_field_name("property"))
    if name in ("insertAdjacentHTML", "document.write"):
        return Hit("unsafe-html", f"{name}() HTML'i olduğu gibi yazıyor",
                   "Değer kullanıcıdan geliyorsa XSS'e açık.", line, name)  # fmt: skip
    return None


def _html_hit(node) -> Hit | None:
    line = node.start_point[0] + 1
    if node.type in ("assignment_expression", "augmented_assignment_expression"):
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if left is None or left.type != "member_expression":
            return None
        prop = syntax.text(left.child_by_field_name("property"))
        if prop in _HTML_PROPERTIES and syntax.string_value(right) is None:
            # a fixed string is the page's own markup; only a computed value can carry input
            return Hit("unsafe-html", f"{prop} hesaplanan bir değerle yazılıyor",
                       "Değer kullanıcıdan geliyorsa XSS'e açık; textContent ya da bir"
                       " temizleyici kullanın.", line, prop)  # fmt: skip
    elif node.type == "jsx_attribute":
        if node.named_children and syntax.text(node.named_children[0]) == (
            "dangerouslySetInnerHTML"
        ):
            return Hit("unsafe-html", "dangerouslySetInnerHTML kullanılıyor",
                       "Değer kullanıcıdan geliyorsa XSS'e açık.", line,
                       "dangerouslySetInnerHTML")  # fmt: skip
    return None


def security_hits(path: str, source: str) -> list[Hit]:
    """Every risky call or assignment in a JavaScript/TypeScript file; empty when the file
    cannot be parsed here (another language, or Tree-sitter not installed)."""
    lang, tree = syntax.language_for(path), syntax.parse(path, source)
    if lang is not syntax.JAVASCRIPT or tree is None:
        return []
    modules, functions = _child_process(tree)
    hits = []
    for node in syntax.walk(tree.root_node):
        if node.type == "call_expression":
            hit = _call_hit(node, modules, functions)
        elif node.type == "new_expression":
            hit = _new_function(node)
        else:
            hit = _html_hit(node)
        if hit is not None:
            hits.append(hit)
    return hits


def _new_function(node) -> Hit | None:
    constructor = node.child_by_field_name("constructor")
    if syntax.text(constructor) != "Function":
        return None
    return Hit("dynamic-code", "new Function() metni kod olarak çalıştırıyor",
               "Metin kullanıcıdan geliyorsa kod enjeksiyonuna açık.",
               node.start_point[0] + 1, "Function")  # fmt: skip
