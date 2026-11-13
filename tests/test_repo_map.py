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


def test_map_shrinks_to_fit_the_budget(tmp_path):
    from tokenwise.config import Settings
    from tokenwise.repo_map import fit_map, is_test_file

    assert is_test_file("tests/unit/test_a.py") and is_test_file("src/a.test.ts")
    assert is_test_file("app/CartTest.java") and not is_test_file("src/latest.py")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    for i in range(30):
        (tmp_path / "src" / f"m{i}.py").write_text(
            f"class C{i}:\n    def method_{i}(self, alpha, beta, gamma):\n        pass\n\n"
            f"def function_{i}(first_argument, second_argument):\n    pass\n"
        )
        (tmp_path / "tests" / f"test_m{i}.py").write_text(
            f"def test_{i}(fixture_one, fixture_two):\n    pass\n"
        )
    files = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.py"))
    full, level = fit_map(Settings(root=tmp_path, map_max_tokens=0), files)
    assert level == "full" and "def test_0(fixture_one, fixture_two)" in full

    sizes = {}
    for budget in (10_000, 700, 450, 300, 100):
        text, level = fit_map(Settings(root=tmp_path, map_max_tokens=budget), files)
        sizes[budget] = level
        if level == "names only":
            assert "def function_0\n" in text + "\n" and "first_argument" not in text
        if level == "top-level names":
            assert "method_0" not in text and "(30 test files not listed)" in text
    assert sizes[10_000] == "full"
    from tokenwise.repo_map import LEVELS

    order = [LEVELS.index(level) for level in sizes.values()]
    assert order == sorted(order) and order[-1] > order[0]  # smaller budget, shorter map
