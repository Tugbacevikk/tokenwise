"""Tree-sitter parsers for the languages beyond Python, and for each one what counts as a
function, a branch point and a call. Every grammar is an optional dependency: a language
whose grammar is missing simply gets no function-level analysis, nothing breaks.

Python is not here on purpose: the standard library's `ast` reads it without any extra
package, and the Python analysis predates this module."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path


@dataclass(frozen=True)
class Lang:
    name: str
    suffixes: tuple[str, ...]
    load: Callable[[str], object]  # suffix -> tree-sitter language pointer (may raise ImportError)
    containers: frozenset[str]  # class-like nodes: their name prefixes what is inside
    functions: frozenset[str]  # named functions and methods (name in the "name" field)
    anonymous: frozenset[str]  # lambdas and closures: counted toward the enclosing function
    branches: frozenset[str]  # one McCabe point each
    cases: frozenset[str] = frozenset()  # one point each unless it is the `default` label
    logic: frozenset[str] = frozenset({"&&", "||"})  # short-circuit operators: one point each


def _javascript(suffix: str):
    if suffix in (".ts", ".tsx"):
        import tree_sitter_typescript as ts

        return ts.language_tsx() if suffix == ".tsx" else ts.language_typescript()
    import tree_sitter_javascript as js

    return js.language()


def _go(_suffix: str):
    import tree_sitter_go as go

    return go.language()


def _java(_suffix: str):
    import tree_sitter_java as java

    return java.language()


def _kinds(names: str) -> frozenset[str]:
    return frozenset(names.split())


JAVASCRIPT = Lang(
    name="javascript",
    suffixes=(".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"),
    load=_javascript,
    containers=_kinds("class_declaration class abstract_class_declaration"),
    functions=_kinds("function_declaration generator_function_declaration method_definition"),
    anonymous=_kinds("arrow_function function_expression function generator_function"),
    branches=_kinds(
        "if_statement for_statement for_in_statement while_statement do_statement"
        " catch_clause ternary_expression switch_case"
    ),
    logic=_kinds("&& || ??"),
)

GO = Lang(
    name="go",
    suffixes=(".go",),
    load=_go,
    containers=frozenset(),  # a method names its type in the receiver instead
    functions=_kinds("function_declaration method_declaration"),
    anonymous=_kinds("func_literal"),
    branches=_kinds("if_statement for_statement expression_case type_case communication_case"),
)

JAVA = Lang(
    name="java",
    suffixes=(".java",),
    load=_java,
    containers=_kinds(
        "class_declaration interface_declaration enum_declaration record_declaration"
    ),
    functions=_kinds("method_declaration constructor_declaration"),
    anonymous=_kinds("lambda_expression"),
    branches=_kinds(
        "if_statement for_statement enhanced_for_statement while_statement do_statement"
        " catch_clause ternary_expression"
    ),
    cases=_kinds("switch_label"),
)

LANGUAGES = (JAVASCRIPT, GO, JAVA)


def language_for(path: str) -> Lang | None:
    if path.endswith(".d.ts"):  # type declarations: no bodies to measure
        return None
    suffix = Path(path).suffix
    return next((lang for lang in LANGUAGES if suffix in lang.suffixes), None)


@cache
def parser(suffix: str):
    """A parser for files with this suffix, or None when the grammar is not installed."""
    lang = next((lang for lang in LANGUAGES if suffix in lang.suffixes), None)
    if lang is None:
        return None
    try:
        from tree_sitter import Language, Parser

        return Parser(Language(lang.load(suffix)))
    except ImportError:
        return None


def parse(path: str, source: str):
    """The syntax tree of `source`, or None when its language cannot be parsed here."""
    if language_for(path) is None:
        return None
    found = parser(Path(path).suffix)
    return None if found is None else found.parse(source.encode("utf-8"))


def available(lang: Lang) -> bool:
    return all(parser(suffix) is not None for suffix in lang.suffixes)


def text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


def string_value(node) -> str | None:
    """The contents of a plain string literal; None for templates with substitutions."""
    if node is None:
        return None
    if node.type in ("string", "interpreted_string_literal", "raw_string_literal",
                     "string_literal"):  # fmt: skip
        return text(node)[1:-1]
    if node.type == "template_string" and not any(
        c.type == "template_substitution" for c in node.children
    ):
        return text(node)[1:-1]
    return None


def walk(node):
    """Every node below `node`, in source order (depth first)."""
    stack = list(reversed(node.children))
    while stack:
        child = stack.pop()
        yield child
        stack.extend(reversed(child.children))
