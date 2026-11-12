from tokenwise.config import Settings
from tokenwise.editor import Edit, build_prompt, plan_changes, propose
from tokenwise.focus import GAP, focused_view
from tokenwise.repo_map import build_map
from tokenwise.selector import parse_targets


def big_python() -> str:
    parts = ["import os\nimport sys\n\nLIMIT = 3\n\n"]
    for i in range(60):
        parts.append(f"def helper_{i}(x):\n    y = x + {i}\n    return y * 2\n\n\n")
    parts.append(
        "class Store:\n"
        "    def __init__(self):\n        self.items = []\n\n"
        "    @property\n    def size(self):\n        return len(self.items)\n\n"
        "    def add(self, item):\n        self.items.append(item)\n"
    )
    return "".join(parts)


def big_ts() -> str:
    parts = ['import { api } from "./api";\n\n']
    for i in range(50):
        parts.append(f"export function helper{i}(x: number): number {{\n  return x + {i};\n}}\n\n")
    parts.append(
        "export class Cart {\n  items: string[] = [];\n\n"
        "  add(item: string) {\n    if (item) { this.items.push(item); }\n  }\n}\n\n"
        "export const total = (xs: number[]) => xs.reduce((a, b) => a + b, 0);\n"
    )
    return "".join(parts)


def test_python_view_has_header_symbols_and_gaps():
    source = big_python()
    view = focused_view("store.py", source, ["Store.size", "helper_5"])
    assert view.startswith("import os\nimport sys\n\nLIMIT = 3\n")
    assert "def helper_5(x):\n    y = x + 5\n" in view
    assert "    @property\n    def size(self):\n        return len(self.items)\n" in view
    assert "def helper_6" not in view.split("Everything defined")[0]
    assert f"{GAP} " in view and "lines omitted" in view
    assert "Everything defined in this file:" in view and "def helper_59(x)" in view
    assert len(view) < len(source) / 2


def test_python_unknown_symbol_or_small_gain_means_whole_file():
    source = big_python()
    assert focused_view("store.py", source, ["nope"]) is None
    dense = "def a():\n    return 1\n" + "".join(f"def f{i}():\n    return {i}\n" for i in range(9))
    names = ["a", *(f"f{i}" for i in range(9))]
    assert focused_view("d.py", dense, names) is None  # would show everything: not worth it


def test_ts_view_finds_class_and_arrow_function():
    view = focused_view("cart.ts", big_ts(), ["Cart", "total"])
    assert 'import { api } from "./api";' in view
    assert "export class Cart {\n" in view and "    if (item) { this.items.push(item); }\n" in view
    assert "export const total = (xs: number[]) =>" in view
    assert "helper7(x: number)" not in view.split("Everything defined")[0]


def test_ts_method_and_missing_symbol():
    assert "  add(item: string) {" in focused_view("cart.ts", big_ts(), ["Cart.add"])
    assert focused_view("cart.ts", big_ts(), ["Missing"]) is None


def test_parse_targets():
    paths, focus = parse_targets(
        '["a.py::f", "a.py::C.m", "b.py", "c.py::g", "c.py", "../x.py::h"]'
    )
    assert paths == ["a.py", "b.py", "c.py"]
    assert focus == {"a.py": ["f", "C.m"]}  # c.py was also named whole


def test_map_marks_large_files(tmp_path):
    (tmp_path / "big.py").write_text(big_python())
    (tmp_path / "small.py").write_text("def f():\n    pass\n")
    settings = Settings(root=tmp_path)
    text = build_map(settings, ["big.py", "small.py"])
    assert "big.py [large: " in text and "small.py\n" in text and "small.py [" not in text


def test_prompt_is_focused_and_edit_still_applies(tmp_path, fake):
    (tmp_path / "store.py").write_text(big_python())
    settings = Settings(root=tmp_path)
    full = build_prompt("x", ["store.py"], tmp_path, ["store.py"])
    focused = build_prompt(
        "x", ["store.py"], tmp_path, ["store.py"], focus={"store.py": ["Store.add"]}
    )
    assert "(shown in part)" in focused and len(focused) < len(full) / 2
    small = Settings(root=tmp_path, focus_lines=10_000)
    same = build_prompt(
        "x",
        ["store.py"],
        tmp_path,
        ["store.py"],
        "",
        {"store.py": ["Store.add"]},
        small.focus_lines,
    )
    assert same == full  # below the threshold the whole file goes

    edit = "store.py\n<<<<<<< SEARCH\n        self.items.append(item)\n=======\n        if item not in self.items:\n            self.items.append(item)\n>>>>>>> REPLACE"
    llm = fake(edit)
    proposal = propose(
        "dedupe",
        ["store.py"],
        settings,
        llm,
        ["store.py"],
        ["m"],
        focus={"store.py": ["Store.add"]},
    )
    assert proposal.changes and "if item not in self.items" in proposal.changes[0].new
    assert "def helper_30" not in llm.calls[0]["prompt"].split("Everything defined")[0]


def test_need_files_on_a_focused_file_sends_it_whole(tmp_path, fake):
    (tmp_path / "store.py").write_text(big_python())
    edit = "store.py\n<<<<<<< SEARCH\nLIMIT = 3\n=======\nLIMIT = 4\n>>>>>>> REPLACE"
    llm = fake("NEED_FILES: store.py", edit)
    settings = Settings(root=tmp_path)
    proposal = propose(
        "x", ["store.py"], settings, llm, ["store.py"], ["m"], focus={"store.py": ["Store"]}
    )
    assert "(shown in part)" in llm.calls[0]["prompt"]
    assert (
        "(shown in part)" not in llm.calls[1]["prompt"]
        and "def helper_30" in llm.calls[1]["prompt"]
    )
    assert proposal.changes


def test_search_copied_with_gap_line_fails_cleanly(tmp_path):
    (tmp_path / "store.py").write_text(big_python())
    import pytest

    from tokenwise.editor import EditError

    with pytest.raises(EditError):
        plan_changes(tmp_path, [Edit("store.py", f"{GAP} 300 lines omitted\n", "x\n")])


def test_pipeline_passes_focus_from_selector(tmp_path, fake):
    from tokenwise.pipeline import run

    (tmp_path / "store.py").write_text(big_python())
    edit = "store.py\n<<<<<<< SEARCH\n        return len(self.items)\n=======\n        return len(set(self.items))\n>>>>>>> REPLACE"
    llm = fake('["store.py::Store.size"]', edit)
    result = run("count unique items", Settings(root=tmp_path), llm, model="m")
    assert result.ok
    assert "(shown in part)" in llm.calls[1]["prompt"]
