from tokenwise.repo_map import (
    build_map,
    estimate_tokens,
    list_source_files,
    outline_js,
    outline_python,
    source_tokens,
)


def test_lists_source_files_and_skips_ignored(settings):
    assert list_source_files(settings) == ["app/auth.py", "app/util.py", "web/api.ts"]


def test_python_outline_keeps_signatures_not_bodies():
    source = (
        "import os\n"
        "class A(Base):\n"
        "    def f(self, x: int = 1) -> str:\n"
        "        return 'x'\n"
        "async def g(*args, **kw):\n"
        "    pass\n"
    )
    assert outline_python(source) == [
        "class A(Base)",
        "  def f(self, x: int=1) -> str",
        "async def g(*args, **kw)",
    ]


def test_python_outline_survives_syntax_errors():
    assert outline_python("def broken(:\n") == ["(could not parse)"]


def test_js_outline_finds_functions_classes_arrows_and_types():
    source = (
        "export async function fetchUser(id: string) {}\n"
        "const sum = (a, b) => a + b;\n"
        "export default class Store {}\n"
        "export interface User { id: string }\n"
        "  if (x) { call(); }\n"
    )
    assert outline_js(source) == [
        "function fetchUser(id: string)",
        "const sum = (a, b) =>",
        "class Store",
        "interface User",
    ]


def test_map_is_smaller_than_source(settings):
    files = list_source_files(settings)
    repo_map = build_map(settings, files)
    assert "app/auth.py\n  def login(user, password)" in repo_map
    assert "return True" not in repo_map
    assert estimate_tokens(repo_map) <= source_tokens(settings, files)


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0
